# -*- coding: utf-8 -*-
"""虚拟机备份子系统的数据类型定义。

设计上刻意与平台既有的「备份集链 / 恢复点 / PIT」模型同构：
一台受保护 VM 的一个恢复点（RecoveryPoint）对应一条 backup_records，
增量关系由 parent_rp_id 串成链，从而可以直接复用平台的存储分层、
生命周期、保留策略与告警。
"""
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional


# 一致性等级（对齐 Veeam / Zerto 语义）
CONSISTENCY_CRASH = "crash"      # 崩溃一致：直接快照，等同突然断电
CONSISTENCY_FS = "fs"            # 文件系统一致：QEMU GA fsfreeze / VSS
CONSISTENCY_APP = "app"          # 应用一致：VSS Writer / pre-post 钩子
CONSISTENCY_LEVELS = (CONSISTENCY_CRASH, CONSISTENCY_FS, CONSISTENCY_APP)

CONSISTENCY_LABEL = {
    CONSISTENCY_CRASH: "崩溃一致",
    CONSISTENCY_FS: "文件系统一致",
    CONSISTENCY_APP: "应用一致",
}


@dataclass
class VMDisk:
    """一块虚拟磁盘。"""
    key: str = ""                # 盘标识（vda / sda / hard-disk-1 / [ds] vm/vm.vmdk）
    path: str = ""               # 宿主机可见路径（备份数据面用）
    size_bytes: int = 0
    format: str = ""             # qcow2 / raw / vmdk / vhdx
    change_tracking: bool = False  # 是否已启用变更跟踪（CBT / bitmap / RCT）
    excluded: bool = False       # 独立盘 / RDM / 直通盘等不支持的对象
    exclude_reason: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class VMInfo:
    """纳管虚拟化平台上的一台虚拟机。"""
    ref: str = ""                # 平台内唯一标识：PVE=vmid / libvirt=UUID / ESXi=vmid
    name: str = ""
    node: str = ""               # PVE 节点 / ESXi 主机 / libvirt 宿主机
    guest_os: str = ""
    power_state: str = "unknown"  # running | stopped | unknown
    cpu: int = 0
    memory_mb: int = 0
    disks: List[VMDisk] = field(default_factory=list)
    change_tracking: bool = False
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def total_size_bytes(self) -> int:
        return sum(int(d.size_bytes or 0) for d in self.disks if not d.excluded)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["total_size_bytes"] = self.total_size_bytes
        return d


@dataclass
class SnapshotRef:
    """快照/检查点引用。"""
    id: str = ""
    name: str = ""
    created_at: str = ""
    change_token: str = ""       # CBT changeId / checkpoint 名 / RCT ID


@dataclass
class BackupArtifact:
    """一次备份动作在「数据源侧」产出的产物。

    path      : 数据源侧路径（PVE 节点上的 vma.zst / libvirt 宿主机 stage 目录 / …）
    local_path: 拉回备份平台后的落盘路径（由引擎回填）
    """
    path: str = ""
    kind: str = "full"           # full | incremental | synthetic_full
    size_bytes: int = 0
    change_token: str = ""       # 本次结束后的变更跟踪位点（下次增量起点）
    parent_token: str = ""       # 本次基于的位点
    consistency: str = CONSISTENCY_CRASH
    disks: List[str] = field(default_factory=list)
    extra: Dict[str, Any] = field(default_factory=dict)
    local_path: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ProviderCaps:
    """Provider 能力声明（UI 与引擎据此决定是否提供某项能力，不做假成功）。"""
    incremental: bool = False            # 是否支持块级增量（CBT / bitmap / RCT）
    change_tracking: bool = False        # 是否可自动开启变更跟踪
    instant_restore: bool = False        # 即时恢复（备份仓库直接开机）
    clone_to_new_vm: bool = False        # 恢复点为新 VM（演练/取证/开发）
    file_restore: bool = False           # 单文件/单目录恢复
    snapshot: bool = True                # 是否支持快照
    consistency_levels: List[str] = field(
        default_factory=lambda: [CONSISTENCY_CRASH])
    notes: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class CloneSpec:
    """克隆/恢复为新虚拟机的目标规格。"""
    target_name: str = ""
    target_node: str = ""
    target_ref: str = ""         # 指定新 VM 的 ID/名称（可选）
    isolate_network: bool = True  # 隔离网络（Veeam SureBackup 的必需项）
    network_name: str = ""       # 隔离网络名（bridge / portgroup / vSwitch）
    auto_start: bool = True
    live: bool = False           # 即时恢复（边跑边回填，RTO 秒级）
    regenerate_mac: bool = True
    ttl_hours: int = 0           # >0 时到期自动销毁
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class CloneResult:
    ok: bool = False
    target_ref: str = ""
    target_name: str = ""
    ip: str = ""
    message: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class HealthCheckResult:
    """恢复验证（SureBackup 式）的一次健康检查。"""
    name: str = ""
    ok: bool = False
    message: str = ""
    duration_sec: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class VerifyResult:
    """恢复验证整体结论：备份能不能真的拉起来、能不能真的服务。"""
    ok: bool = False
    target_ref: str = ""
    checks: List[HealthCheckResult] = field(default_factory=list)
    message: str = ""
    duration_sec: float = 0.0
    cleaned: bool = False        # 验证后隔离 VM 是否已销毁

    def to_dict(self) -> dict:
        d = asdict(self)
        d["checks"] = [c if isinstance(c, dict) else c.to_dict()
                       for c in self.checks]
        return d


class VMProviderError(RuntimeError):
    """Provider 操作失败。"""


__all__ = [
    "CONSISTENCY_CRASH", "CONSISTENCY_FS", "CONSISTENCY_APP",
    "CONSISTENCY_LEVELS", "CONSISTENCY_LABEL",
    "VMDisk", "VMInfo", "SnapshotRef", "BackupArtifact", "ProviderCaps",
    "CloneSpec", "CloneResult", "HealthCheckResult", "VerifyResult",
    "VMProviderError",
]
