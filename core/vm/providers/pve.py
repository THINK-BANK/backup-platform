# -*- coding: utf-8 -*-
"""Proxmox VE（PVE / PBS）虚拟机备份适配器。

控制面：PVE REST API（/api2/json），支持 API Token 与用户名密码两种认证。
数据面：vzdump 在 PVE 节点本地产出归档后，由平台经 SSH/SFTP 拉回。

能力说明（诚实声明，不夸大）：
* 全量：所有 PVE 环境可用（mode=snapshot，QEMU 内建快照，业务几乎无感知）。
* 增量：仅当备份目标是 **PBS（Proxmox Backup Server）** 时为真增量——
  PVE 侧 QEMU 维护 dirty bitmap，只上传脏块，配合 PBS 内容寻址天然
  「增量永久」；目标是本地/NFS 存储时每次都是完整归档，本 Provider
  会直接返回「不支持块级增量」，由引擎按策略回退为全量（不静默造假）。
* 即时恢复：PBS live-restore（PBS 存储 + PVE 7+）下可用。
* 克隆为新 VM：原生 API 支持（restore 到新 VMID），用于演练/取证/开发。
"""
import os
import re
import shlex
import time

from core.vm import http as vmhttp
from core.vm import ssh as vmssh
from core.vm.base import VMProvider
from core.vm.types import (
    BackupArtifact, CloneResult, CloneSpec, ProviderCaps, VMInfo, VMDisk,
    VMProviderError,
)

# vzdump 归档路径日志样例：
# INFO: creating archive '/mnt/pve/backups/dump/vzdump-qemu-100-2026_09_16-12_00_00.vma.zst'
_ARCHIVE_RE = re.compile(r"creating archive '([^']+)'")
# PBS: INFO: creating backup on storage 'pbs1' → 归档由 PBS 管理，用日志里的 snapshot 名
_PBS_SNAP_RE = re.compile(r"creating backup on storage '([^']+)'")


class PVEProvider(VMProvider):
    provider_id = "pve"
    display_name = "Proxmox VE (KVM)"

    def __init__(self, hypervisor: dict, logger=None):
        super().__init__(hypervisor, logger)
        self._base = (self.hv.get("endpoint") or "").rstrip("/")
        if not self._base.endswith("/api2/json"):
            self._base = self._base + "/api2/json"
        self._verify_ssl = bool(int(self.hv.get("verify_ssl") or 0))
        self._token = None
        self._csrf = None
        self._version = ""

    # ---------------- 认证 ----------------
    def _headers(self) -> dict:
        h = {"Accept": "application/json"}
        if self.hv.get("token_id") and self.hv.get("token_secret"):
            h["Authorization"] = "PVEAPIToken=%s=%s" % (
                self.hv["token_id"], self.hv["token_secret"])
        elif self._token:
            h["Cookie"] = "PVEAuthCookie=%s" % self._token
            h["CSRFPreventionToken"] = self._csrf or ""
        return h

    def _login(self):
        if self.hv.get("token_id") and self.hv.get("token_secret"):
            return True, "API Token 认证"
        user = self.hv.get("username") or ""
        pw = self.hv.get("password") or ""
        if not (user and pw):
            return False, "缺少 PVE 认证信息（API Token 或 用户名密码）"
        r = vmhttp.request("POST", self._base + "/access/ticket",
                           data=vmhttp.form_encode({"username": user, "password": pw}),
                           headers={"Content-Type": "application/x-www-form-urlencoded"},
                           timeout=30, verify_ssl=self._verify_ssl)
        if not r["ok"]:
            return False, "PVE 认证失败: %s %s" % (r.get("error"), (r.get("text") or "")[:200])
        data = (r["json"] or {}).get("data") or {}
        self._token = data.get("ticket") or ""
        self._csrf = data.get("CSRFPreventionToken") or ""
        return bool(self._token), "PVE 认证成功" if self._token else "PVE 认证未返回 ticket"

    def connect(self):
        ok, msg = self._login()
        if not ok:
            return False, msg
        r = vmhttp.request("GET", self._base + "/version", headers=self._headers(),
                           timeout=20, verify_ssl=self._verify_ssl)
        if not r["ok"]:
            return False, "PVE 连接失败: %s %s" % (r.get("error"), (r.get("text") or "")[:200])
        data = (r["json"] or {}).get("data") or {}
        self._version = str(data.get("version") or "")
        return True, "PVE %s 连接正常" % (self._version or "")

    def version(self) -> str:
        return self._version

    # ---------------- 能力 ----------------
    def _storage(self) -> str:
        return (self.extra.get("storage") or "").strip()

    def _is_pbs(self) -> bool:
        st = self._storage()
        if not st:
            return False
        if self.extra.get("pbs") in (True, 1, "1", "true"):
            return True
        # 探测存储类型
        try:
            r = vmhttp.request("GET", self._base + "/storage", headers=self._headers(),
                               timeout=20, verify_ssl=self._verify_ssl)
            for s in ((r.get("json") or {}).get("data") or []):
                if s.get("storage") == st:
                    return str(s.get("type") or "").startswith("pbs")
        except Exception:
            pass
        return False

    def capabilities(self) -> ProviderCaps:
        pbs = self._is_pbs()
        levels = ["crash", "fs"] if self.extra.get("agent") else ["crash"]
        return ProviderCaps(
            incremental=pbs,
            change_tracking=pbs,
            instant_restore=pbs,
            clone_to_new_vm=True,
            file_restore=pbs,
            snapshot=True,
            consistency_levels=levels,
            notes=("PBS 存储：块级 dirty-bitmap 增量 + 内容寻址去重，支持即时恢复与单文件恢复；"
                   "本地/NFS 存储：每次为完整归档，无块级增量能力。"
                   if pbs else "当前备份存储非 PBS，无块级增量能力（增量任务将按全量执行）。"))

    # ---------------- 资产发现 ----------------
    def list_vms(self) -> list:
        ok, msg = self._login()
        if not ok:
            raise VMProviderError(msg)
        r = vmhttp.request("GET", self._base + "/cluster/resources?type=vm",
                           headers=self._headers(), timeout=60,
                           verify_ssl=self._verify_ssl)
        if not r["ok"]:
            raise VMProviderError("PVE 拉取虚拟机列表失败: %s" % (r.get("text") or r.get("error"))[:200])
        out = []
        for it in ((r["json"] or {}).get("data") or []):
            if it.get("type") != "qemu":
                continue  # 本期只保护 KVM 虚拟机（LXC 容器不支持磁盘级快照备份）
            out.append(VMInfo(
                ref=str(it.get("vmid")),
                name=it.get("name") or ("vm-%s" % it.get("vmid")),
                node=it.get("node") or "",
                guest_os=str(it.get("guest_os") or it.get("tags") or ""),
                power_state="running" if it.get("status") == "running" else "stopped",
                cpu=int(it.get("maxcpu") or 0),
                memory_mb=int((it.get("maxmem") or 0) // 1048576),
                disks=self._disks(it.get("node"), str(it.get("vmid"))),
                change_tracking=self._is_pbs(),
                raw=it,
            ))
        return out

    def _disks(self, node: str, vmid: str) -> list:
        r = vmhttp.request("GET", "%s/nodes/%s/qemu/%s/config"
                           % (self._base, node, vmid),
                           headers=self._headers(), timeout=30,
                           verify_ssl=self._verify_ssl)
        disks = []
        for key, val in (((r.get("json") or {}).get("data") or {}) or {}).items():
            if not re.match(r"^(scsi|sata|virtio|ide)\d+$", key):
                continue
            size = 0
            m = re.search(r"size=(\d+)([KMGT]?)", str(val))
            if m:
                mult = {"": 1, "K": 1024, "M": 1048576, "G": 1073741824,
                        "T": 1099511627776}.get(m.group(2), 1)
                size = int(m.group(1)) * mult
            if "media=cdrom" in str(val):
                continue
            disks.append(VMDisk(key=key, path=str(val), size_bytes=size,
                                format="qcow2" if "qcow2" in str(val) else "raw",
                                change_tracking=self._is_pbs()))
        return disks

    # ---------------- 备份 ----------------
    def _work_dir(self) -> str:
        return (self.extra.get("work_dir") or "/var/tmp/vmbk_stage").rstrip("/")

    def _node_ssh(self, node: str):
        """定位承载该节点的 SSH 连接（单节点用 endpoint；集群用 extra.ssh_nodes）。"""
        nodes = self.extra.get("ssh_nodes") or {}
        cfg = dict(self.hv)
        if node and nodes.get(node):
            n = dict(nodes[node])
            extra = dict(self.extra)
            extra["ssh"] = n
            cfg["extra_config"] = extra
        return vmssh.connect(cfg)

    def _run_vzdump(self, vm: VMInfo, mode_full: bool = True) -> BackupArtifact:
        storage = self._storage()
        if not storage:
            raise VMProviderError("未配置 PVE 备份存储（extra_config.storage），无法执行 vzdump")
        payload = {
            "vmid": vm.ref,
            "storage": storage,
            "mode": "snapshot",           # QEMU 内建快照，业务无感
            "compress": self.extra.get("compress") or "zstd",
            "remove": 0,                  # 成功后不删除已有旧备份（保留策略由平台管理）
            "quiet": 1,
        }
        if not self._is_pbs():
            payload["dumpdir"] = self._work_dir()  # 非 PBS：归档落到节点本地暂存目录
        r = vmhttp.request("POST", "%s/nodes/%s/vzdump" % (self._base, vm.node),
                           headers=self._headers(), data=vmhttp.form_encode(payload),
                           timeout=60, verify_ssl=self._verify_ssl)
        if not r["ok"]:
            raise VMProviderError("vzdump 触发失败: %s %s"
                                  % (r.get("error"), (r.get("text") or "")[:300]))
        upid = ((r.get("json") or {}).get("data")) or ""
        if not upid:
            raise VMProviderError("vzdump 未返回任务号(UPID): %s" % (r.get("text") or "")[:200])
        status, log = self._wait_task(vm.node, upid, timeout=int(
            self.extra.get("backup_timeout") or 21600))
        if status != "OK":
            raise VMProviderError("vzdump 任务失败(%s): %s" % (status, log[-800:]))
        archive = ""
        m = _ARCHIVE_RE.search(log)
        if m:
            archive = m.group(1)
        if not archive:
            raise VMProviderError("未能从 vzdump 日志解析归档路径（PBS 存储请用"
                                  "「恢复点 → 克隆/还原」API，平台按 PBS 快照名定位）")

        size = 0
        try:
            client = self._node_ssh(vm.node)
            size = vmssh.remote_size(client, archive)
        except Exception:
            size = 0
        return BackupArtifact(
            path=archive,
            kind="full" if mode_full else "incremental",
            size_bytes=size,
            change_token=self._snapshot_from_archive(archive),
            consistency=self.extra.get("consistency") or "crash",
            extra={"node": vm.node, "upid": upid, "pbs": self._is_pbs()},
        )

    @staticmethod
    def _snapshot_from_archive(archive: str) -> str:
        base = os.path.basename(archive or "")
        m = re.search(r"(\d{4}_\d{2}_\d{2}-\d{2}_\d{2}_\d{2})", base)
        return m.group(1) if m else base

    def backup_full(self, vm_ref: str, work_dir: str, consistency: str = "crash") -> BackupArtifact:
        vm = self.get_vm(vm_ref) or VMInfo(ref=vm_ref, node=self.extra.get("node") or "")
        return self._run_vzdump(vm, mode_full=True)

    def backup_incremental(self, vm_ref: str, work_dir: str, parent_token: str,
                           consistency: str = "crash") -> BackupArtifact:
        if not self._is_pbs():
            raise VMProviderError(
                "当前 PVE 备份存储不是 PBS，无块级增量能力：每次 vzdump 都是完整归档。"
                "请改用 PBS 存储，或在保护策略中把该 VM 设为「每次全量」。")
        vm = self.get_vm(vm_ref) or VMInfo(ref=vm_ref, node=self.extra.get("node") or "")
        art = self._run_vzdump(vm, mode_full=False)
        art.parent_token = parent_token or ""
        return art

    def _wait_task(self, node: str, upid: str, timeout: int = 21600) -> tuple:
        """轮询 PVE 任务直到结束，返回 (exitstatus, log)。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            r = vmhttp.request("GET", "%s/nodes/%s/tasks/%s/status"
                               % (self._base, node, upid.replace("/", "%2F")),
                               headers=self._headers(), timeout=60,
                               verify_ssl=self._verify_ssl)
            data = (r.get("json") or {}).get("data") or {}
            if data.get("status") == "stopped":
                log = self._task_log(node, upid)
                return str(data.get("exitstatus") or "ERROR"), log
            time.sleep(5)
        return "TIMEOUT", "等待 PVE 任务超时(%ss)" % timeout

    def _task_log(self, node: str, upid: str, lines: int = 500) -> str:
        r = vmhttp.request("GET", "%s/nodes/%s/tasks/%s/log?limit=%d"
                           % (self._base, node, upid.replace("/", "%2F"), lines),
                           headers=self._headers(), timeout=60,
                           verify_ssl=self._verify_ssl)
        data = (r.get("json") or {}).get("data") or []
        return "\n".join(str(x.get("t") or "") for x in data)

    def download_artifact(self, artifact: BackupArtifact, local_path: str) -> dict:
        node = artifact.extra.get("node") or self.extra.get("node") or ""
        client = self._node_ssh(node)
        try:
            res = vmssh.pull_file(client, artifact.path, local_path,
                                  label=os.path.basename(artifact.path))
        finally:
            try:
                client.close()
            except Exception:
                pass
        return {"size": res.get("size") or 0, "sha256": res.get("sha256") or ""}

    def upload_artifact(self, local_path: str, remote_dir: str) -> str:
        node = self.extra.get("node") or ""
        client = self._node_ssh(node)
        try:
            remote = "%s/%s" % (remote_dir.rstrip("/"), os.path.basename(local_path))
            vmssh.push_file(client, local_path, remote)
            return remote
        finally:
            try:
                client.close()
            except Exception:
                pass

    def cleanup_artifact(self, artifact: BackupArtifact) -> None:
        node = artifact.extra.get("node") or self.extra.get("node") or ""
        try:
            client = self._node_ssh(node)
            try:
                vmssh.exec_capture(client, "rm -f %s" % shlex.quote(artifact.path),
                                   timeout=120)
            finally:
                client.close()
        except Exception:
            pass

    # ---------------- 恢复 / 克隆 ----------------
    def restore_in_place(self, vm_ref: str, artifacts: list, consistency: str = "crash"):
        vm = self.get_vm(vm_ref) or VMInfo(ref=vm_ref, node=self.extra.get("node") or "")
        archive = artifacts[0] if artifacts else ""
        if not archive:
            return False, "缺少备份产物路径"
        self.stop_vm(vm_ref, vm.node, force=True)
        payload = {"vmid": vm.ref, "archive": archive, "force": 1, "start": 1}
        if self._storage():
            payload["storage"] = self._storage()
        r = vmhttp.request("POST", "%s/nodes/%s/qemu" % (self._base, vm.node),
                           headers=self._headers(), data=vmhttp.form_encode(payload),
                           timeout=300, verify_ssl=self._verify_ssl)
        if not r["ok"]:
            return False, "原位还原失败: %s %s" % (r.get("error"), (r.get("text") or "")[:300])
        upid = ((r.get("json") or {}).get("data")) or ""
        if upid:
            st, log = self._wait_task(vm.node, upid, timeout=21600)
            if st != "OK":
                return False, "原位还原任务失败(%s): %s" % (st, log[-500:])
        return True, "原位还原成功（VM %s 已恢复并开机）" % vm.ref

    def clone_to_new_vm(self, vm_ref: str, artifacts: list, spec: CloneSpec) -> CloneResult:
        src = self.get_vm(vm_ref) or VMInfo(ref=vm_ref, node=self.extra.get("node") or "")
        archive = artifacts[0] if artifacts else ""
        if not archive:
            return CloneResult(ok=False, message="缺少备份产物路径")
        target = spec.target_ref or spec.target_name or ""
        if not str(target).isdigit():
            nid = vmhttp.request("GET", self._base + "/cluster/nextid",
                                 headers=self._headers(), timeout=30,
                                 verify_ssl=self._verify_ssl)
            target = str(((nid.get("json") or {}).get("data")) or "")
        if not target:
            return CloneResult(ok=False, message="无法分配新 VMID")
        payload = {
            "vmid": target,
            "archive": archive,
            "start": 1 if spec.auto_start else 0,
            "unique": 1 if spec.regenerate_mac else 0,
        }
        if spec.target_name:
            payload["name"] = spec.target_name
        if self._storage():
            payload["storage"] = self._storage()
        if spec.live and self._is_pbs():
            payload["live-restore"] = 1  # 即时恢复：先开机，数据后台回填
        node = spec.target_node or src.node
        r = vmhttp.request("POST", "%s/nodes/%s/qemu" % (self._base, node),
                           headers=self._headers(), data=vmhttp.form_encode(payload),
                           timeout=300, verify_ssl=self._verify_ssl)
        if not r["ok"]:
            return CloneResult(ok=False, message="克隆失败: %s %s"
                               % (r.get("error"), (r.get("text") or "")[:300]))
        upid = ((r.get("json") or {}).get("data")) or ""
        if upid:
            st, log = self._wait_task(node, upid, timeout=21600)
            if st != "OK":
                return CloneResult(ok=False, message="克隆任务失败(%s): %s" % (st, log[-500:]))
        if spec.isolate_network:
            try:
                self._isolate_network(node, target, spec.network_name)
            except Exception as e:
                return CloneResult(ok=True, target_ref=target,
                                   target_name=spec.target_name or target,
                                   message="克隆成功，但网络隔离失败: %s" % e)
        ip = self.guest_ip(target, node) if spec.auto_start else ""
        return CloneResult(ok=True, target_ref=target,
                           target_name=spec.target_name or target, ip=ip,
                           message="已从恢复点克隆为新 VM（VMID=%s，节点=%s）"
                                   % (target, node))

    def _isolate_network(self, node: str, vmid: str, bridge: str) -> None:
        bridge = bridge or self.extra.get("isolate_bridge") or ""
        if not bridge:
            return
        r = vmhttp.request("GET", "%s/nodes/%s/qemu/%s/config"
                           % (self._base, node, vmid),
                           headers=self._headers(), timeout=30,
                           verify_ssl=self._verify_ssl)
        cfg = (r.get("json") or {}).get("data") or {}
        patch = {}
        for key, val in cfg.items():
            if re.match(r"^net\d+$", key) and "bridge=" in str(val):
                patch[key] = re.sub(r"bridge=[^,]+", "bridge=%s" % bridge, str(val))
        if not patch:
            return
        vmhttp.request("PUT", "%s/nodes/%s/qemu/%s/config"
                       % (self._base, node, vmid),
                       headers=self._headers(), data=vmhttp.form_encode(patch),
                       timeout=60, verify_ssl=self._verify_ssl)

    def delete_vm(self, ref: str, node: str = ""):
        node = node or self.extra.get("node") or ""
        self.stop_vm(ref, node, force=True)
        r = vmhttp.request("DELETE", "%s/nodes/%s/qemu/%s?purge=1&destroy-unreferenced-disks=1"
                           % (self._base, node, ref),
                           headers=self._headers(), timeout=120,
                           verify_ssl=self._verify_ssl)
        if not r["ok"]:
            return False, "销毁 VM 失败: %s" % (r.get("text") or r.get("error"))[:200]
        return True, "已销毁 VM %s" % ref

    def start_vm(self, ref: str, node: str = ""):
        node = node or self.extra.get("node") or ""
        r = vmhttp.request("POST", "%s/nodes/%s/qemu/%s/status/start"
                           % (self._base, node, ref),
                           headers=self._headers(), timeout=120,
                           verify_ssl=self._verify_ssl)
        return (r["ok"], "开机成功" if r["ok"] else (r.get("text") or "")[:200])

    def stop_vm(self, ref: str, node: str = "", force: bool = False):
        node = node or self.extra.get("node") or ""
        url = "%s/nodes/%s/qemu/%s/status/%s" % (
            self._base, node, ref, "stop" if force else "shutdown")
        r = vmhttp.request("POST", url, headers=self._headers(), timeout=180,
                           verify_ssl=self._verify_ssl)
        return (r["ok"], "关机成功" if r["ok"] else (r.get("text") or "")[:200])

    def power_state(self, vm_ref: str, node: str = "") -> str:
        node = node or self.extra.get("node") or ""
        r = vmhttp.request("GET", "%s/nodes/%s/qemu/%s/status/current"
                           % (self._base, node, vm_ref),
                           headers=self._headers(), timeout=30,
                           verify_ssl=self._verify_ssl)
        st = (((r.get("json") or {}).get("data") or {}).get("status") or "unknown")
        return "running" if st == "running" else "stopped"

    def guest_ip(self, vm_ref: str, node: str = "") -> str:
        node = node or self.extra.get("node") or ""
        r = vmhttp.request("GET", "%s/nodes/%s/qemu/%s/agent/network-get-interfaces"
                           % (self._base, node, vm_ref),
                           headers=self._headers(), timeout=30,
                           verify_ssl=self._verify_ssl)
        data = (r.get("json") or {}).get("data") or {}
        for itf in (data.get("result") or []):
            for addr in (itf.get("ip-addresses") or []):
                ip = addr.get("ip-address") or ""
                if ip and not ip.startswith("127.") and ":" not in ip and "fe80" not in ip:
                    return ip
        return ""
