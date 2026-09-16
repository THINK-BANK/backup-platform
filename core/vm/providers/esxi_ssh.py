# -*- coding: utf-8 -*-
"""VMware ESXi 虚拟机备份适配器（SSH + vim-cmd，零 agent、无需 vCenter 授权）。

为什么不走 VDDK / vStorage API
------------------------------
VDDK 是 C SDK，需要 Broadcom 授权与分发协议，且必须随平台分发二进制——
与本项目「零客户端安装 + 完全离线交付」的约束冲突。本 Provider 走
**宿主机自带命令 + SSH/SFTP 流式传输** 的等价路径：

* 资产发现：``vim-cmd vmsvc/getallvms``
* 一致性快照：``vim-cmd vmsvc/snapshot.create``（quiesce=1 时依赖 VMware Tools）
* 数据面：``tar --exclude delta/ctk`` 打包 VM 目录，经 SSH 流式拉回平台
* 恢复 / 克隆：回传归档 → 解开到 datastore → ``vim-cmd solo/registervm`` 注册
  （新 VM 改写 displayName / MAC / 网络，接隔离端口组）

能力诚实声明：**无块级增量**（CBT 只提供变更区间，读取脏块必须 VDDK 或
NFC Range + CGI ticket，二者都不可在零安装约束下实现）。增量任务会被引擎
明确回退为全量，不伪造增量。
"""
import os
import re
import shlex
import time

from core.vm import ssh as vmssh
from core.vm.base import VMProvider
from core.vm.types import (
    BackupArtifact, CloneResult, CloneSpec, ProviderCaps, SnapshotRef,
    VMDisk, VMInfo, VMProviderError,
)


class ESXiSSHProvider(VMProvider):
    provider_id = "esxi_ssh"
    display_name = "VMware ESXi (SSH)"

    def __init__(self, hypervisor: dict, logger=None):
        super().__init__(hypervisor, logger)
        self._work_dir = (self.extra.get("work_dir")
                          or "/vmfs/volumes/datastore1/vmbk").rstrip("/")
        self._client = None

    def _c(self):
        if self._client is None:
            self._client = vmssh.connect(self.hv)
        return self._client

    def _run(self, cmd: str, timeout: int = 600) -> dict:
        return vmssh.exec_capture(self._c(), cmd, timeout=timeout)

    def _vim(self, args: str, timeout: int = 180) -> dict:
        return self._run("vim-cmd %s" % args, timeout=timeout)

    def connect(self):
        r = self._vim("vmsvc/getallvms", timeout=120)
        if r["rc"] != 0:
            return False, "ESXi 连接失败: %s" % (r["err"] or r["out"] or "")[:300]
        v = self._run("vmware -v", timeout=60)
        return True, ("ESXi 连接正常（%s）" % (v.get("out") or "").strip()[:60])

    def version(self) -> str:
        return (self._run("vmware -v", timeout=60).get("out") or "").strip()[:80]

    def capabilities(self) -> ProviderCaps:
        return ProviderCaps(
            incremental=False, change_tracking=False, instant_restore=False,
            clone_to_new_vm=True, file_restore=False, snapshot=True,
            consistency_levels=["crash", "fs"],
            notes="ESXi SSH 通道：仅整机型全量备份（无块级增量，需块级增量请走 PVE/PBS "
                  "或具备 VDDK 采集节点的方案）。")

    # ---------------- 资产发现 ----------------
    def list_vms(self) -> list:
        r = self._vim("vmsvc/getallvms", timeout=120)
        if r["rc"] != 0:
            raise VMProviderError("获取 ESXi 虚拟机列表失败: %s" % (r["err"] or "")[:300])
        out = []
        for line in (r["out"] or "").splitlines()[1:]:
            m = re.match(r"^(\d+)\s+(\S.*?)\s+\[([^\]]+)\]\s+(\S+\.vmx)\s+(\S+)\s+(\S+)",
                         line.strip())
            if not m:
                continue
            vmid, name, ds, vmx, guest, ver = m.groups()
            ds_path = "/vmfs/volumes/%s/%s" % (ds, os.path.dirname(vmx))
            out.append(VMInfo(
                ref=vmid, name=name, guest_os=guest,
                power_state=self._state(vmid),
                disks=self._disks(ds_path, vmx),
                raw={"datastore": ds, "vmx": vmx, "dir": ds_path},
            ))
        return out

    def _state(self, vmid: str) -> str:
        r = self._vim("vmsvc/power.getstate %s" % vmid, timeout=60)
        return "running" if "on" in (r.get("out") or "").lower() else "stopped"

    def _disks(self, ds_path: str, vmx_rel: str) -> list:
        vmx = "%s/%s" % (ds_path, os.path.basename(vmx_rel))
        r = self._run("cat %s 2>/dev/null || echo ''" % shlex.quote(vmx), timeout=60)
        disks = []
        seen = set()
        for line in (r.get("out") or "").splitlines():
            m = re.match(r"^(scsi|sata|nvme|ide)\d+:\d+\.fileName\s*=\s*\"(.+)\"", line.strip())
            if not m:
                continue
            dev, fname = m.group(1), m.group(2).strip()
            if not fname.lower().endswith(".vmdk") or fname in seen:
                continue
            seen.add(fname)
            path = fname if fname.startswith("/") else "%s/%s" % (ds_path, fname)
            if "rdm" in path.lower() or path.lower().startswith("/dev/"):
                disks.append(VMDisk(key=dev, path=path, excluded=True,
                                    exclude_reason="RDM/裸盘不支持文件级备份"))
                continue
            size = self._vmdk_size(path)
            disks.append(VMDisk(key=dev, path=path, size_bytes=size, format="vmdk"))
        return disks

    def _vmdk_size(self, vmdk_path: str) -> int:
        r = self._run("grep -i '^createType\\|RW' %s 2>/dev/null | head -5"
                      % shlex.quote(vmdk_path), timeout=60)
        m = re.search(r"RW\s+(\d+)", r.get("out") or "")
        return int(m.group(1)) * 512 if m else 0

    def _vm_dir(self, vm: VMInfo) -> str:
        return (vm.raw or {}).get("dir") or ""

    # ---------------- 备份 ----------------
    def backup_full(self, vm_ref: str, work_dir: str, consistency: str = "crash") -> BackupArtifact:
        vm = self.get_vm(vm_ref) or VMInfo(ref=vm_ref)
        vmdir = self._vm_dir(vm)
        if not vmdir:
            raise VMProviderError("无法确定 VM 目录（datastore 路径解析失败）")
        stage = work_dir or self._work_dir
        self._run("mkdir -p %s" % shlex.quote(stage), timeout=120)
        snap = "vmbk_%s" % time.strftime("%Y%m%d%H%M%S")
        quiesce = 1 if consistency in ("fs", "app") else 0
        r = self._vim("vmsvc/snapshot.create %s %s 'platform-vm-backup' 0 %d"
                      % (vm.ref, snap, quiesce), timeout=900)
        snap_ok = r["rc"] == 0
        if not snap_ok:
            self.log("ESXi 快照创建失败（%s），继续按崩溃一致备份",
                     (r.get("err") or "")[:120])
        tar = "%s/%s_%s.tar.gz" % (stage, vm.name or vm.ref, time.strftime("%Y%m%d_%H%M%S"))
        try:
            pack = self._run(
                "cd %s && tar --exclude='*-delta.vmdk' --exclude='*-ctk.vmdk' "
                "--exclude='*.vswp' --exclude='*.log' --exclude='*.nvram' "
                "--exclude='*-flat.vmdk.tmp' -czf %s ." % (shlex.quote(vmdir),
                                                           shlex.quote(tar)),
                timeout=21600)
            if pack["rc"] != 0:
                raise VMProviderError("ESXi 打包 VM 目录失败: %s" % (pack["err"] or "")[:300])
        finally:
            if snap_ok:
                self._vim("vmsvc/snapshot.removeall %s" % vm.ref, timeout=900)
        return BackupArtifact(path=tar, kind="full", size_bytes=0,
                              consistency=consistency,
                              extra={"vm_name": vm.name, "vm_dir": vmdir,
                                     "quiesced": bool(quiesce and snap_ok)})

    def backup_incremental(self, vm_ref: str, work_dir: str, parent_token: str,
                           consistency: str = "crash") -> BackupArtifact:
        raise VMProviderError(
            "ESXi SSH 通道不支持块级增量：CBT 只提供变更区间，读取脏块需要 VDDK。"
            "如需块级增量请使用 PVE/PBS 或在具备 VDDK 采集节点的环境接入。")

    def download_artifact(self, artifact: BackupArtifact, local_path: str) -> dict:
        res = vmssh.pull_file(self._c(), artifact.path, local_path,
                              label=os.path.basename(artifact.path))
        return {"size": res.get("size") or 0, "sha256": res.get("sha256") or ""}

    def upload_artifact(self, local_path: str, remote_dir: str) -> str:
        self._run("mkdir -p %s" % shlex.quote(remote_dir), timeout=120)
        remote = "%s/%s" % (remote_dir.rstrip("/"), os.path.basename(local_path))
        vmssh.push_file(self._c(), local_path, remote)
        return remote

    def cleanup_artifact(self, artifact: BackupArtifact) -> None:
        try:
            self._run("rm -f %s" % shlex.quote(artifact.path), timeout=120)
        except Exception:
            pass

    # ---------------- 恢复 / 克隆 ----------------
    def _extract(self, archive: str, dest_dir: str) -> None:
        self._run("mkdir -p %s && tar -xzf %s -C %s"
                  % (shlex.quote(dest_dir), shlex.quote(archive), shlex.quote(dest_dir)),
                  timeout=21600)

    def restore_in_place(self, vm_ref: str, artifacts: list, consistency: str = "crash"):
        vm = self.get_vm(vm_ref) or VMInfo(ref=vm_ref)
        vmdir = self._vm_dir(vm)
        if not vmdir or not artifacts:
            return False, "缺少 VM 目录或备份产物"
        self._vim("vmsvc/power.off %s" % vm.ref, timeout=300)
        stage = "%s/restore_%s" % (self._work_dir, time.strftime("%Y%m%d%H%M%S"))
        self._extract(artifacts[0], stage)
        # 仅覆盖虚拟机磁盘与配置（不删快照等宿主对象）
        r = self._run("cp -f %s/*.vmdk %s/ 2>/dev/null; cp -f %s/*.vmx %s/ 2>/dev/null; true"
                      % (shlex.quote(stage), shlex.quote(vmdir),
                         shlex.quote(stage), shlex.quote(vmdir)), timeout=21600)
        self._run("rm -rf %s" % shlex.quote(stage), timeout=300)
        self._vim("vmsvc/power.on %s" % vm.ref, timeout=300)
        return True, "ESXi 原位还原完成（已回写 VMX/VMDK 并开机）%s" % (
            "" if r["rc"] == 0 else "（部分文件复制有告警）")

    def clone_to_new_vm(self, vm_ref: str, artifacts: list, spec: CloneSpec) -> CloneResult:
        vm = self.get_vm(vm_ref) or VMInfo(ref=vm_ref)
        if not artifacts:
            return CloneResult(ok=False, message="缺少备份产物")
        new_name = spec.target_name or ("%s_clone_%s" % (vm.name or vm.ref,
                                                         time.strftime("%m%d%H%M")))
        datastore = (vm.raw or {}).get("datastore") or self.extra.get("datastore") or ""
        if not datastore:
            return CloneResult(ok=False, message="未配置目标 datastore")
        base = "/vmfs/volumes/%s" % datastore
        new_dir = "%s/%s" % (base, new_name)
        self._run("mkdir -p %s" % shlex.quote(new_dir), timeout=120)
        self._extract(artifacts[0], new_dir)
        vmx = "%s/%s.vmx" % (new_dir, new_name)
        old_vmx = "%s/%s" % (new_dir, os.path.basename((vm.raw or {}).get("vmx") or ""))
        if old_vmx != vmx:
            self._run("mv -f %s %s 2>/dev/null || true"
                      % (shlex.quote(old_vmx), shlex.quote(vmx)), timeout=300)
        # 改写身份与网络，避免与原 VM 冲突
        edits = [
            "s/^displayName.*/displayName = \"%s\"/" % new_name,
            "/^uuid\\.bios/d", "/^uuid\\.location/d",
            "s/^ethernet0\\.generatedAddress.*/ethernet0.generatedAddress = \"%s\"/"
            % self._random_mac(),
            "s/^ethernet0\\.addressType.*/ethernet0.addressType = \"generated\"/",
        ]
        if spec.isolate_network and (spec.network_name or self.extra.get("isolate_network")):
            net = spec.network_name or self.extra.get("isolate_network")
            edits.append("s/^ethernet0\\.networkName.*/ethernet0.networkName = \"%s\"/" % net)
        sed = " ; ".join("sed -i '%s' %s" % (e, shlex.quote(vmx)) for e in edits)
        self._run(sed + " ; true", timeout=300)
        r = self._vim("solo/registervm %s %s" % (shlex.quote(vmx), shlex.quote(new_name)),
                      timeout=300)
        if r["rc"] != 0:
            return CloneResult(ok=False, message="注册新 VM 失败: %s" % (r["err"] or "")[:300])
        new_id = self._find_vmid(new_name)
        if spec.auto_start and new_id:
            self._vim("vmsvc/power.on %s" % new_id, timeout=300)
        return CloneResult(ok=True, target_ref=new_id or new_name, target_name=new_name,
                           ip=self.guest_ip(new_id) if new_id else "",
                           message="已从恢复点克隆为新 VM「%s」%s"
                                   % (new_name, "（隔离网络）" if spec.isolate_network else ""))

    def _find_vmid(self, name: str) -> str:
        r = self._vim("vmsvc/getallvms", timeout=120)
        for line in (r.get("out") or "").splitlines()[1:]:
            m = re.match(r"^(\d+)\s+(\S+)", line.strip())
            if m and m.group(2) == name:
                return m.group(1)
        return ""

    @staticmethod
    def _random_mac() -> str:
        import random
        return "00:50:56:%02x:%02x:%02x" % (random.randint(0, 63),
                                            random.randint(0, 255),
                                            random.randint(0, 255))

    def delete_vm(self, ref: str, node: str = ""):
        self._vim("vmsvc/power.off %s" % ref, timeout=300)
        r = self._vim("vmsvc/destroy %s" % ref, timeout=600)
        return (r["rc"] == 0,
                "已销毁 VM %s" % ref if r["rc"] == 0 else (r["err"] or "")[:200])

    def start_vm(self, ref: str, node: str = ""):
        r = self._vim("vmsvc/power.on %s" % ref, timeout=300)
        return (r["rc"] == 0, "开机成功" if r["rc"] == 0 else (r["err"] or "")[:200])

    def stop_vm(self, ref: str, node: str = "", force: bool = False):
        cmd = "vmsvc/power.off" if force else "vmsvc/power.shutdown"
        r = self._vim("%s %s" % (cmd, ref), timeout=300)
        return (r["rc"] == 0, "关机成功" if r["rc"] == 0 else (r["err"] or "")[:200])

    def guest_ip(self, vm_ref: str, node: str = "") -> str:
        r = self._vim("vmsvc/get.guest %s" % vm_ref, timeout=60)
        m = re.search(r"ipAddress\s*=\s*\"([^\"]+)\"", r.get("out") or "")
        if m and "," not in m.group(1):
            return m.group(1)
        return ""

    def create_snapshot(self, vm_ref: str, name: str,
                        consistency: str = "crash") -> SnapshotRef:
        q = 1 if consistency in ("fs", "app") else 0
        r = self._vim("vmsvc/snapshot.create %s %s 'platform' 0 %d" % (vm_ref, name, q),
                      timeout=900)
        if r["rc"] != 0:
            raise VMProviderError("ESXi 创建快照失败: %s" % (r["err"] or "")[:200])
        return SnapshotRef(id=name, name=name)

    def delete_snapshot(self, vm_ref: str, snap: SnapshotRef) -> None:
        self._vim("vmsvc/snapshot.remove %s %s" % (vm_ref, snap.name), timeout=900)
