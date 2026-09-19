# -*- coding: utf-8 -*-
"""单次任务「走了哪条通道、侵入到什么等级」的判定层（P0 落点）。

与 :mod:`core.agentless.channels` 的分工：

- ``channels`` 是**能力声明**（平台一共有哪些通道、各自等级）；
- ``plan``   是**单次归因**（某个任务这次到底走了哪几条、最高侵入到哪级）。

刻意写成纯函数（只吃 dict，不碰 DB / SSH），因此可以离线单测，也不会因为
某台主机连不上就影响任务列表渲染。判定规则全部对齐
``docs/agentless_architecture_20260919.md §2``，与实现不一致以实现为准并同步文档。
"""
from __future__ import annotations

import json
from typing import Optional

from .channels import INVASION_LEVELS, channels, target_requirements

#: 等级轻重顺序（用于取"最高侵入等级"）：越靠后越重
_LEVEL_ORDER = ["A0", "A1", "A2", "A3", "X"]

#: 走本机/目标端自带客户端执行的数据库（logical 模式）
_DB_NATIVE_CLIENT = {
    "mysql": "mysqldump", "mariadb": "mariadb-dump", "postgresql": "pg_dump",
    "kingbase": "sys_dump", "dameng": "dexp", "oracle": "expdp",
    "sqlserver": "sqlcmd", "redis": "redis-cli", "mongodb": "mongodump",
}


def _extra_of(task: dict) -> dict:
    raw = task.get("extra_options")
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw) if raw else {}
    except Exception:
        return {}


def _worst(levels: list[str]) -> str:
    worst = "A0"
    for lv in levels:
        if lv in _LEVEL_ORDER and _LEVEL_ORDER.index(lv) > _LEVEL_ORDER.index(worst):
            worst = lv
    return worst


def _mk(channels_by_id: dict, cid: str, reason: str) -> Optional[dict]:
    """把通道声明拼上"本次为什么算它"的原因。"""
    c = channels_by_id.get(cid)
    if not c:
        return None
    item = dict(c)
    item["level_desc"] = INVASION_LEVELS.get(c["invasiveness"], "")
    item["reason"] = reason
    return item


def plan_for_task(task: dict, ssh_host: Optional[dict] = None) -> dict:
    """给出单个任务的通道计划。

    :param task: ``core.models.get_task`` 的字典（不需要 include_secret）
    :param ssh_host: 任务实际使用的 SSH 主机（``ssh_hosts.get_host``），
        本机任务传 ``None``。传了但任务没引用 SSH 时仍会按其自身判断。
    :return: ``{channels, max_level, max_level_desc, target, temp_binary,
        requires_target_config, ...}``
    """
    db_type = (task.get("db_type") or "").lower()
    extra = _extra_of(task)
    mode = (task.get("backup_mode") or extra.get("backup_mode") or "logical").lower()
    realtime = bool(task.get("rt_enabled") or extra.get("rt_enabled"))
    by_id = {c["id"]: c for c in channels()}
    used: list[dict] = []

    # ---- 1) 预检/列举通道：与备份执行通道可能不同，必须分开算 ----
    if db_type in ("vm", "object_storage"):
        probe = _mk(by_id, "rest", "虚拟化控制面 / 对象存储 S3 REST 端口，纯标准协议探活")
    elif db_type == "file":
        probe = _mk(by_id, "ssh", "文件系统备份经 SSH/SFTP 执行")
    else:
        probe = _mk(by_id, "native-protocol",
                    "连接测试 / 拉库列表走数据库原生协议直连")
    if probe:
        used.append(probe)

    # ---- 2) 执行通道 ----
    # 纯 REST 控制面的类型（对象存储、虚拟机）压根不连目标端 shell，因此**不应**
    # 出现 SSH 通道：把它们算成 SSH 会虚报侵入等级。
    if db_type in ("vm", "object_storage"):
        return {
            "task_id": task.get("id"), "task_name": task.get("name") or "",
            "db_type": db_type, "backup_mode": mode, "realtime": realtime,
            "target": "%s（控制面 REST）" % (task.get("host") or ""),
            "ssh_host_id": None, "channels": used, "max_level": "A0",
            "max_level_desc": INVASION_LEVELS["A0"], "temp_binary": False,
            "requires_target_config": [], "forbidden_ok": True,
            "note": "该类型全部动作走标准 REST 控制面，既不 SSH 也不推送二进制，"
                    "目标端零接触。",
        }
    host_ref = ssh_host or {}
    ssh_host_id = extra.get("ssh_host_id") or task.get("ssh_host_id")
    has_ssh_cred = bool(extra.get("ssh_cred"))
    if host_ref or has_ssh_cred or ssh_host_id:
        used.append(_mk(by_id, "ssh",
                        "备份/恢复命令经 %s 在目标端执行（只调用目标端自带命令）"
                        % (host_ref.get("host_key") or "任务自带 SSH 凭据")))
        target = host_ref.get("host_key") or (task.get("host") or "SSH 目标")
    else:
        used.append(_mk(by_id, "ssh",
                        "目标在平台本机或未纳管 SSH：走平台本机通道执行本机命令"))
        target = "%s:%s（平台本机）" % (task.get("host") or "127.0.0.1",
                                   task.get("port") or "-")

    # ---- 3) 物理备份：可能触发临时二进制（A2） ----
    temp_binary = False
    if mode == "physical" and db_type in ("mysql", "mariadb"):
        used.append(_mk(by_id, "temp-binary",
                        "%s 物理备份：目标端无该工具时由平台临时推送 xtrabackup/"
                        "mariabackup 到临时目录，执行完即删"
                        % db_type))
        temp_binary = True
    elif mode == "physical":
        used.append(_mk(by_id, "ssh",
                        "%s 物理备份调用数据库自带工具（%s），不推送任何二进制"
                        % (db_type, {"postgresql": "pg_basebackup",
                                     "kingbase": "sys_basebackup",
                                     "oracle": "rman"}.get(db_type, "自带工具"))))

    # ---- 4) 实时备份：需要目标端开启配置（A3，属于"改配置"不是"装软件"） ----
    reqs: list[dict] = []
    if realtime:
        reqs = target_requirements(db_type)
        for r in reqs:
            used.append(_mk(by_id, "ssh",
                            "实时链路要求目标端开启 %s（客户侧配置，非安装软件）"
                            % r["item"]))
        # A3 由"要求目标端改配置"本身构成，即使该库型未登记前置项也算 A3
        if not reqs:
            used.append(_mk(by_id, "ssh",
                            "实时链路已启用，需按该库型的 CDC 前置配置准备目标端"))

    levels = [u["invasiveness"] for u in used if u.get("invasiveness")]
    if realtime:
        levels.append("A3")
    max_level = _worst(levels) if levels else "A0"

    return {
        "task_id": task.get("id"),
        "task_name": task.get("name") or "",
        "db_type": db_type,
        "backup_mode": mode,
        "realtime": realtime,
        "target": target,
        "ssh_host_id": host_ref.get("id") or ssh_host_id,
        "channels": used,
        "max_level": max_level,
        "max_level_desc": INVASION_LEVELS.get(max_level, ""),
        "temp_binary": temp_binary,
        "requires_target_config": reqs,
        "forbidden_ok": max_level != "X",
        "note": "本次通道由任务配置推导（纯离线判定）；目标端有无残留请用 "
                "GET /api/v1/targets/<id>/no-agent-audit 做只读取证。",
    }


def plan_tasks(tasks: list, host_of: Optional[dict] = None) -> list[dict]:
    """批量推导。``host_of`` 为 ``{host_id: 主机字典}`` 映射，避免逐个查库。"""
    cache = host_of or {}
    out = []
    for t in tasks or []:
        extra = _extra_of(t)
        hid = extra.get("ssh_host_id") or t.get("ssh_host_id")
        host = cache.get(int(hid)) if hid else None
        try:
            out.append(plan_for_task(t, host))
        except Exception as e:  # 单个任务判定失败不应拖垮整页
            out.append({"task_id": t.get("id"), "task_name": t.get("name") or "",
                        "db_type": t.get("db_type"), "max_level": "A0",
                        "channels": [], "error": "推导失败: %s" % str(e)[:200]})
    return out


def summarize(plans: list[dict]) -> dict:
    """把一批任务的计划汇总成面板用的一句话结论。"""
    from collections import Counter
    cnt = Counter(p.get("max_level") or "A0" for p in plans or [])
    worst = _worst(list(cnt)) if cnt else "A0"
    return {
        "total": len(plans or []),
        "by_level": dict(cnt),
        "worst_level": worst,
        "worst_level_desc": INVASION_LEVELS.get(worst, ""),
        "temp_binary_tasks": sum(1 for p in plans or [] if p.get("temp_binary")),
    }
