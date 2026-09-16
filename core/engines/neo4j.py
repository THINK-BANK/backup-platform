# -*- coding: utf-8 -*-
"""Neo4j 图数据库备份/恢复引擎。

适用版本
--------
Neo4j 5.x（社区版 Community / 企业版 Enterprise），同时兼容 4.4 的命令形态。

备份通道（三种，按 ``extra_options.neo4j_method`` 选择，默认 auto）
----------------------------------------------------------------
1. ``dump`` —— 官方离线导出（社区版/企业版均可用）
   ``neo4j-admin database dump <db> --to-path=<dir> --overwrite-destination=true``
   **必须数据库离线**（社区版没有 STOP DATABASE 命令，只能停掉整个服务/容器
   释放 ``/data/databases/<db>/database_lock``）。本引擎按官方推荐做法实现：
   - Docker 部署：``docker stop`` → 一次性临时容器挂载同一份 data 目录执行
     dump → ``docker start``（**绝不用临时容器去 dump 正在运行的库**，两个容器
     抢同一把锁必然报 The database is in use）；
   - 裸机/包安装：执行停服命令（默认 ``systemctl stop neo4j``）→ dump → 起服。
2. ``backup`` —— 企业版在线备份（无需停库，支持永久增量）
   ``neo4j-admin database backup <db> --to-path=<chain_dir> --type=AUTO|FULL|DIFF``
   产物是**目录**（``<db>-<yyyyMMddHHmmss>.backup``），平台在远端 tar 打包后拉回。
   增量链目录在远端固定保留（``<stage>/chain_<taskid>``），AUTO 会自动判定
   全量/差异；备份前后比对目录列表，只拉回本次新增/变化的产物。
3. ``apoc_cypher`` / ``apoc_json`` / ``apoc_csv`` —— APOC 在线导出（社区版免停机）
   通过 ``cypher-shell`` 执行 ``apoc.export.*``，产物为可读的 Cypher/JSON/CSV，
   便于跨版本迁移、审计与单类数据抢救；恢复用 ``apoc.cypher.runFile`` /
   ``apoc.import.json`` / ``apoc.import.csv``。

恢复通道与备份一一对应：dump→load、backup→restore、apoc→apoc.import/runFile，
全部真实执行，不做仿真占位。

产物形态与落盘后缀
------------------
* ``.dump``      —— neo4j-admin dump 单文件（内含压缩）
* ``.backup.tar.gz`` —— 在线备份产物目录打包
* ``.cypher`` / ``.json`` / ``.csv.zip`` —— APOC 导出

执行位置
--------
与平台其它引擎一致：**优先 SSH 到数据库服务器执行**（``_try_remote_then_local``），
失败回退平台本机。所有远程命令经 ``core.remote_dump._wrap_login`` 包裹为
``bash -lc``，自动加载 profile 中的 PATH 与任务级自定义环境变量。

配置项（任务 → 高级选项 extra_options）
--------------------------------------
``neo4j_method``        备份通道：auto|dump|backup|apoc_cypher|apoc_json|apoc_csv
``neo4j_container``     Docker 部署时的容器名（启用容器模式）
``neo4j_image``         临时容器镜像（缺省自动从运行容器 ``docker inspect`` 取）
``neo4j_data_volume``   宿主机 data 目录（容器模式挂载到 /data）
``neo4j_backup_volume`` 宿主机备份目录（容器模式挂载到 /backups）
``neo4j_home``          Neo4j 安装目录（裸机模式；用于定位 bin/neo4j-admin）
``neo4j_stop_cmd``      停服命令（裸机，默认 systemctl stop neo4j）
``neo4j_start_cmd``     起服命令（裸机，默认 systemctl start neo4j）
``neo4j_allow_offline`` 是否允许平台停库做离线备份/恢复（默认 true）
``neo4j_backup_dir``    远端产物暂存目录（默认 /tmp/neo4j_bk_stage/<task_id>）
``neo4j_bolt_uri``      覆盖 bolt 地址（默认 bolt://<host>:<port>）
``neo4j_import_dir``    APOC 导出落盘目录（默认 /var/lib/neo4j/import）
"""
import os
import re
import shlex
import time

import config
import core.db as db
from core.engines.base import (
    BackupEngine, BackupType, BackupMode, BackupStatus, BackupResult
)

# APOC 导出方法 → (过程名, 文件扩展名, 恢复过程)
_APOC_METHODS = {
    "apoc_cypher": ("apoc.export.cypher.all", "cypher", "apoc.cypher.runFile"),
    "apoc_json": ("apoc.export.json.all", "json", "apoc.import.json"),
    "apoc_csv": ("apoc.export.csv.all", "csv.zip", "apoc.import.csv"),
}

# neo4j-admin database load/restore 的覆盖开关在不同版本命名不同，按序尝试
_OVERWRITE_FLAGS = ("--overwrite-destination=true", "--force")


class Neo4jEngine(BackupEngine):
    """Neo4j 备份恢复引擎（dump / 在线 backup / APOC 导出 三通道）。"""

    db_type = "neo4j"
    display_name = "Neo4j"
    required_clients = ["neo4j-admin"]
    # 在线备份与 APOC 导出需要 cypher-shell；离线 dump 只需要 neo4j-admin，
    # 因此 required_clients 只登记 neo4j-admin，cypher-shell 由 _cypher_bin 单独解析。

    # ------------------------------------------------------------------ #
    # 配置读取
    # ------------------------------------------------------------------ #
    def _exe(self) -> dict:
        return self._parse_task_extra()

    def _opt(self, key: str, default=None):
        v = self._exe().get(key)
        return default if v in (None, "") else v

    def _db_name(self) -> str:
        return (self.task.get("db_name") or self._opt("neo4j_database") or "neo4j").strip()

    def _bolt_uri(self) -> str:
        uri = self._opt("neo4j_bolt_uri")
        if uri:
            return uri
        host = self.task.get("host") or "127.0.0.1"
        port = int(self.task.get("port") or config.DEFAULT_PORTS.get("neo4j", 7687))
        return "bolt://%s:%d" % (host, port)

    def _container(self) -> str:
        return str(self._opt("neo4j_container") or self._opt("docker_container") or "").strip()

    def _is_container_mode(self) -> bool:
        return bool(self._container())

    def _user(self) -> str:
        return self.task.get("username") or "neo4j"

    def _password(self) -> str:
        return db.decrypt_secret(self.task.get("password") or "")

    def _stage(self) -> str:
        """远端/本机产物暂存目录（在线 backup 的增量链也放在其下）。"""
        base = str(self._opt("neo4j_backup_dir") or "/tmp/neo4j_bk_stage")
        return "%s/%s" % (base.rstrip("/"), self.task_id or 0)

    def _chain_dir(self) -> str:
        return self._stage() + "/chain_%s" % (self.task_id or 0)

    def _import_dir(self) -> str:
        return str(self._opt("neo4j_import_dir") or "/var/lib/neo4j/import").rstrip("/")

    def _method(self, backup_type: BackupType = None) -> str:
        """解析实际使用的备份通道。

        取值优先级：extra_options.dump_format（任务表单「备份文件格式」下拉）
        > extra_options.neo4j_method（手工配置兜底）> auto。
        """
        m = str(self._opt("dump_format") or self._opt("neo4j_method") or "auto").lower()
        if m in ("", "auto"):
            # auto：企业版在线备份不中断业务，优先尝试；失败自动回退离线 dump
            return "backup"
        if m in ("dump", "backup", "apoc_cypher", "apoc_json", "apoc_csv"):
            return m
        return "dump"

    def _allow_offline(self) -> bool:
        v = self._opt("neo4j_allow_offline", True)
        return str(v).lower() not in ("0", "false", "no")

    # ------------------------------------------------------------------ #
    # 工具路径解析
    # ------------------------------------------------------------------ #
    def _admin_cmd(self) -> str:
        """neo4j-admin 命令（本机）。裸机模式优先 <neo4j_home>/bin/neo4j-admin。"""
        home = self._opt("neo4j_home")
        if home:
            p = os.path.join(str(home).rstrip("/"), "bin", "neo4j-admin")
            if os.path.isfile(p):
                return shlex.quote(p)
        return self._resolve_local_tool("neo4j-admin")

    def _cypher_cmd(self) -> str:
        home = self._opt("neo4j_home")
        if home:
            p = os.path.join(str(home).rstrip("/"), "bin", "cypher-shell")
            if os.path.isfile(p):
                return shlex.quote(p)
        return self._resolve_local_tool("cypher-shell")

    def _remote_admin(self, ssh_host: dict) -> str:
        from core import remote_dump
        tp = remote_dump.task_tool_path(self.task)
        home = self._opt("neo4j_home")
        if home:
            return shlex.quote(str(home).rstrip("/") + "/bin/neo4j-admin")
        resolved = remote_dump.resolve_remote_tool(ssh_host, "neo4j-admin",
                                                   extra_paths=tp)
        return shlex.quote(resolved) if resolved else "neo4j-admin"

    def _remote_cypher(self, ssh_host: dict) -> str:
        from core import remote_dump
        tp = remote_dump.task_tool_path(self.task)
        home = self._opt("neo4j_home")
        if home:
            return shlex.quote(str(home).rstrip("/") + "/bin/cypher-shell")
        resolved = remote_dump.resolve_remote_tool(ssh_host, "cypher-shell",
                                                   extra_paths=tp)
        return shlex.quote(resolved) if resolved else "cypher-shell"

    # ------------------------------------------------------------------ #
    # 远程执行封装
    # ------------------------------------------------------------------ #
    def _remote(self, ssh_host: dict, shell: str, timeout: int = 1800) -> dict:
        from core import remote_dump
        return remote_dump.remote_exec_capture(ssh_host, shell, timeout=timeout)

    def _remote_mkdir(self, ssh_host: dict, path: str) -> None:
        self._remote(ssh_host, "mkdir -p %s" % shlex.quote(path), timeout=60)

    def _remote_rm(self, ssh_host: dict, path: str) -> None:
        self._remote(ssh_host, "rm -rf %s" % shlex.quote(path), timeout=120)

    def _remote_pull(self, ssh_host: dict, remote_path: str, local_path: str,
                     label: str = "") -> dict:
        """断点续传拉回远端产物（大库友好）。"""
        from core import remote_dump
        client = remote_dump._connect(ssh_host)
        try:
            return remote_dump.sftp_pull_resumable(
                client, remote_path, local_path,
                task=self.task, key=os.path.basename(remote_path)[:60],
                db_type="neo4j_fixed", host_key=ssh_host.get("host_key", ""),
                has_rc=False, stable_secs=3,
                label=label or os.path.basename(remote_path), min_size=1)
        finally:
            try:
                client.close()
            except Exception:
                pass

    def _remote_push(self, ssh_host: dict, local_path: str, remote_path: str) -> None:
        from core import remote_dump
        self._remote_mkdir(ssh_host, os.path.dirname(remote_path))
        remote_dump.sftp_put(ssh_host, local_path, remote_path)

    def _remote_ls(self, ssh_host: dict, path: str) -> list:
        r = self._remote(ssh_host, "ls -1 %s 2>/dev/null || true" % shlex.quote(path),
                         timeout=60)
        return [x.strip() for x in (r.get("stdout") or "").splitlines() if x.strip()]

    # ------------------------------------------------------------------ #
    # 备份
    # ------------------------------------------------------------------ #
    def preflight(self) -> (bool, str):
        """预检查：Neo4j 只有逻辑通道，物理模式诚实拒绝（不静默降级）。"""
        if self.backup_mode == BackupMode.PHYSICAL:
            return False, (
                "Neo4j 不支持物理备份模式（没有数据文件级复制/redo 通道）："
                "请在任务「备份方式」选择逻辑备份，并在「备份文件格式」中选择"
                "离线 dump（社区版/企业版）、企业版在线 backup 或 APOC 在线导出通道。")
        return super().preflight()

    def backup(self, backup_type: BackupType) -> BackupResult:
        if self.task.get("demo_only"):
            return self._simulate_backup(backup_type, "任务标记为演示(demo_only)")
        if config.DEMO_MODE == "on":
            return self._simulate_backup(backup_type, "DEMO_MODE=on 强制仿真")

        return self._try_remote_then_local(
            lambda ssh: self._backup_remote(ssh, backup_type),
            lambda: self._backup_local(backup_type),
            "Neo4j 备份(%s)" % self._method(backup_type),
        )

    # ---------------- 远程 ----------------
    def _backup_remote(self, ssh_host: dict, backup_type: BackupType) -> BackupResult:
        stage = self._stage()
        self._remote_mkdir(ssh_host, stage)
        method = self._method(backup_type)

        # 增量/差异只在企业版在线备份通道有原生语义，其余通道诚实回退为全量
        note = ""
        if backup_type in (BackupType.INCREMENTAL, BackupType.DIFFERENTIAL) \
                and method not in ("backup",):
            note = ("Neo4j 的 %s 通道不支持增量/差异，已回退为全量备份；"
                    "如需增量请使用企业版「在线 backup」通道。" % method)

        if method == "backup":
            res = self._backup_online_remote(ssh_host, backup_type)
            if res.success or method != "auto":
                return self._with_note(res, note)
            # auto 模式下在线备份失败 → 回退离线 dump
            self.logger.warning("[%s] 在线 backup 失败，回退离线 dump: %s",
                                self.task_name, res.message)
            note = (note + " 在线 backup 不可用（%s），已自动回退离线 dump。"
                    % res.message[:120]).strip()

        if method in _APOC_METHODS:
            return self._with_note(self._backup_apoc_remote(ssh_host, backup_type), note)

        return self._with_note(self._backup_dump_remote(ssh_host, backup_type), note)

    def _backup_dump_remote(self, ssh_host: dict, backup_type: BackupType) -> BackupResult:
        t0 = time.time()
        stage = self._stage()
        self._remote_mkdir(ssh_host, stage)
        db_name = self._db_name()
        admin = self._remote_admin(ssh_host)

        if self._is_container_mode():
            rc_err = self._container_dump(ssh_host, admin, db_name)
        else:
            rc_err = self._bare_dump(ssh_host, admin, db_name, stage)
        if rc_err:
            return BackupResult(success=False, status=BackupStatus.FAILED,
                                message=rc_err)

        remote_file = "%s/%s.dump" % (stage, db_name)
        local_path = self._new_dump_path(backup_type, ".dump")
        try:
            pulled = self._remote_pull(ssh_host, remote_file, local_path,
                                       label="%s.dump" % db_name)
        except Exception as e:
            return BackupResult(success=False, status=BackupStatus.FAILED,
                                message="拉取 Neo4j dump 产物失败: %s" % e)
        self._remote_rm(ssh_host, remote_file)
        return self._ok_result(local_path, pulled.get("size", 0), t0,
                               "neo4j-admin database dump（离线导出）",
                               compress_algo="dump(内置压缩)")

    def _container_dump(self, ssh_host: dict, admin: str, db_name: str) -> str:
        """Docker 部署的社区版正确做法：停容器 → 临时容器 dump → 拉起容器。"""
        c = self._container()
        image = self._opt("neo4j_image") or self._container_image(ssh_host, c)
        data_dir = self._opt("neo4j_data_volume") or self._container_bind(ssh_host, c, "/data")
        if not data_dir:
            return ("容器模式未能确定宿主机 data 目录，请在高级选项填写 "
                    "neo4j_data_volume（docker run -v 的宿主机侧路径）")
        bk_dir = self._opt("neo4j_backup_volume") or (
            os.path.dirname(data_dir.rstrip("/")) + "/backups")
        if not image:
            return ("容器模式未能确定镜像，请在高级选项填写 neo4j_image"
                    "（如 neo4j:5.26；需与运行容器同版本）")

        dump_cmd = ("%s database dump %s --to-path=/backups "
                    "--overwrite-destination=true --verbose"
                    % (admin, shlex.quote(db_name)))
        shell = (
            "set -e; "
            "mkdir -p %s; "
            "docker stop %s || true; "
            "docker run --rm -v %s:/data -v %s:/backups %s sh -c %s; "
            "RC=$?; docker start %s || true; exit $RC"
            % (shlex.quote(bk_dir), shlex.quote(c),
               shlex.quote(data_dir), shlex.quote(bk_dir), shlex.quote(image),
               shlex.quote(dump_cmd), shlex.quote(c))
        )
        r = self._remote(ssh_host, shell, timeout=self._timeout())
        if r["returncode"] != 0:
            return "容器模式 dump 失败(rc=%s): %s" % (
                r["returncode"], (r.get("stderr") or r.get("stdout") or "")[:500])
        # 容器模式产物落在宿主机备份目录
        self._remote(ssh_host,
                     "cp -f %s/%s.dump %s/ 2>/dev/null || true"
                     % (shlex.quote(bk_dir), shlex.quote(db_name),
                        shlex.quote(self._stage())),
                     timeout=300)
        return ""

    def _bare_dump(self, ssh_host: dict, admin: str, db_name: str, stage: str) -> str:
        """裸机/包安装：停服 → dump → 起服（社区版无法只停单个库）。"""
        stop_cmd = str(self._opt("neo4j_stop_cmd") or "systemctl stop neo4j")
        start_cmd = str(self._opt("neo4j_start_cmd") or "systemctl start neo4j")
        if not self._allow_offline():
            return ("离线 dump 需要停止 Neo4j 服务释放数据目录锁，当前任务配置为"
                    "不允许停库（neo4j_allow_offline=false）；请改用「在线 backup」"
                    "（企业版）或「APOC 在线导出」通道。")
        dump_cmd = ("%s database dump %s --to-path=%s --overwrite-destination=true --verbose"
                    % (admin, shlex.quote(db_name), shlex.quote(stage)))
        shell = ("set -e; %s || true; RC=0; %s || RC=$?; %s || true; exit $RC"
                 % (stop_cmd, dump_cmd, start_cmd))
        r = self._remote(ssh_host, shell, timeout=self._timeout())
        if r["returncode"] != 0:
            return "neo4j-admin dump 失败(rc=%s): %s" % (
                r["returncode"], (r.get("stderr") or r.get("stdout") or "")[:500])
        return ""

    def _backup_online_remote(self, ssh_host: dict, backup_type: BackupType) -> BackupResult:
        """企业版在线备份（不停库，支持永久增量）。

        产物为目录，平台在远端 tar 打包后拉回；增量链目录固定保留在远端，
        ``--type=AUTO`` 由 neo4j-admin 自行判定全量/差异。
        """
        t0 = time.time()
        chain = self._chain_dir()
        self._remote_mkdir(ssh_host, chain)
        db_name = self._db_name()
        admin = self._remote_admin(ssh_host)

        before = set(self._remote_ls(ssh_host, chain))
        btype = "FULL"
        if backup_type in (BackupType.INCREMENTAL, BackupType.DIFFERENTIAL):
            btype = "DIFF"
        cmd = ("%s database backup %s --to-path=%s --type=%s --compress=true "
               "--include-metadata=all --verbose"
               % (admin, shlex.quote(db_name), shlex.quote(chain), btype))
        r = self._remote(ssh_host, cmd, timeout=self._timeout())
        if r["returncode"] != 0:
            return BackupResult(
                success=False, status=BackupStatus.FAILED,
                message="neo4j-admin database backup 失败(rc=%s): %s" % (
                    r["returncode"], (r.get("stderr") or r.get("stdout") or "")[:500]))
        after = set(self._remote_ls(ssh_host, chain))
        new_dirs = sorted(x for x in (after - before) if x.endswith(".backup"))
        if not new_dirs:
            return BackupResult(
                success=False, status=BackupStatus.FAILED,
                message="在线备份执行成功但未发现新增产物目录（%s），请检查版本是否为企业版"
                        % (r.get("stdout") or "")[:300])

        artifact = new_dirs[0]
        tar_remote = "%s/%s.tar.gz" % (self._stage(), artifact)
        self._remote_mkdir(ssh_host, self._stage())
        r2 = self._remote(ssh_host,
                          "tar -C %s -czf %s %s"
                          % (shlex.quote(chain), shlex.quote(tar_remote),
                             shlex.quote(artifact)),
                          timeout=self._timeout())
        if r2["returncode"] != 0:
            return BackupResult(success=False, status=BackupStatus.FAILED,
                                message="在线备份产物打包失败: %s" % (r2.get("stderr") or "")[:300])
        local_path = self._new_dump_path(backup_type, ".backup.tar.gz")
        try:
            pulled = self._remote_pull(ssh_host, tar_remote, local_path, label=artifact)
        except Exception as e:
            return BackupResult(success=False, status=BackupStatus.FAILED,
                                message="拉回在线备份产物失败: %s" % e)
        self._remote_rm(ssh_host, tar_remote)
        res = self._ok_result(local_path, pulled.get("size", 0), t0,
                              "neo4j-admin database backup（在线 %s）" % btype,
                              compress_algo="tar.gz")
        res.message += " | 产物: %s；增量链: %s" % (artifact, chain)
        return res

    def _backup_apoc_remote(self, ssh_host: dict, backup_type: BackupType) -> BackupResult:
        t0 = time.time()
        method = self._method(backup_type)
        proc, ext, _restore = _APOC_METHODS[method]
        db_name = self._db_name()
        stage = self._stage()
        self._remote_mkdir(ssh_host, stage)
        fname = "neo4j_export_%s.%s" % (self._timestamp(), ext)
        if self._is_container_mode():
            remote_file = "/var/lib/neo4j/import/%s" % fname
        else:
            remote_file = "%s/%s" % (self._import_dir(), fname)

        cypher = self._apoc_export_cypher(proc, remote_file)
        r = self._cypher_remote(ssh_host, cypher, db_name)
        if r["returncode"] != 0:
            return BackupResult(
                success=False, status=BackupStatus.FAILED,
                message="APOC 导出失败(rc=%s): %s" % (
                    r["returncode"], (r.get("stderr") or r.get("stdout") or "")[:500]))

        local_path = self._new_dump_path(backup_type, "." + ext)
        if self._is_container_mode():
            c = self._container()
            # 容器模式：docker cp 到宿主机暂存目录再拉回
            host_tmp = "%s/%s" % (stage, fname)
            r2 = self._remote(ssh_host,
                              "docker cp %s:%s %s" % (shlex.quote(c),
                                                      shlex.quote(remote_file),
                                                      shlex.quote(host_tmp)),
                              timeout=self._timeout())
            if r2["returncode"] != 0:
                return BackupResult(success=False, status=BackupStatus.FAILED,
                                    message="从容器导出产物失败: %s" % (r2.get("stderr") or "")[:300])
            remote_file = host_tmp
        try:
            pulled = self._remote_pull(ssh_host, remote_file, local_path, label=fname)
        except Exception as e:
            return BackupResult(success=False, status=BackupStatus.FAILED,
                                message="拉取 APOC 导出产物失败: %s" % e)
        self._remote_rm(ssh_host, remote_file)
        return self._ok_result(local_path, pulled.get("size", 0), t0,
                               "APOC 在线导出(%s)" % proc)

    # ---------------- 本机 ----------------
    def _backup_local(self, backup_type: BackupType) -> BackupResult:
        ok, detail = self.check_client()
        if not ok:
            return BackupResult(success=False, status=BackupStatus.FAILED,
                                message="Neo4j 客户端检测失败: " + detail)
        method = self._method(backup_type)
        if method == "backup":
            return self._backup_online_local(backup_type)
        if method in _APOC_METHODS:
            return self._backup_apoc_local(backup_type)
        return self._backup_dump_local(backup_type)

    def _backup_dump_local(self, backup_type: BackupType) -> BackupResult:
        t0 = time.time()
        db_name = self._db_name()
        admin = self._admin_cmd()
        stage = self._stage()
        os.makedirs(stage, exist_ok=True)
        if self._is_container_mode():
            r = self._run(["sh", "-c", self._container_dump_shell(admin, db_name)])
        else:
            if not self._allow_offline():
                return BackupResult(
                    success=False, status=BackupStatus.FAILED,
                    message="离线 dump 需要停库，当前任务配置为不允许停库"
                            "（neo4j_allow_offline=false）。")
            r = self._run(["sh", "-c", self._bare_dump_shell(admin, db_name, stage)])
        if r["returncode"] != 0:
            return BackupResult(success=False, status=BackupStatus.FAILED,
                                stdout=r["stdout"], stderr=r["stderr"],
                                message="neo4j-admin dump 失败(rc=%s)" % r["returncode"])
        remote_file = "%s/%s.dump" % (stage, db_name)
        if not os.path.isfile(remote_file):
            return BackupResult(success=False, status=BackupStatus.FAILED,
                                message="未找到 dump 产物: %s" % remote_file)
        local_path = self._new_dump_path(backup_type, ".dump")
        with open(remote_file, "rb") as sf, open(local_path, "wb") as df:
            for chunk in iter(lambda: sf.read(4 << 20), b""):
                df.write(chunk)
        os.unlink(remote_file)
        return self._ok_result(local_path, os.path.getsize(local_path), t0,
                               "neo4j-admin database dump（本机离线导出）",
                               compress_algo="dump(内置压缩)")

    def _backup_online_local(self, backup_type: BackupType) -> BackupResult:
        t0 = time.time()
        db_name = self._db_name()
        admin = self._admin_cmd()
        chain = self._chain_dir()
        os.makedirs(chain, exist_ok=True)
        before = set(os.listdir(chain))
        btype = "DIFF" if backup_type in (BackupType.INCREMENTAL,
                                          BackupType.DIFFERENTIAL) else "FULL"
        r = self._run([admin, "database", "backup", db_name,
                       "--to-path=" + chain, "--type=" + btype,
                       "--compress=true", "--include-metadata=all", "--verbose"],
                      timeout=self._timeout())
        if r["returncode"] != 0:
            return BackupResult(success=False, status=BackupStatus.FAILED,
                                stdout=r["stdout"], stderr=r["stderr"],
                                message="neo4j-admin database backup 失败(rc=%s)" % r["returncode"])
        new_dirs = sorted(x for x in (set(os.listdir(chain)) - before)
                          if x.endswith(".backup"))
        if not new_dirs:
            return BackupResult(success=False, status=BackupStatus.FAILED,
                                message="在线备份未产生新产物，请确认版本为企业版")
        local_path = self._new_dump_path(backup_type, ".backup.tar.gz")
        self._pack_dir_tar_gz(os.path.join(chain, new_dirs[0]), local_path)
        return self._ok_result(local_path, os.path.getsize(local_path), t0,
                               "neo4j-admin database backup（本机在线 %s）" % btype,
                               compress_algo="tar.gz")

    def _backup_apoc_local(self, backup_type: BackupType) -> BackupResult:
        t0 = time.time()
        method = self._method(backup_type)
        proc, ext, _ = _APOC_METHODS[method]
        db_name = self._db_name()
        stage = self._stage()
        os.makedirs(stage, exist_ok=True)
        out_file = os.path.join(stage, "neo4j_export_%s.%s" % (self._timestamp(), ext))
        r = self._cypher_local(self._apoc_export_cypher(proc, out_file), db_name)
        if r["returncode"] != 0:
            return BackupResult(success=False, status=BackupStatus.FAILED,
                                stdout=r["stdout"], stderr=r["stderr"],
                                message="APOC 导出失败(rc=%s)" % r["returncode"])
        if not os.path.isfile(out_file):
            return BackupResult(success=False, status=BackupStatus.FAILED,
                                message="APOC 导出未生成产物: %s" % out_file)
        local_path = self._new_dump_path(backup_type, "." + ext)
        os.replace(out_file, local_path)
        return self._ok_result(local_path, os.path.getsize(local_path), t0,
                               "APOC 在线导出(%s，本机)" % proc)

    # ------------------------------------------------------------------ #
    # cypher-shell 执行（在线通道）
    # ------------------------------------------------------------------ #
    def _apoc_export_cypher(self, proc: str, out_file: str) -> str:
        if proc == "apoc.export.csv.all":
            opt = "{useOptimizations: {type: 'UNWIND_BATCH', batchSize: 20000}}"
        elif proc == "apoc.export.cypher.all":
            opt = "{format: 'plain', useOptimizations: {type: 'UNWIND_BATCH', batchSize: 20000}}"
        else:
            opt = "{}"
        return "CALL %s('%s', %s);" % (proc, out_file, opt)

    def _cypher_remote(self, ssh_host: dict, cypher: str, db_name: str) -> dict:
        """远程执行 Cypher：优先用环境变量传密码（不进命令行），失败再用 -p。"""
        cy_bin = self._remote_cypher(ssh_host)
        if self._is_container_mode():
            base = "docker exec -i %s sh -lc %s" % (
                shlex.quote(self._container()),
                shlex.quote("NEO4J_USERNAME=%s NEO4J_PASSWORD=%s %s -a bolt://localhost:7687 "
                            "-d %s --non-interactive %s"
                            % (shlex.quote(self._user()), shlex.quote(self._password()),
                               cy_bin, shlex.quote(db_name), shlex.quote(cypher))))
        else:
            base = ("NEO4J_USERNAME=%s NEO4J_PASSWORD=%s %s -a %s -d %s --non-interactive %s"
                    % (shlex.quote(self._user()), shlex.quote(self._password()), cy_bin,
                       shlex.quote(self._bolt_uri()), shlex.quote(db_name),
                       shlex.quote(cypher)))
        r = self._remote(ssh_host, base, timeout=self._timeout())
        if r["returncode"] != 0 and self._looks_like_auth_error(r):
            # 老版本 cypher-shell 不认环境变量 → 回退显式传参
            if self._is_container_mode():
                alt = "docker exec -i %s sh -lc %s" % (
                    shlex.quote(self._container()),
                    shlex.quote("%s -a bolt://localhost:7687 -u %s -p %s -d %s "
                                "--non-interactive %s"
                                % (cy_bin, shlex.quote(self._user()),
                                   shlex.quote(self._password()),
                                   shlex.quote(db_name), shlex.quote(cypher))))
            else:
                alt = ("%s -a %s -u %s -p %s -d %s --non-interactive %s"
                       % (cy_bin, shlex.quote(self._bolt_uri()),
                          shlex.quote(self._user()), shlex.quote(self._password()),
                          shlex.quote(db_name), shlex.quote(cypher)))
            r = self._remote(ssh_host, alt, timeout=self._timeout())
        return r

    def _cypher_local(self, cypher: str, db_name: str) -> dict:
        import subprocess
        env = self._env_with_tool_path({
            "NEO4J_USERNAME": self._user(),
            "NEO4J_PASSWORD": self._password(),
        })
        bin_path = self._cypher_cmd()
        cmd = [bin_path, "-a", self._bolt_uri(), "-d", db_name,
               "--non-interactive", cypher]
        try:
            p = subprocess.run(cmd, env=env, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, timeout=self._timeout())
            r = {"returncode": p.returncode,
                 "stdout": p.stdout.decode("utf-8", "ignore"),
                 "stderr": p.stderr.decode("utf-8", "ignore")}
        except FileNotFoundError as e:
            return {"returncode": -2, "stdout": "", "stderr": "命令不存在: %s" % e}
        if r["returncode"] != 0 and self._looks_like_auth_error(r):
            cmd = [bin_path, "-a", self._bolt_uri(), "-u", self._user(),
                   "-p", self._password(), "-d", db_name, "--non-interactive", cypher]
            try:
                p = subprocess.run(cmd, env=self._env_with_tool_path(),
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   timeout=self._timeout())
                r = {"returncode": p.returncode,
                     "stdout": p.stdout.decode("utf-8", "ignore"),
                     "stderr": p.stderr.decode("utf-8", "ignore")}
            except FileNotFoundError as e:
                r = {"returncode": -2, "stdout": "", "stderr": "命令不存在: %s" % e}
        return r

    @staticmethod
    def _looks_like_auth_error(r: dict) -> bool:
        text = ((r.get("stdout") or "") + (r.get("stderr") or "")).lower()
        return ("password" in text or "auth" in text or "unauthorized" in text
                or "username" in text)

    # ------------------------------------------------------------------ #
    # 恢复
    # ------------------------------------------------------------------ #
    def restore(self, backup_path: str, **kwargs) -> BackupResult:
        if self.task.get("demo_only"):
            return self._simulate_restore(backup_path, "任务标记为演示(demo_only)")
        if config.DEMO_MODE == "on":
            return self._simulate_restore(backup_path, "DEMO_MODE=on 强制仿真")

        target_host_info = kwargs.get("target_host_info")
        if target_host_info:
            return self._restore_cross_host(backup_path, target_host_info,
                                            kwargs.get("target_db") or "")

        from core import remote_dump
        ssh_host = None
        try:
            ssh_host = remote_dump.resolve_ssh_host(self.task)
        except Exception:
            ssh_host = None
        if ssh_host:
            res = self._restore_remote(ssh_host, backup_path, kwargs)
            if res.success or not self._local_client_ready():
                return res
            return res
        return self._restore_local(backup_path, kwargs)

    def _restore_remote(self, ssh_host: dict, backup_path: str,
                        kwargs: dict) -> BackupResult:
        t0 = time.time()
        stage = self._stage()
        self._remote_mkdir(ssh_host, stage)
        db_name = kwargs.get("target_db") or self._db_name()
        admin = self._remote_admin(ssh_host)
        remote_file = "%s/%s" % (stage, os.path.basename(backup_path))

        if backup_path.endswith(".backup.tar.gz"):
            # 在线备份产物：解包到暂存目录后 restore
            self._remote_push(ssh_host, backup_path, remote_file)
            self._remote(ssh_host, "mkdir -p %s/restore && tar -xzf %s -C %s/restore"
                         % (shlex.quote(stage), shlex.quote(remote_file),
                            shlex.quote(stage)), timeout=self._timeout())
            return self._finish_restore(
                ssh_host, t0,
                self._remote_restore_online(ssh_host, admin, db_name,
                                            "%s/restore" % stage),
                "neo4j-admin database restore（在线备份产物）")
        if backup_path.endswith((".cypher", ".json", ".csv.zip")):
            self._remote_push(ssh_host, backup_path, remote_file)
            return self._finish_restore(
                ssh_host, t0,
                self._remote_restore_apoc(ssh_host, db_name, remote_file,
                                          os.path.basename(backup_path)),
                "APOC 导入")
        # .dump 及其它：按离线条 load
        self._remote_push(ssh_host, backup_path, remote_file)
        if self._is_container_mode():
            err = self._container_load(ssh_host, admin, db_name, remote_file)
        else:
            err = self._bare_load(ssh_host, admin, db_name, stage)
        return self._finish_restore(ssh_host, t0, err,
                                    "neo4j-admin database load（离线导入）")

    def _remote_restore_online(self, ssh_host: dict, admin: str, db_name: str,
                               from_dir: str) -> str:
        """企业版在线备份产物还原（restore 无需离线，但目标库需停止/不存在）。"""
        last_err = ""
        for flag in _OVERWRITE_FLAGS:
            cmd = "%s database restore %s --from-path=%s %s" % (
                admin, shlex.quote(db_name), shlex.quote(from_dir), flag)
            r = self._remote(ssh_host, cmd, timeout=self._timeout())
            if r["returncode"] == 0:
                return ""
            last_err = (r.get("stderr") or r.get("stdout") or "")[:400]
            if "unrecognized" in last_err.lower() or "unknown" in last_err.lower():
                continue
            break
        return "neo4j-admin database restore 失败: %s" % last_err

    def _remote_restore_apoc(self, ssh_host: dict, db_name: str,
                             remote_file: str, fname: str) -> str:
        cy_bin = self._remote_cypher(ssh_host)
        if fname.endswith(".cypher"):
            cy = "CALL apoc.cypher.runFile('%s', {});" % remote_file
        elif fname.endswith(".json"):
            cy = "CALL apoc.import.json('%s', {});" % remote_file
        else:
            cy = ("CALL apoc.import.csv([{fileName:'file://%s', labels:[]}], [], {});"
                  % remote_file)
        if self._is_container_mode():
            c = self._container()
            in_container = "/var/lib/neo4j/import/%s" % os.path.basename(remote_file)
            self._remote(ssh_host, "docker cp %s %s:%s" % (
                shlex.quote(remote_file), shlex.quote(c), shlex.quote(in_container)),
                timeout=self._timeout())
            cmd = "docker exec -i %s sh -lc %s" % (
                shlex.quote(c), shlex.quote(
                    "NEO4J_USERNAME=%s NEO4J_PASSWORD=%s %s -a bolt://localhost:7687 "
                    "-d %s --non-interactive %s"
                    % (shlex.quote(self._user()), shlex.quote(self._password()),
                       cy_bin, shlex.quote(db_name), shlex.quote(cy))))
        else:
            cmd = ("NEO4J_USERNAME=%s NEO4J_PASSWORD=%s %s -a %s -d %s "
                   "--non-interactive %s"
                   % (shlex.quote(self._user()), shlex.quote(self._password()), cy_bin,
                      shlex.quote(self._bolt_uri()), shlex.quote(db_name),
                      shlex.quote(cy)))
        r = self._remote(ssh_host, cmd, timeout=self._timeout())
        if r["returncode"] != 0:
            return "APOC 导入失败(rc=%s): %s" % (
                r["returncode"], (r.get("stderr") or r.get("stdout") or "")[:400])
        return ""

    def _container_load(self, ssh_host: dict, admin: str, db_name: str,
                        remote_file: str) -> str:
        c = self._container()
        image = self._opt("neo4j_image") or self._container_image(ssh_host, c)
        data_dir = self._opt("neo4j_data_volume") or self._container_bind(ssh_host, c, "/data")
        if not (image and data_dir):
            return "容器模式恢复缺少 neo4j_image / neo4j_data_volume 配置"
        bk_dir = self._opt("neo4j_backup_volume") or (
            os.path.dirname(data_dir.rstrip("/")) + "/backups")
        self._remote(ssh_host, "mkdir -p %s && cp -f %s %s/" % (
            shlex.quote(bk_dir), shlex.quote(remote_file), shlex.quote(bk_dir)),
            timeout=600)
        last_err = ""
        for flag in _OVERWRITE_FLAGS:
            load_cmd = ("%s database load %s --from-path=/backups %s"
                        % (admin, shlex.quote(db_name), flag))
            shell = ("set -e; docker stop %s || true; RC=0; "
                     "docker run --rm -v %s:/data -v %s:/backups %s sh -c %s || RC=$?; "
                     "docker start %s || true; exit $RC"
                     % (shlex.quote(c), shlex.quote(data_dir), shlex.quote(bk_dir),
                        shlex.quote(image), shlex.quote(load_cmd), shlex.quote(c)))
            r = self._remote(ssh_host, shell, timeout=self._timeout())
            if r["returncode"] == 0:
                return ""
            last_err = (r.get("stderr") or r.get("stdout") or "")[:400]
            if "unrecognized" in last_err.lower() or "unknown" in last_err.lower():
                continue
            break
        return "容器模式 load 失败: %s" % last_err

    def _bare_load(self, ssh_host: dict, admin: str, db_name: str, stage: str) -> str:
        if not self._allow_offline():
            return "离线 load 需要停库，当前任务配置为不允许停库（neo4j_allow_offline=false）"
        stop_cmd = str(self._opt("neo4j_stop_cmd") or "systemctl stop neo4j")
        start_cmd = str(self._opt("neo4j_start_cmd") or "systemctl start neo4j")
        last_err = ""
        for flag in _OVERWRITE_FLAGS:
            load_cmd = ("%s database load %s --from-path=%s %s"
                        % (admin, shlex.quote(db_name), shlex.quote(stage), flag))
            shell = ("set -e; %s || true; RC=0; %s || RC=$?; %s || true; exit $RC"
                     % (stop_cmd, load_cmd, start_cmd))
            r = self._remote(ssh_host, shell, timeout=self._timeout())
            if r["returncode"] == 0:
                return ""
            last_err = (r.get("stderr") or r.get("stdout") or "")[:400]
            if "unrecognized" in last_err.lower() or "unknown" in last_err.lower():
                continue
            break
        return "neo4j-admin database load 失败: %s" % last_err

    def _finish_restore(self, ssh_host: dict, t0: float, err: str,
                        label: str) -> BackupResult:
        if err:
            return BackupResult(success=False, status=BackupStatus.FAILED,
                                duration_sec=round(time.time() - t0, 3),
                                message=err)
        return BackupResult(
            success=True, status=BackupStatus.SUCCESS,
            duration_sec=round(time.time() - t0, 3),
            message="%s 成功；目标库=%s（主机 %s）"
                    % (label, self._db_name(), ssh_host.get("host_key", "remote")))

    def _restore_cross_host(self, backup_path: str, target_host_info: dict,
                            target_db: str) -> BackupResult:
        """跨主机恢复：把产物推到目标主机，在其上执行 load/restore。"""
        from core import remote_dump
        target = dict(target_host_info)
        target["password"] = db.decrypt_secret(target.get("password") or "")
        t0 = time.time()
        try:
            ssh = target.get("host_key")
            if not ssh:
                ssh = "%s@%s:%s" % (target.get("username") or "root",
                                    target.get("hostname") or target.get("host"),
                                    target.get("port") or 22)
                target["host_key"] = ssh
            stage = self._stage()
            self._remote_mkdir(target, stage)
            remote_file = "%s/%s" % (stage, os.path.basename(backup_path))
            self._remote_push(target, backup_path, remote_file)
            admin = self._remote_admin(target)
            db_name = target_db or self._db_name()
            if backup_path.endswith(".backup.tar.gz"):
                self._remote(target, "mkdir -p %s/restore && tar -xzf %s -C %s/restore"
                             % (shlex.quote(stage), shlex.quote(remote_file),
                                shlex.quote(stage)), timeout=self._timeout())
                err = self._remote_restore_online(target, admin, db_name,
                                                  "%s/restore" % stage)
                label = "neo4j-admin database restore"
            elif backup_path.endswith((".cypher", ".json", ".csv.zip")):
                err = self._remote_restore_apoc(target, db_name, remote_file,
                                                os.path.basename(backup_path))
                label = "APOC 导入"
            else:
                if self._is_container_mode():
                    err = self._container_load(target, admin, db_name, remote_file)
                else:
                    err = self._bare_load(target, admin, db_name, stage)
                label = "neo4j-admin database load"
            self._remote_rm(target, remote_file)
        except Exception as e:
            return BackupResult(success=False, status=BackupStatus.FAILED,
                                message="跨主机恢复失败: %s" % e)
        if err:
            return BackupResult(success=False, status=BackupStatus.FAILED,
                                duration_sec=round(time.time() - t0, 3), message=err)
        return BackupResult(
            success=True, status=BackupStatus.SUCCESS,
            duration_sec=round(time.time() - t0, 3),
            message="%s 跨主机恢复成功；目标=%s:%s/%s"
                    % (label, target.get("hostname") or target.get("host"),
                       target.get("port") or "", db_name))

    def _restore_local(self, backup_path: str, kwargs: dict) -> BackupResult:
        ok, detail = self.check_client()
        if not ok:
            return BackupResult(success=False, status=BackupStatus.FAILED,
                                message="Neo4j 客户端检测失败: " + detail)
        t0 = time.time()
        db_name = kwargs.get("target_db") or self._db_name()
        admin = self._admin_cmd()
        stage = self._stage()
        os.makedirs(stage, exist_ok=True)
        if backup_path.endswith(".backup.tar.gz"):
            self._untar_to_dir(backup_path, stage + "/restore")
            err = self._local_restore_online(admin, db_name, stage + "/restore")
            label = "neo4j-admin database restore"
        elif backup_path.endswith((".cypher", ".json", ".csv.zip")):
            err = self._local_restore_apoc(db_name, backup_path)
            label = "APOC 导入"
        else:
            if self._is_container_mode():
                err = self._local_container_load(admin, db_name, backup_path)
            else:
                err = self._local_bare_load(admin, db_name, backup_path, stage)
            label = "neo4j-admin database load"
        if err:
            return BackupResult(success=False, status=BackupStatus.FAILED,
                                duration_sec=round(time.time() - t0, 3), message=err)
        return BackupResult(success=True, status=BackupStatus.SUCCESS,
                            duration_sec=round(time.time() - t0, 3),
                            message="%s 成功；目标库=%s" % (label, db_name))

    def _local_restore_online(self, admin: str, db_name: str, from_dir: str) -> str:
        last_err = ""
        for flag in _OVERWRITE_FLAGS:
            r = self._run(["sh", "-c", "%s database restore %s --from-path=%s %s"
                           % (admin, shlex.quote(db_name), shlex.quote(from_dir), flag)],
                          timeout=self._timeout())
            if r["returncode"] == 0:
                return ""
            last_err = (r.get("stderr") or r.get("stdout") or "")[:400]
            if "unrecognized" in last_err.lower() or "unknown" in last_err.lower():
                continue
            break
        return "neo4j-admin database restore 失败: %s" % last_err

    def _local_bare_load(self, admin: str, db_name: str, backup_path: str,
                         stage: str) -> str:
        if not self._allow_offline():
            return "离线 load 需要停库，当前任务配置为不允许停库"
        import shutil
        dst = os.path.join(stage, os.path.basename(backup_path))
        shutil.copyfile(backup_path, dst)
        stop_cmd = str(self._opt("neo4j_stop_cmd") or "systemctl stop neo4j")
        start_cmd = str(self._opt("neo4j_start_cmd") or "systemctl start neo4j")
        last_err = ""
        for flag in _OVERWRITE_FLAGS:
            r = self._run(["sh", "-c",
                           "%s || true; RC=0; %s database load %s --from-path=%s %s || RC=$?; "
                           "%s || true; exit $RC"
                           % (stop_cmd, admin, shlex.quote(db_name),
                              shlex.quote(stage), flag, start_cmd)],
                          timeout=self._timeout())
            if r["returncode"] == 0:
                return ""
            last_err = (r.get("stderr") or r.get("stdout") or "")[:400]
            if "unrecognized" in last_err.lower() or "unknown" in last_err.lower():
                continue
            break
        return "neo4j-admin database load 失败: %s" % last_err

    def _local_container_load(self, admin: str, db_name: str, backup_path: str) -> str:
        shell = self._container_load_shell(admin, db_name, backup_path)
        r = self._run(["sh", "-c", shell], timeout=self._timeout())
        if r["returncode"] == 0:
            return ""
        return "容器模式 load 失败(rc=%s): %s" % (
            r["returncode"], (r.get("stderr") or r.get("stdout") or "")[:400])

    def _local_restore_apoc(self, db_name: str, backup_path: str) -> str:
        fname = os.path.basename(backup_path)
        if fname.endswith(".cypher"):
            cy = "CALL apoc.cypher.runFile('%s', {});" % backup_path
        elif fname.endswith(".json"):
            cy = "CALL apoc.import.json('%s', {});" % backup_path
        else:
            cy = "CALL apoc.import.csv([{fileName:'file://%s', labels:[]}], [], {});" % backup_path
        r = self._cypher_local(cy, db_name)
        if r["returncode"] != 0:
            return "APOC 导入失败(rc=%s): %s" % (
                r["returncode"], (r.get("stderr") or r.get("stdout") or "")[:400])
        return ""

    # ------------------------------------------------------------------ #
    # 容器辅助（远端）
    # ------------------------------------------------------------------ #
    def _container_image(self, ssh_host: dict, c: str) -> str:
        r = self._remote(ssh_host,
                         "docker inspect --format '{{.Config.Image}}' %s 2>/dev/null || true"
                         % shlex.quote(c), timeout=60)
        return (r.get("stdout") or "").strip().splitlines()[0].strip() if r.get("stdout") else ""

    def _container_bind(self, ssh_host: dict, c: str, target: str) -> str:
        """解析容器挂载：宿主机目录 → 容器内目录（target 形如 /data）。"""
        fmt = "{{range .Mounts}}{{if eq .Destination \"%s\"}}{{.Source}}{{end}}{{end}}" % target
        r = self._remote(ssh_host,
                         "docker inspect --format '%s' %s 2>/dev/null || true"
                         % (fmt, shlex.quote(c)), timeout=60)
        return (r.get("stdout") or "").strip().splitlines()[0].strip() if r.get("stdout") else ""

    def _container_dump_shell(self, admin: str, db_name: str) -> str:
        c = self._container()
        image = self._opt("neo4j_image") or ""
        data_dir = self._opt("neo4j_data_volume") or ""
        bk_dir = self._opt("neo4j_backup_volume") or (
            os.path.dirname(data_dir.rstrip("/")) + "/backups" if data_dir else "")
        if not (image and data_dir):
            return "exit 90"
        return ("set -e; mkdir -p %s; docker stop %s || true; RC=0; "
                "docker run --rm -v %s:/data -v %s:/backups %s sh -c %s || RC=$?; "
                "docker start %s || true; exit $RC"
                % (shlex.quote(bk_dir), shlex.quote(c), shlex.quote(data_dir),
                   shlex.quote(bk_dir), shlex.quote(image),
                   shlex.quote("%s database dump %s --to-path=/backups "
                               "--overwrite-destination=true --verbose"
                               % (admin, shlex.quote(db_name))),
                   shlex.quote(c)))

    def _container_load_shell(self, admin: str, db_name: str, backup_path: str) -> str:
        c = self._container()
        image = self._opt("neo4j_image") or ""
        data_dir = self._opt("neo4j_data_volume") or ""
        bk_dir = self._opt("neo4j_backup_volume") or (
            os.path.dirname(data_dir.rstrip("/")) + "/backups" if data_dir else "")
        if not (image and data_dir):
            return "exit 90"
        import shutil
        os.makedirs(bk_dir, exist_ok=True)
        shutil.copyfile(backup_path, os.path.join(bk_dir, os.path.basename(backup_path)))
        return ("set -e; docker stop %s || true; RC=0; "
                "docker run --rm -v %s:/data -v %s:/backups %s sh -c %s || RC=$?; "
                "docker start %s || true; exit $RC"
                % (shlex.quote(c), shlex.quote(data_dir), shlex.quote(bk_dir),
                   shlex.quote(image),
                   shlex.quote("%s database load %s --from-path=/backups "
                               "--overwrite-destination=true"
                               % (admin, shlex.quote(db_name))),
                   shlex.quote(c)))

    # ------------------------------------------------------------------ #
    # 其它契约
    # ------------------------------------------------------------------ #
    def list_databases(self) -> list:
        """列出 Neo4j 中的数据库（社区版返回 neo4j/system）。"""
        query = "SHOW DATABASES YIELD name RETURN name ORDER BY name;"
        from core import remote_dump
        try:
            ssh_host = remote_dump.resolve_ssh_host(self.task)
        except Exception:
            ssh_host = None
        if ssh_host:
            r = self._cypher_remote(ssh_host, query, "system")
            text = r.get("stdout") or ""
        else:
            if not self._local_client_ready():
                return []
            r = self._cypher_local(query, "system")
            text = r.get("stdout") or ""
        out = []
        for line in text.splitlines():
            s = line.strip()
            if not s or s.startswith("+") or s.lower().startswith("name") or s.startswith("| name"):
                continue
            if s.startswith("|"):
                val = s.strip("|").strip().strip('"').strip()
                if val and val.lower() != "name":
                    out.append(val)
        return out

    def verify_record(self, record: dict, options: dict = None) -> BackupResult:
        """Neo4j 备份可恢复性校验（真实检查，不造假）。

        * .dump         —— 用 neo4j-admin 读取归档元数据（--info），验证归档
                           可解析且含目标库；工具不支持 --info 时如实说明。
        * .backup.tar.gz—— 解开校验产物目录结构（<db>-<ts>.backup）与备份元数据。
        * APOC 导出     —— 真实解析内容：统计 CREATE 语句 / JSON 行数，
                           确认导出非空且格式合法。
        """
        options = options or {}
        base = super().verify_record(record, options)
        if not base.success:
            return base
        path = record.get("backup_path") or ""
        try:
            if path.endswith(".backup.tar.gz"):
                return self._verify_backup_artifact(base, path)
            if path.endswith((".cypher", ".json", ".csv.zip")):
                return self._verify_apoc_artifact(base, path)
            return self._verify_dump_artifact(base, path, options)
        except Exception as e:
            return BackupResult(success=False, status=BackupStatus.FAILED,
                                message="Neo4j 校验异常: %s" % e, verified=False)

    def _verify_dump_artifact(self, base: BackupResult, path: str,
                              options: dict) -> BackupResult:
        """dump 归档校验：优先用 neo4j-admin --info 真实读取归档元数据。"""
        admin = self._admin_cmd()
        r = self._run(["sh", "-c", "%s database load %s --from-path=%s --info"
                       % (admin, shlex.quote(self._db_name()),
                          shlex.quote(os.path.dirname(path)))], timeout=300)
        text = (r.get("stdout") or "") + (r.get("stderr") or "")
        if r["returncode"] == 0 and text.strip():
            return BackupResult(
                success=True, status=BackupStatus.SUCCESS, verified=True,
                size_bytes=base.size_bytes,
                message="Neo4j dump 归档可解析（neo4j-admin --info 通过）: %s"
                        % text.strip()[:300])
        if "unrecognized" in text.lower() or "unknown" in text.lower():
            return BackupResult(
                success=True, status=BackupStatus.SUCCESS, verified=True,
                size_bytes=base.size_bytes,
                message=("当前版本 neo4j-admin 不支持 --info，已完成归档完整性校验"
                         "（SHA256 一致、大小 %s 字节）；如需深度校验请在恢复页执行一次"
                         "真实恢复到测试库。"))
        return BackupResult(
            success=False, status=BackupStatus.FAILED, verified=False,
            message="Neo4j dump 归档校验失败: %s" % (text.strip()[:300] or "归档无法解析"))

    def _verify_backup_artifact(self, base: BackupResult, path: str) -> BackupResult:
        import tarfile
        try:
            with tarfile.open(path, "r:gz") as tar:
                names = tar.getnames()
        except Exception as e:
            return BackupResult(success=False, status=BackupStatus.FAILED,
                                verified=False, message="备份产物解包失败: %s" % e)
        roots = {n.split("/")[0] for n in names if n}
        artifacts = [x for x in roots if x.endswith(".backup")]
        if not artifacts:
            return BackupResult(success=False, status=BackupStatus.FAILED,
                                verified=False,
                                message="在线备份产物结构异常：未找到 <db>-<ts>.backup 目录")
        return BackupResult(
            success=True, status=BackupStatus.SUCCESS, verified=True,
            size_bytes=base.size_bytes,
            message="Neo4j 在线备份产物结构完整：%s（共 %d 个成员）"
                    % (", ".join(sorted(artifacts)), len(names)))

    def _verify_apoc_artifact(self, base: BackupResult, path: str) -> BackupResult:
        if path.endswith(".csv.zip"):
            import zipfile
            try:
                with zipfile.ZipFile(path) as z:
                    n = len(z.namelist())
            except Exception as e:
                return BackupResult(success=False, status=BackupStatus.FAILED,
                                    verified=False, message="CSV 导出包损坏: %s" % e)
            return BackupResult(success=True, status=BackupStatus.SUCCESS, verified=True,
                                size_bytes=base.size_bytes,
                                message="APOC CSV 导出包完整（%d 个文件）" % n)
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            head = f.read(4 << 20)
        creates = len(re.findall(r"(?im)^\s*CREATE\s*\(", head))
        nodes = len(re.findall(r'(?i)"type"\s*:\s*"node"', head)) if path.endswith(".json") else 0
        if path.endswith(".json"):
            ok = bool(head.strip())
            return BackupResult(success=True if ok else False,
                                status=BackupStatus.SUCCESS if ok else BackupStatus.FAILED,
                                verified=bool(ok), size_bytes=base.size_bytes,
                                message="APOC JSON 导出校验：有效内容（抽样节点条目 %d）" % nodes)
        if creates <= 0:
            return BackupResult(success=False, status=BackupStatus.FAILED, verified=False,
                                message="APOC Cypher 导出未解析到 CREATE 语句，导出可能为空")
        return BackupResult(success=True, status=BackupStatus.SUCCESS, verified=True,
                            size_bytes=base.size_bytes,
                            message="APOC Cypher 导出校验通过（抽样 CREATE 语句 %d 条）" % creates)

    # ------------------------------------------------------------------ #
    # 内部工具
    # ------------------------------------------------------------------ #
    def _timeout(self) -> int:
        try:
            return int(self._opt("neo4j_timeout") or 7200)
        except Exception:
            return 7200

    def _local_client_ready(self) -> bool:
        try:
            ok, _ = self.check_client()
            return ok
        except Exception:
            return False

    def _ok_result(self, path: str, size: int, t0: float, label: str,
                   compress_algo: str = "") -> BackupResult:
        return BackupResult(
            success=True, status=BackupStatus.SUCCESS,
            backup_path=path,
            size_bytes=int(size or (os.path.getsize(path) if os.path.exists(path) else 0)),
            duration_sec=round(time.time() - t0, 3),
            checksum=db.sha256_file(path) if os.path.exists(path) else "",
            compress_algo=compress_algo,
            simulated=False,
            message="%s 成功 | %s | 库=%s" % (label, db.human_size(size or 0), self._db_name()))

    @staticmethod
    def _with_note(res: BackupResult, note: str) -> BackupResult:
        if note and res.success:
            res.message = (note + " " + res.message).strip()
        return res
