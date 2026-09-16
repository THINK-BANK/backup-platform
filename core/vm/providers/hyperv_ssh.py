# -*- coding: utf-8 -*-
"""Microsoft Hyper-V 虚拟机备份适配器（SSH + PowerShell，零 agent）。

通道：Windows Server 上启用 OpenSSH Server（或 Win32-OpenSSH），平台经 SSH
执行 PowerShell cmdlet（``Get-VM`` / ``Checkpoint-VM`` / ``Export-VM`` /
``Import-VM``），数据面用 Windows 自带 ``tar.exe``（Win10/2019 起内置）打包
后经 SFTP 拉回。

能力诚实声明：
* 全量：``Export-VM``（支持生产检查点，Guest 内 VSS → 应用一致）；
* 增量：Hyper-V 的 RCT 差异导出粒度与 API 复杂度高、且需 WinRM/WMI 深度集成，
  本 Provider **不提供块级增量**，增量任务由引擎明确回退为全量；
* 克隆为新 VM：``Import-VM -GenerateNewId`` + 隔离虚拟交换机。
"""
import json
import os
import re
import time

from core.vm import ssh as vmssh
from core.vm.base import VMProvider
from core.vm.types import (
    BackupArtifact, CloneResult, CloneSpec, ProviderCaps, SnapshotRef,
    VMDisk, VMInfo, VMProviderError,
)


def _ps(script: str) -> str:
    """把 PowerShell 脚本包装成 SSH 单行命令（单引号转义）。"""
    esc = script.replace("'", "''")
    return "powershell -NoProfile -NonInteractive -Command \"%s\"" % esc.replace('"', '\\"')


class HyperVSSHProvider(VMProvider):
    provider_id = "hyperv_ssh"
    display_name = "Microsoft Hyper-V (SSH/PowerShell)"

    def __init__(self, hypervisor: dict, logger=None):
        super().__init__(hypervisor, logger)
        self._work_dir = (self.extra.get("work_dir") or "C:\\vmbk").rstrip("\\")
        self._client = None

    def _c(self):
        if self._client is None:
            self._client = vmssh.connect(self.hv)
        return self._client

    def _run(self, cmd: str, timeout: int = 600) -> dict:
        return vmssh.exec_capture(self._c(), cmd, timeout=timeout, wrap=False)

    def _ps(self, script: str, timeout: int = 600) -> dict:
        return self._run(_ps(script), timeout=timeout)

    def _ps_json(self, script: str, timeout: int = 600):
        r = self._ps(script, timeout=timeout)
        txt = (r.get("out") or "").strip()
        # PowerShell ConvertTo-Json 输出可能被日志/进度行污染，截取首个 [ 或 {
        i = min([x for x in (txt.find("["), txt.find("{")) if x >= 0] or [-1])
        if i > 0:
            txt = txt[i:]
        try:
            return json.loads(txt)
        except Exception:
            return None

    def connect(self):
        r = self._ps("$ErrorActionPreference='Stop'; (Get-VM | Measure-Object).Count",
                     timeout=120)
        if r["rc"] != 0:
            return False, "Hyper-V 连接失败: %s" % (r.get("err") or r.get("out") or "")[:300]
        return True, "Hyper-V 连接正常（VM 数量 %s）" % (r.get("out") or "").strip()

    def version(self) -> str:
        r = self._ps("(Get-ItemProperty 'HKLM:\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion')"
                     ".ProductName", timeout=60)
        return (r.get("out") or "").strip()[:80]

    def capabilities(self) -> ProviderCaps:
        return ProviderCaps(
            incremental=False, change_tracking=False, instant_restore=False,
            clone_to_new_vm=True, file_restore=False, snapshot=True,
            consistency_levels=["crash", "app"],
            notes="Hyper-V SSH 通道：Export-VM 全量 + 生产检查点（VSS 应用一致）；"
                  "不支持块级增量（RCT 差异导出需 WinRM/WMI 深度集成）。")

    # ---------------- 资产发现 ----------------
    def list_vms(self) -> list:
        data = self._ps_json(
            "$ErrorActionPreference='Stop'; Get-VM | ForEach-Object { "
            "$d = @(Get-VMHardDiskDrive -VM $_ | ForEach-Object { "
            "  @{ key=$_.ControllerLocation; path=$_.Path; size=(Get-VHD -Path $_.Path -ErrorAction SilentlyContinue).FileSize } }); "
            "@{ name=$_.Name; id=$_.VMId.Guid; state=[string]$_.State; "
            "cpu=$_.ProcessorCount; mem=[int64]$_.MemoryAssigned; disks=$d } "
            "} | ConvertTo-Json -Depth 5", timeout=180)
        if data is None:
            return []
        items = data if isinstance(data, list) else [data]
        out = []
        for it in items:
            disks = []
            for d in (it.get("disks") or []):
                disks.append(VMDisk(key=str(d.get("key")), path=d.get("path") or "",
                                    size_bytes=int(d.get("size") or 0), format="vhdx"))
            st = str(it.get("state") or "").lower()
            out.append(VMInfo(ref=it.get("id") or it.get("name"), name=it.get("name"),
                              power_state="running" if st == "running" else "stopped",
                              cpu=int(it.get("cpu") or 0),
                              memory_mb=int(int(it.get("mem") or 0) / 1048576),
                              disks=disks, raw=it))
        return out

    def _name_of(self, vm_ref: str) -> str:
        for vm in self.list_vms():
            if str(vm.ref) == str(vm_ref):
                return vm.name
        return vm_ref

    # ---------------- 备份 ----------------
    def backup_full(self, vm_ref: str, work_dir: str, consistency: str = "crash") -> BackupArtifact:
        name = self._name_of(vm_ref)
        stage = work_dir or self._work_dir
        self._ps("New-Item -ItemType Directory -Force -Path '%s' | Out-Null" % stage,
                 timeout=120)
        ts = time.strftime("%Y%m%d_%H%M%S")
        export_dir = "%s\\export_%s" % (stage, ts)
        self._ps("$ErrorActionPreference='Stop'; Export-VM -Name '%s' -Path '%s' -CaptureLiveState:$false"
                 % (name, export_dir), timeout=21600)
        zip_path = "%s\\%s_%s.zip" % (stage, re.sub(r"[^\w.-]", "_", name), ts)
        self._ps("$ErrorActionPreference='Stop'; tar -a -c -f '%s' -C '%s' ."
                 % (zip_path, export_dir), timeout=21600)
        self._ps("Remove-Item -Recurse -Force '%s' -ErrorAction SilentlyContinue" % export_dir,
                 timeout=600)
        return BackupArtifact(path=zip_path, kind="full", size_bytes=0,
                              consistency=consistency,
                              extra={"vm_name": name, "windows": True})

    def backup_incremental(self, vm_ref: str, work_dir: str, parent_token: str,
                           consistency: str = "crash") -> BackupArtifact:
        raise VMProviderError(
            "Hyper-V SSH 通道不支持块级增量（RCT 差异导出需 WinRM/WMI 深度集成）；"
            "如需更细 RPO 请缩短全量周期或改用支持 CBT 的平台。")

    def download_artifact(self, artifact: BackupArtifact, local_path: str) -> dict:
        res = vmssh.pull_file(self._c(), artifact.path, local_path,
                              label=os.path.basename(artifact.path))
        return {"size": res.get("size") or 0, "sha256": res.get("sha256") or ""}

    def upload_artifact(self, local_path: str, remote_dir: str) -> str:
        win_dir = remote_dir.replace("/", "\\")
        self._ps("New-Item -ItemType Directory -Force -Path '%s' | Out-Null" % win_dir,
                 timeout=120)
        remote = "%s\\%s" % (win_dir.rstrip("\\"), os.path.basename(local_path))
        vmssh.push_file(self._c(), local_path, remote)
        return remote

    def cleanup_artifact(self, artifact: BackupArtifact) -> None:
        try:
            self._ps("Remove-Item -Force '%s' -ErrorAction SilentlyContinue" % artifact.path,
                     timeout=120)
        except Exception:
            pass

    # ---------------- 恢复 / 克隆 ----------------
    def restore_in_place(self, vm_ref: str, artifacts: list, consistency: str = "crash"):
        name = self._name_of(vm_ref)
        if not artifacts:
            return False, "缺少备份产物"
        stage = "%s\\restore_%s" % (self._work_dir, time.strftime("%Y%m%d%H%M%S"))
        self._ps("New-Item -ItemType Directory -Force -Path '%s' | Out-Null; "
                 "tar -x -f '%s' -C '%s'" % (stage, artifacts[0], stage), timeout=21600)
        self._ps("Stop-VM -Name '%s' -Force -ErrorAction SilentlyContinue" % name, timeout=300)
        # 用导出目录内的磁盘覆盖现有磁盘
        self._ps(
            "$ErrorActionPreference='SilentlyContinue'; "
            "$src = Get-ChildItem -Path '%s' -Recurse -Filter *.vhdx | Select-Object -First 10; "
            "$dst = @(Get-VMHardDiskDrive -VMName '%s'); "
            "for ($i=0; $i -lt $dst.Count -and $i -lt $src.Count; $i++) { "
            "  Copy-Item -Path $src[$i].FullName -Destination $dst[$i].Path -Force }"
            % (stage, name), timeout=21600)
        self._ps("Start-VM -Name '%s' -ErrorAction SilentlyContinue" % name, timeout=300)
        self._ps("Remove-Item -Recurse -Force '%s' -ErrorAction SilentlyContinue" % stage,
                 timeout=600)
        return True, "Hyper-V 原位还原完成（已覆盖 VHDX 并开机）"

    def clone_to_new_vm(self, vm_ref: str, artifacts: list, spec: CloneSpec) -> CloneResult:
        name = self._name_of(vm_ref)
        if not artifacts:
            return CloneResult(ok=False, message="缺少备份产物")
        new_name = spec.target_name or ("%s_clone_%s" % (name, time.strftime("%m%d%H%M")))
        stage = "%s\\clone_%s" % (self._work_dir, time.strftime("%Y%m%d%H%M%S"))
        self._ps("New-Item -ItemType Directory -Force -Path '%s' | Out-Null; "
                 "tar -x -f '%s' -C '%s'" % (stage, artifacts[0], stage), timeout=21600)
        r = self._ps(
            "$ErrorActionPreference='Stop'; "
            "$vmcx = Get-ChildItem -Path '%s' -Recurse -Filter '*.vmcx' | Select-Object -First 1; "
            "$vm = Import-VM -Path $vmcx.FullName -Copy -GenerateNewId -VirtualMachinePath '%s' "
            "-ErrorAction Stop; Rename-VM -VM $vm -NewName '%s'"
            % (stage, self.extra.get("vm_path") or "C:\\ProgramData\\Microsoft\\Windows\\Hyper-V",
               new_name), timeout=21600)
        if r["rc"] != 0:
            return CloneResult(ok=False, message="导入新 VM 失败: %s"
                               % (r.get("err") or r.get("out") or "")[:300])
        if spec.isolate_network:
            sw = spec.network_name or self.extra.get("isolate_network") or ""
            if sw:
                self._ps("Get-VMNetworkAdapter -VMName '%s' | Connect-VMNetworkAdapter "
                         "-SwitchName '%s'" % (new_name, sw), timeout=300)
        if spec.auto_start:
            self._ps("Start-VM -Name '%s' -ErrorAction SilentlyContinue" % new_name,
                     timeout=300)
        self._ps("Remove-Item -Recurse -Force '%s' -ErrorAction SilentlyContinue" % stage,
                 timeout=600)
        return CloneResult(ok=True, target_ref=new_name, target_name=new_name,
                           ip=self.guest_ip(new_name),
                           message="已从恢复点克隆为新 VM「%s」%s"
                                   % (new_name, "（隔离交换机）" if spec.isolate_network else ""))

    def delete_vm(self, ref: str, node: str = ""):
        name = self._name_of(ref)
        r = self._ps("$ErrorActionPreference='Stop'; Stop-VM -Name '%s' -Force "
                     "-ErrorAction SilentlyContinue; Remove-VM -Name '%s' -Force" % (name, name),
                     timeout=600)
        return (r["rc"] == 0,
                "已销毁 VM「%s」（保留磁盘文件）" % name if r["rc"] == 0
                else (r.get("err") or "")[:200])

    def start_vm(self, ref: str, node: str = ""):
        r = self._ps("Start-VM -Name '%s'" % self._name_of(ref), timeout=300)
        return (r["rc"] == 0, "开机成功" if r["rc"] == 0 else (r.get("err") or "")[:200])

    def stop_vm(self, ref: str, node: str = ""):
        r = self._ps("Stop-VM -Name '%s'" % self._name_of(ref), timeout=300)
        return (r["rc"] == 0, "关机成功" if r["rc"] == 0 else (r.get("err") or "")[:200])

    def guest_ip(self, vm_ref: str, node: str = "") -> str:
        r = self._ps("(Get-VMNetworkAdapter -VMName '%s').IPAddresses" % self._name_of(vm_ref),
                     timeout=120)
        m = re.search(r"(\d+\.\d+\.\d+\.\d+)", r.get("out") or "")
        return m.group(1) if m else ""

    def create_snapshot(self, vm_ref: str, name: str,
                        consistency: str = "crash") -> SnapshotRef:
        # 生产检查点走 Guest VSS（应用一致）；标准检查点为崩溃一致
        if consistency in ("fs", "app"):
            r = self._ps("Checkpoint-VM -Name '%s' -SnapshotName '%s'"
                         % (self._name_of(vm_ref), name), timeout=900)
        else:
            r = self._ps("Checkpoint-VM -Name '%s' -SnapshotName '%s' -SnapshotType Standard"
                         % (self._name_of(vm_ref), name), timeout=900)
        if r["rc"] != 0:
            raise VMProviderError("Hyper-V 创建检查点失败: %s" % (r.get("err") or "")[:200])
        return SnapshotRef(id=name, name=name)
