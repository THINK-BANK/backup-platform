# -*- coding: utf-8 -*-
"""全实例逻辑备份/恢复 —— 本机实现（PG 系 / MySQL 系通用）。

与 core/remote_dump.py 的远端（SSH）实现语义一致：
- 全实例备份 = 枚举库（默认排除系统库）→ 逐库 dump（每库一个文件，
  各自一致性快照）+ 全局对象（PG 系 dumpall -g）→ tar.gz + manifest.json；
  pg_dump/sys_dump 不存在 --all-databases；MySQL 虽有该参数，但为统一
  「逐库文件 + 可排除系统库」语义，同样走逐库打包。
- 全实例恢复 = 解包 → 全局对象 → 缺失库自动 CREATE → 逐库恢复。

产物格式（manifest.json）：
  {"format": "multi-db-tar", "db_type": "...", "generated_at": "...",
   "globals": "yes|no|failed|na", "include_system_dbs": false,
   "databases": ["db1", "db2"]}

系统库清单（默认排除，include_system_dbs=true 时包含）见 SYSTEM_DBS。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import time

# PG 协议系（逐库 dump + globals 语义一致），新增同源库时加入此元组即可
PG_FAMILY = ("postgresql", "kingbase", "opengauss")

# 各库类型的系统库清单（全实例默认排除，业务库优先）
SYSTEM_DBS = {
    "mysql": ("information_schema", "performance_schema", "mysql", "sys"),
    "mariadb": ("information_schema", "performance_schema", "mysql", "sys"),
    "postgresql": ("postgres", "template0", "template1"),
    "kingbase": ("template0", "template1", "template2", "security", "test"),
    # openGauss 默认库：postgres（维护库）+ 两个模板库
    "opengauss": ("postgres", "template0", "template1"),
}

# 各库类型差异点
TOOLING = {
    "postgresql": {
        "catalog_sqls": (
            "SELECT datname FROM pg_database WHERE NOT datistemplate ORDER BY 1",
        ),
        "maint_candidates": ("postgres", "template1"),
        "env_exports": ("PGPASSWORD",),
        "default_query": "psql",
        "default_dumpall": "pg_dumpall",
    },
    "kingbase": {
        # V8/V009R003=sys_database、V9R1=pg_database —— 运行时候选探测
        "catalog_sqls": (
            "SELECT datname FROM sys_database WHERE NOT datistemplate ORDER BY 1",
            "SELECT datname FROM pg_database WHERE NOT datistemplate ORDER BY 1",
        ),
        "maint_candidates": ("test", "postgres", "security", "template1"),
        "env_exports": ("KINGBASE_PASSWORD", "PGPASSWORD"),
        "default_query": "ksql",
        "default_dumpall": "sys_dumpall",
    },
    # openGauss：gsql/gs_dump/gs_dumpall（工具随 GAUSSHOME/bin 提供）
    "opengauss": {
        "catalog_sqls": (
            "SELECT datname FROM pg_database WHERE NOT datistemplate ORDER BY 1",
        ),
        "maint_candidates": ("postgres", "template1"),
        "env_exports": ("PGPASSWORD",),
        "default_query": "gsql",
        "default_dumpall": "gs_dumpall",
    },
    "mysql": {
        "catalog_sqls": ("SHOW DATABASES",),
        "maint_candidates": ("mysql",),
        "env_exports": ("MYSQL_PWD",),
        "default_query": "mysql",
    },
    "mariadb": {
        "catalog_sqls": ("SHOW DATABASES",),
        "maint_candidates": ("mysql",),
        "env_exports": ("MYSQL_PWD",),
        "default_query": "mysql",
    },
}


def _build_env(db_type: str, password: str) -> dict:
    env = os.environ.copy()
    if password:
        for name in TOOLING[db_type]["env_exports"]:
            env[name] = password
    return env


def _run(cmd: list, env: dict, timeout: int = 3600, stdin_file=None):
    kwargs = {"capture_output": True, "text": True, "timeout": timeout, "env": env}
    if stdin_file:
        with open(stdin_file, "rb") as f:
            p = subprocess.run(cmd, stdin=f, **kwargs)
    else:
        p = subprocess.run(cmd, **kwargs)
    return p.returncode, p.stdout, p.stderr


def _which_any(*names: str) -> str:
    for n in names:
        p = shutil.which(n)
        if p:
            return p
    return ""


def enumerate_databases(db_type: str, query_tool: str, host: str, port,
                        user: str, env: dict, include_system_dbs: bool = False):
    """枚举库清单，默认过滤系统库。返回 (命中的维护库, 库名列表, 最近错误)。

    PG 系用 SQL 目录表查询（需先连到某个维护库）；MySQL 系 SHOW DATABASES
    无需维护库。全部候选失败返回 ("", [], 最后一条 stderr)——把 stderr 带回去，
    是为了让"连不上/认证失败"这类真实原因能出现在用户看到的失败消息里。
    """
    cfg = TOOLING[db_type]
    is_pg = db_type in PG_FAMILY
    sys_set = set() if include_system_dbs else set(SYSTEM_DBS.get(db_type) or ())
    last_err = ""
    for mdb in cfg["maint_candidates"]:
        for catalog_sql in cfg["catalog_sqls"]:
            if is_pg:
                cmd = [query_tool, "-h", str(host), "-p", str(port), "-U", user,
                       "-d", mdb, "-t", "-A", "-c", catalog_sql]
            else:
                # 注意 MySQL 客户端 -P(大写)=端口、-p(小写)=密码
                # --no-defaults 必须在首位：否则平台机的 /root/.my.cnf 会**覆盖**
                # 任务里配置的用户与密码（MYSQL_PWD 环境变量优先级低于配置文件），
                # 表现为密码填错也能"备份成功"，实际连的是 my.cnf 里的实例/账号。
                cmd = [query_tool, "--no-defaults", "-h", str(host), "-P", str(port),
                       "-u", user, "-N", "-B", "-e", catalog_sql]
            rc, out, err = _run(cmd, env)
            if rc == 0 and out.strip():
                dbs = [ln.strip().split("\t")[0] for ln in out.splitlines() if ln.strip()]
                dbs = [d for d in dbs if d and d not in sys_set]
                return mdb, dbs, ""
            if not last_err and err and err.strip():
                last_err = err.strip()
    return "", [], last_err


def _mysqldump_to_file(cmd: list, env: dict, fpath: str):
    """mysqldump 输出**流式**落盘（stdout 直连文件，二进制保真）。

    绝不能把产物读进内存：全实例/大库的 dump 动辄数 GB，而平台常与数据库
    部署在同一台机器上（内存被 DB 占用），一旦用 ``capture_output=True`` 整体
    缓存，Python 会抛 ``MemoryError``——它恰好 **``str()`` 为空字符串**，最终
    上报成「MySQL 全实例备份失败: 」这种看不到任何原因的消息。用户侧表现就是
    「明明备份跑了 5 分多钟，最后突然失败」，极难排查。

    改为 stdout 直连文件后，本机备份内存占用恒定为管道缓冲区大小（与产物大小无关）。
    """
    with open(fpath, "wb") as f:
        p = subprocess.run(cmd, stdout=f, stderr=subprocess.PIPE,
                           timeout=7200, env=env)
    err = (p.stderr or b"").decode("utf-8", "replace")[:300]
    return p.returncode, "", err


def backup_full_instance(db_type: str, *, host, port, user, password,
                         dump_tool: str, out_path: str,
                         query_tool: str = "", dumpall_tool: str = "",
                         include_system_dbs: bool = False) -> dict:
    """本机全实例备份：逐库一个文件 + globals（PG 系）→ tar.gz。返回 manifest。"""
    cfg = TOOLING[db_type]
    env = _build_env(db_type, password)
    is_pg = db_type in PG_FAMILY
    query_tool = query_tool or _which_any(cfg["default_query"])
    if not query_tool:
        raise RuntimeError(
            f"本机未找到 SQL 客户端（{cfg['default_query']}），无法枚举数据库执行全实例备份")
    if is_pg:
        dumpall_tool = dumpall_tool or _which_any(cfg["default_dumpall"])

    maint, dbs, enum_err = enumerate_databases(
        db_type, query_tool, host, port, user, env, include_system_dbs)
    if not is_pg:
        # mysqldump 无法导出虚拟库（--all-databases 同样跳过），勾选包含也排除
        dbs = [d for d in dbs
               if d not in ("information_schema", "performance_schema")]
    if not dbs:
        if enum_err:
            # 连不上/认证失败才是真原因，比"没有可备份的库"有用得多
            hint = (f"连接或认证失败: {enum_err[:200]}。"
                    f"请核对任务里配置的地址、端口、用户与密码")
        elif not include_system_dbs:
            hint = "排除系统库后没有可备份的库，可勾选「包含系统库」"
        else:
            hint = (f"候选维护库: {', '.join(cfg['maint_candidates'])}，"
                    f"请检查连接信息")
        raise RuntimeError(f"无法在 {host}:{port} 枚举到可备份的数据库——{hint}")

    work = tempfile.mkdtemp(prefix="bp_fullinst_")
    try:
        dbs_dir = os.path.join(work, "dbs")
        os.makedirs(dbs_dir)
        for d in dbs:
            if is_pg:
                rc, _o, err = _run([
                    dump_tool, "-h", str(host), "-p", str(port), "-U", user,
                    "-Fc", "-f", os.path.join(dbs_dir, f"{d}.dump"), d,
                ], env, timeout=7200)
            else:
                # MySQL 系：逐库 .sql（--databases 保证含 CREATE DATABASE/USE）
                # --no-defaults 见 enumerate_databases 处说明：必须用任务配置的凭据
                cmd = [dump_tool, "--no-defaults", "-h", str(host), "-P", str(port),
                       "-u", user, "--single-transaction", "--routines",
                       "--triggers", "--events", "--default-character-set=utf8mb4",
                       "--databases", d]
                rc, _o, err = _mysqldump_to_file(
                    cmd, env, os.path.join(dbs_dir, f"{d}.sql"))
            if rc != 0:
                raise RuntimeError(f"库 {d} dump 失败(rc={rc}): {err[:300]}")

        globals_status = "na"
        globals_path = ""
        if is_pg:
            if dumpall_tool:
                rc, out, _err = _run([
                    dumpall_tool, "-h", str(host), "-p", str(port), "-U", user, "-g",
                ], env, timeout=1800)
                if rc == 0 and out.strip():
                    globals_path = os.path.join(work, "globals.sql")
                    with open(globals_path, "w", encoding="utf-8") as f:
                        f.write(out)
                    globals_status = "yes"
                else:
                    globals_status = "failed"
            else:
                globals_status = "no"

        manifest = {
            "format": "multi-db-tar",
            "db_type": db_type,
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "globals": globals_status,
            "include_system_dbs": bool(include_system_dbs),
            "databases": dbs,
        }
        with open(os.path.join(work, "manifest.json"), "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)

        with tarfile.open(out_path, "w:gz") as tf:
            tf.add(os.path.join(work, "manifest.json"), arcname="manifest.json")
            tf.add(dbs_dir, arcname="dbs")
            if globals_path and os.path.exists(globals_path):
                tf.add(globals_path, arcname="globals.sql")
        return manifest
    finally:
        shutil.rmtree(work, ignore_errors=True)


def restore_full_instance(db_type: str, *, host, port, user, password,
                          backup_path: str, restore_tool: str,
                          query_tool: str = "") -> dict:
    """本机全实例恢复：解包 → globals（PG 系）→ 缺失库自动建库 → 逐库恢复。

    返回 {"restored": [库名...], "globals": bool}；任一库恢复失败即抛异常。
    """
    cfg = TOOLING[db_type]
    env = _build_env(db_type, password)
    is_pg = db_type in PG_FAMILY
    query_tool = query_tool or _which_any(cfg["default_query"])
    if not query_tool:
        raise RuntimeError(
            f"本机未找到 SQL 客户端（{cfg['default_query']}），无法执行全实例恢复")

    work = tempfile.mkdtemp(prefix="bp_restore_")
    try:
        with tarfile.open(backup_path, "r:gz") as tf:
            tf.extractall(work)
        mpath = os.path.join(work, "manifest.json")
        if not os.path.exists(mpath):
            raise RuntimeError("tar 包内缺少 manifest.json，不是全实例备份产物")
        with open(mpath, encoding="utf-8") as f:
            manifest = json.load(f)
        dbs = manifest.get("databases") or []

        # 恢复端枚举不做系统库过滤（include_system_dbs 备份可能含系统库）
        maint, existing, _enum_err = enumerate_databases(
            db_type, query_tool, host, port, user, env, include_system_dbs=True)

        globals_ok = False
        gpath = os.path.join(work, "globals.sql")
        if is_pg:
            if not maint:
                raise RuntimeError(
                    f"无法连接 {host}:{port}（尝试维护库: "
                    f"{', '.join(cfg['maint_candidates'])}），请检查目标实例连接信息")
            if os.path.exists(gpath) and os.path.getsize(gpath) > 0:
                rc, _o, _e = _run([
                    query_tool, "-h", str(host), "-p", str(port), "-U", user,
                    "-d", maint, "-f", gpath,
                ], env, timeout=1800)
                globals_ok = rc == 0  # 失败多为对象已存在，不阻塞逐库恢复
            # pg_restore/sys_restore 低版本可能无 --if-exists，探测后再用
            rc, out, _e = _run([restore_tool, "--help"], env)
            ifex = ["--if-exists"] if "--if-exists" in (out or "") else []

        restored = []
        for d in dbs:
            if is_pg:
                if d not in existing:
                    _run([
                        query_tool, "-h", str(host), "-p", str(port), "-U", user,
                        "-d", maint, "-c", f'CREATE DATABASE "{d}"',
                    ], env)  # 已存在/权限不足等忽略，交由 restore 报真实错误
                rc, _o, err = _run([
                    restore_tool, "-h", str(host), "-p", str(port), "-U", user,
                    "--dbname", d, "--clean", *ifex,
                    os.path.join(work, "dbs", f"{d}.dump"),
                ], env, timeout=7200)
            else:
                # MySQL 系：dump 内含 CREATE DATABASE/USE，直接灌入即可
                src = os.path.join(work, "dbs", f"{d}.sql")
                if not os.path.exists(src):
                    src = os.path.join(work, "dbs", f"{d}.dump")
                rc, _o, err = _run([
                    query_tool, "--no-defaults", "-h", str(host), "-P", str(port),
                    "-u", user,
                ], env, timeout=7200, stdin_file=src)
            if rc != 0:
                raise RuntimeError(f"库 {d} 恢复失败(rc={rc}): {err[:300]}")
            restored.append(d)
        return {"restored": restored, "globals": globals_ok,
                "declared": dbs}
    finally:
        shutil.rmtree(work, ignore_errors=True)
