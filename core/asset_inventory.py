# -*- coding: utf-8 -*-
"""备份数据资产盘点与价值评估（Asset Inventory & Value Scoring）。

业界数据资产管理平台的通用路径是「盘点 → 分级 → 评估 → 治理 → 运营」。
本模块把这条路径落到**备份侧的真实元数据**上（backup_tasks / backup_records /
backup_sets / rt_tasks / vdb_instances / restore_test_reports …），回答三个问题：

1. 我到底有哪些数据资产？多大？分布在哪些实例/存储层级？（盘点）
2. 哪些是高价值核心资产、哪些是睡在磁盘里的僵尸资产？（价值评估）
3. 哪些数据该降级到冷存储 / 该清理 / 该补保护？（治理建议，带量化收益）

所有取值均来自元数据库，不做假数据；统计失败（表/列缺失）自动降级为 0 并标记。
"""
import os
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional

import core.db as db

# ----------------------------- 安全查询 -----------------------------

def _q(sql: str, params: tuple = ()) -> List[dict]:
    """查询失败返回空列表（某些可选列/表在精简库上不存在）。"""
    try:
        return db.query(sql, params) or []
    except Exception:  # noqa: BLE001
        return []


def _days_since(iso: Optional[str]) -> Optional[int]:
    if not iso:
        return None
    try:
        s = str(iso).replace("T", " ").split(".")[0]
        dt = datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S")
        return (datetime.now() - dt).days
    except Exception:  # noqa: BLE001
        return None


def _pct(n: float) -> int:
    return int(round(float(n or 0)))


# ----------------------------- 盘点 -----------------------------

def _task_rows() -> List[dict]:
    return _q(
        "SELECT id, name, biz_system, db_type, host, port, db_name, backup_type, "
        "backup_mode, schedule_type, cron_expr, interval_minutes, enabled, "
        "retention_days, retention_count, storage_backend, demo_only, "
        "created_at, updated_at, last_run_at, last_status "
        "FROM backup_tasks ORDER BY id")


def _record_agg() -> Dict[int, dict]:
    """按 task_id 聚合备份记录统计。"""
    rows = _q(
        "SELECT task_id, COUNT(*) AS cnt, "
        "SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) AS ok_cnt, "
        "SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS fail_cnt, "
        "SUM(CASE WHEN status='success' THEN size_bytes ELSE 0 END) AS bytes, "
        "MAX(CASE WHEN status='success' THEN size_bytes ELSE 0 END) AS max_bytes, "
        "MIN(started_at) AS first_at, MAX(started_at) AS last_at, "
        "MAX(CASE WHEN status='success' THEN started_at END) AS last_ok_at, "
        "SUM(verified) AS verified_cnt, "
        "SUM(CASE WHEN backup_type='incremental' THEN 1 ELSE 0 END) AS inc_cnt, "
        "AVG(CASE WHEN status='success' THEN duration_sec END) AS avg_dur "
        "FROM backup_records WHERE task_id IS NOT NULL GROUP BY task_id")
    out: Dict[int, dict] = {}
    for r in rows:
        out[int(r["task_id"])] = {
            "record_count": int(r["cnt"] or 0),
            "success_count": int(r["ok_cnt"] or 0),
            "failed_count": int(r["fail_cnt"] or 0),
            "total_bytes": int(r["bytes"] or 0),
            "max_bytes": int(r["max_bytes"] or 0),
            "first_backup_at": r.get("first_at") or "",
            "last_backup_at": r.get("last_at") or "",
            "last_success_at": r.get("last_ok_at") or "",
            "verified_count": int(r["verified_cnt"] or 0),
            "incremental_count": int(r["inc_cnt"] or 0),
            "avg_duration_sec": round(float(r["avg_dur"] or 0), 1),
        }
    return out


def _recent_activity(days: int = 30) -> Dict[int, dict]:
    """近 N 天每个任务的备份次数与数据量（用于活跃度 / 增长趋势）。"""
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    rows = _q(
        "SELECT task_id, COUNT(*) AS cnt, SUM(size_bytes) AS bytes "
        "FROM backup_records WHERE status='success' "
        "AND COALESCE(substr(started_at,1,19),'')>=? "
        "AND task_id IS NOT NULL GROUP BY task_id", (since,))
    return {int(r["task_id"]): {"count": int(r["cnt"] or 0),
                                "bytes": int(r["bytes"] or 0)} for r in rows}


def _extension_refs() -> Dict[int, dict]:
    """外部引用：实时保护(rt_tasks) / 同步(managed 引用) / 克隆(VDB)。"""
    refs: Dict[int, dict] = {}
    for r in _q("SELECT task_id, rt_mode, is_running, health_status, rpo_current_seconds "
                "FROM rt_tasks"):
        refs.setdefault(int(r["task_id"]), {}).update({
            "realtime": True,
            "rt_mode": r.get("rt_mode") or "",
            "cdc": (r.get("rt_mode") or "") in ("db_cdc", "mixed"),
            "rt_running": bool(r.get("is_running")),
            "rt_health": r.get("health_status") or "unknown",
            "rpo_seconds": r.get("rpo_current_seconds"),
        })
    for r in _q("SELECT source_task_id FROM sync_tasks WHERE source_type='managed' "
                "AND source_task_id IS NOT NULL"):
        refs.setdefault(int(r["source_task_id"]), {}).update({"sync": True})
    for r in _q("SELECT task_id, COUNT(*) AS c FROM vdb_instances "
                "WHERE task_id IS NOT NULL GROUP BY task_id"):
        refs.setdefault(int(r["task_id"]), {})["clone_count"] = int(r["c"] or 0)
    return refs


def _recoverability() -> Dict[int, dict]:
    """恢复/演练能力：restore_records（成功次数）+ restore_test_reports。"""
    out: Dict[int, dict] = {}
    for r in _q("SELECT task_id, COUNT(*) AS cnt, "
                "SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) AS ok "
                "FROM restore_records WHERE task_id IS NOT NULL GROUP BY task_id"):
        tid = int(r["task_id"])
        out[tid] = {"restore_count": int(r["cnt"] or 0),
                    "restore_ok": int(r["ok"] or 0)}
    for r in _q("SELECT task_id, COUNT(*) AS cnt, SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) AS ok "
                "FROM restore_test_reports WHERE task_id IS NOT NULL GROUP BY task_id"):
        tid = int(r["task_id"])
        cur = out.setdefault(tid, {"restore_count": 0, "restore_ok": 0})
        cur["drill_count"] = int(r["cnt"] or 0)
        cur["drill_ok"] = int(r["ok"] or 0)
    return out


def _tier_distribution() -> List[dict]:
    rows = _q("SELECT COALESCE(storage_tier,'local') AS tier, COUNT(*) AS cnt, "
              "SUM(size_bytes) AS bytes FROM backup_records WHERE status='success' "
              "GROUP BY tier")
    names = {"local": "L1 本地", "minio": "L2 MinIO 热数据", "s3": "L3 S3 冷数据",
             "multi": "多级(本地+对象)", "tape": "磁带归档"}
    return [{"tier": r["tier"], "label": names.get(r["tier"], r["tier"]),
             "count": int(r["cnt"] or 0), "bytes": int(r["bytes"] or 0)}
            for r in rows]


def trend(days: int = 30) -> List[dict]:
    """近 N 天每日备份量与次数（补零，前端画趋势图）。"""
    since = (datetime.now() - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    rows = _q("SELECT substr(started_at,1,10) AS d, COUNT(*) AS cnt, "
              "SUM(size_bytes) AS bytes FROM backup_records "
              "WHERE status='success' AND substr(started_at,1,10)>=? GROUP BY d",
              (since,))
    m = {r["d"]: (int(r["cnt"] or 0), int(r["bytes"] or 0)) for r in rows}
    out = []
    for i in range(days):
        day = (datetime.now() - timedelta(days=days - 1 - i)).strftime("%Y-%m-%d")
        c, b = m.get(day, (0, 0))
        out.append({"date": day, "count": c, "bytes": b})
    return out


def redundancy() -> Dict[str, Any]:
    """重复产物识别：同一 checksum 出现多次 → 对象级重删收益。"""
    rows = _q("SELECT checksum, COUNT(*) AS cnt, SUM(size_bytes) AS bytes, "
              "GROUP_CONCAT(id) AS ids FROM backup_records "
              "WHERE status='success' AND COALESCE(checksum,'')<>'' AND size_bytes>0 "
              "GROUP BY checksum HAVING cnt>1 ORDER BY bytes DESC LIMIT 20")
    groups = []
    saved = 0
    for r in rows:
        cnt = int(r["cnt"] or 0)
        size_each = int(r["bytes"] or 0) / max(1, cnt)
        waste = int(size_each * (cnt - 1))
        saved += waste
        groups.append({
            "checksum": str(r["checksum"])[:16],
            "count": cnt,
            "records": [int(i) for i in str(r["ids"] or "").split(",") if i][:10],
            "wasted_bytes": waste,
        })
    return {"groups": groups, "wasted_bytes": saved, "group_count": len(groups)}


def build_assets(days: int = 30) -> List[dict]:
    """构建资产清单（每备份任务一条资产）。"""
    aggs = _record_agg()
    acts = _recent_activity(days)
    refs = _extension_refs()
    recov = _recoverability()
    assets: List[dict] = []
    for t in _task_rows():
        tid = int(t["id"])
        a = aggs.get(tid, {})
        act = acts.get(tid, {"count": 0, "bytes": 0})
        rf = refs.get(tid, {})
        rv = recov.get(tid, {})
        host = t.get("host") or ""
        inst = "{}{}".format(host, (":" + str(t["port"])) if t.get("port") else "")
        rc_n = a.get("record_count", 0)
        assets.append({
            "task_id": tid,
            "name": t.get("name") or f"task-{tid}",
            "biz_system": t.get("biz_system") or "",
            "db_type": t.get("db_type") or "unknown",
            "instance": inst,
            "host": host,
            "port": t.get("port"),
            "database": t.get("db_name") or "",
            "backup_type": t.get("backup_type") or "full",
            "backup_mode": t.get("backup_mode") or "logical",
            "schedule_type": t.get("schedule_type") or "none",
            "scheduled": (t.get("schedule_type") or "none") != "none",
            "enabled": bool(t.get("enabled", 1)),
            "demo_only": bool(t.get("demo_only", 0)),
            "retention_days": int(t.get("retention_days") or 0),
            "retention_count": int(t.get("retention_count") or 0),
            "storage_backend": t.get("storage_backend") or "local",
            "last_status": t.get("last_status") or "",
            "created_at": t.get("created_at") or "",
            # 规模
            "record_count": rc_n,
            "success_count": a.get("success_count", 0),
            "failed_count": a.get("failed_count", 0),
            "total_bytes": a.get("total_bytes", 0),
            "max_bytes": a.get("max_bytes", 0),
            "avg_duration_sec": a.get("avg_duration_sec", 0),
            "verified_count": a.get("verified_count", 0),
            "incremental_count": a.get("incremental_count", 0),
            # 时间
            "first_backup_at": a.get("first_backup_at", ""),
            "last_backup_at": a.get("last_backup_at", ""),
            "last_success_at": a.get("last_success_at", ""),
            "idle_days": _days_since(a.get("last_success_at") or t.get("last_run_at")),
            # 活跃
            "recent_count": act["count"],
            "recent_bytes": act["bytes"],
            # 引用
            "realtime": rf.get("realtime", False),
            "cdc": rf.get("cdc", False),
            "rt_running": rf.get("rt_running", False),
            "rt_health": rf.get("rt_health", ""),
            "sync_used": rf.get("sync", False),
            "clone_count": rf.get("clone_count", 0),
            # 可恢复
            "restore_count": rv.get("restore_count", 0),
            "restore_ok": rv.get("restore_ok", 0),
            "drill_count": rv.get("drill_count", 0),
            "drill_ok": rv.get("drill_ok", 0),
            # 占位：敏感等级由 scan 结果回填
            "sensitive_level": 0,
        })
    return assets


# ----------------------------- 价值评估 -----------------------------

def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


COLD_LEVELS = {
    "hot": {"label": "热数据", "badge": "danger", "desc": "近 7 天内有成功备份，处于活跃状态"},
    "warm": {"label": "温数据", "badge": "warning", "desc": "7~30 天内有更新，常规业务在用"},
    "cold": {"label": "冷数据", "badge": "info", "desc": "30~90 天无更新，可考虑降级存储"},
    "frozen": {"label": "冰封数据", "badge": "secondary", "desc": "超 90 天无更新或任务已停用，建议归档/清理"},
}


def score_asset(a: dict, max_bytes_ref: int = 1) -> dict:
    """多维度价值打分（0~100）+ 冷热分层 + 治理建议。"""
    dims: Dict[str, float] = {}

    # ① 规模（15）：相对全平台最大资产
    size_ratio = (a.get("total_bytes") or 0) / max(1, max_bytes_ref)
    dims["scale"] = round(_clamp(15 * (0.4 + 0.6 * size_ratio) if size_ratio else 0, 0, 15), 1)

    # ② 活跃度（25）：近 30 天备份批次 + 是否有增量链路 + 实时保护
    recent = a.get("recent_count") or 0
    act = min(1.0, recent / 30.0) * 15
    if a.get("incremental_count"):
        act += 5                       # 有增量链 = 变更频繁的生产系统
    if a.get("realtime") or a.get("cdc"):
        act += 5                       # 被实时保护 = 业务关键
    dims["activity"] = round(_clamp(act, 0, 25), 1)

    # ③ 保护完备性（20）
    prot = 0.0
    prot += 6 if a.get("scheduled") else 0
    prot += 3 if a.get("enabled") else 0
    prot += 3 if (a.get("retention_days") or 0) > 0 else 0
    tot = max(1, a.get("record_count") or 0)
    prot += 4 * ((a.get("success_count") or 0) / tot)
    prot += 4 * ((a.get("verified_count") or 0) / tot)
    dims["protection"] = round(_clamp(prot, 0, 20), 1)

    # ④ 可恢复性（15）
    rec = 0.0
    if a.get("restore_count"):
        rec += 6 * min(1.0, (a.get("restore_ok") or 0) / max(1, a["restore_count"]))
    if a.get("drill_count"):
        rec += 6 * min(1.0, (a.get("drill_ok") or 0) / max(1, a["drill_count"]))
    if a.get("clone_count"):
        rec += 3
    dims["recoverability"] = round(_clamp(rec, 0, 15), 1)

    # ⑤ 业务辐射（15）
    biz = 0.0
    biz += 4 if a.get("sync_used") else 0
    biz += 4 if (a.get("clone_count") or 0) else 0
    biz += 4 if a.get("realtime") or a.get("cdc") else 0
    biz += 3 if a.get("biz_system") else 0
    dims["business"] = round(_clamp(biz, 0, 15), 1)

    # ⑥ 合规敏感度（10）
    dims["compliance"] = round(_clamp((a.get("sensitive_level") or 0) * 2.5, 0, 10), 1)

    score = int(round(sum(dims.values())))
    grade = "高价值" if score >= 75 else ("中价值" if score >= 50 else "低价值")

    idle = a.get("idle_days")
    if a.get("enabled") is False:
        cold = "frozen"
    elif idle is None:
        cold = "frozen"              # 从来没有成功备份 = 无数据沉淀
    elif idle <= 7:
        cold = "hot"
    elif idle <= 30:
        cold = "warm"
    elif idle <= 90:
        cold = "cold"
    else:
        cold = "frozen"

    a["dims"] = dims
    a["value_score"] = score
    a["value_grade"] = grade
    a["cold_level"] = cold
    a["cold_label"] = COLD_LEVELS[cold]["label"]
    a["cold_badge"] = COLD_LEVELS[cold]["badge"]
    a["cold_desc"] = COLD_LEVELS[cold]["desc"]
    return a


def score_all(assets: List[dict]) -> List[dict]:
    ref = max([a.get("total_bytes") or 0 for a in assets], default=1)
    return [score_asset(a, ref) for a in assets]


# ----------------------------- 治理建议 -----------------------------

LOCAL_TIER_LABEL = {1: "L1 本地", 2: "L2 MinIO 热数据", 3: "L3 S3 冷数据"}


def governance(assets: List[dict], redun: dict, days: int = 30) -> Dict[str, Any]:
    """产出可执行的治理建议 + 量化收益。"""
    actions: List[dict] = []

    # 1. 冷/冰数据 → 降级 L3 冷存储，预计释放本地高端存储
    frozen = [a for a in assets if a.get("cold_level") in ("cold", "frozen")
              and (a.get("total_bytes") or 0) > 0]
    if frozen:
        bytes_sum = sum(a["total_bytes"] for a in frozen)
        actions.append({
            "id": "archive_to_cold",
            "title": f"{len(frozen)} 个冷/冰资产建议降级到 L3 冷存储",
            "level": "info",
            "reason": "超期未更新的备份继续占用本地高端存储，单位容量成本最高",
            "impact_items": len(frozen),
            "impact_bytes": bytes_sum,
            "assets": [{"task_id": a["task_id"], "name": a["name"],
                        "bytes": a["total_bytes"],
                        "idle_days": a.get("idle_days")} for a in frozen[:10]],
        })

    # 2. 停用任务仍占空间
    disabled = [a for a in assets if not a.get("enabled") and (a.get("total_bytes") or 0) > 0]
    if disabled:
        actions.append({
            "id": "cleanup_disabled",
            "title": f"{len(disabled)} 个已停用任务仍占用备份空间",
            "level": "warning",
            "reason": "任务停用后历史产物不再被保留策略扫描，成为无主占用",
            "impact_items": len(disabled),
            "impact_bytes": sum(a["total_bytes"] for a in disabled),
            "assets": [{"task_id": a["task_id"], "name": a["name"],
                        "bytes": a["total_bytes"]} for a in disabled[:10]],
        })

    # 3. 未接入保护（无调度）
    unsched = [a for a in assets if not a.get("scheduled")]
    if unsched:
        actions.append({
            "id": "attach_policy",
            "title": f"{len(unsched)} 个资产未配置定时保护",
            "level": "danger",
            "reason": "只能手动触发备份，一旦遗忘该数据处于裸奔状态",
            "impact_items": len(unsched),
            "impact_bytes": sum(a.get("total_bytes", 0) for a in unsched),
            "assets": [{"task_id": a["task_id"], "name": a["name"]} for a in unsched[:10]],
        })

    # 4. 从未做过恢复/演练
    never = [a for a in assets if a.get("record_count") and not a.get("restore_count")
             and not a.get("drill_count")]
    if never:
        actions.append({
            "id": "add_verify",
            "title": f"{len(never)} 个资产从未做过恢复验证",
            "level": "warning",
            "reason": "没有恢复/演练记录，无法证明备份可恢复（合规审计常见扣分项）",
            "impact_items": len(never),
            "impact_bytes": sum(a.get("total_bytes", 0) for a in never),
            "assets": [{"task_id": a["task_id"], "name": a["name"]} for a in never[:10]],
        })

    # 5. 重复产物（对象级重删）
    if redun.get("group_count"):
        actions.append({
            "id": "dedup",
            "title": f"存在 {redun['group_count']} 组完全相同的重复产物",
            "level": "info",
            "reason": "同一 sha256 被多次落盘，开启全局重删可释放这部分空间",
            "impact_items": redun["group_count"],
            "impact_bytes": redun["wasted_bytes"],
            "assets": [{"checksum": g["checksum"], "count": g["count"],
                        "bytes": g["wasted_bytes"]} for g in redun["groups"][:10]],
        })

    # 6. 备份成功率告警
    failing = [a for a in assets if a.get("record_count")
               and (a.get("success_count") or 0) / max(1, a["record_count"]) < 0.8]
    if failing:
        actions.append({
            "id": "fix_failure",
            "title": f"{len(failing)} 个资产备份成功率低于 80%",
            "level": "danger",
            "reason": "失败占比过高意味着该资产实际处于无有效备份状态",
            "impact_items": len(failing),
            "impact_bytes": 0,
            "assets": [{"task_id": a["task_id"], "name": a["name"],
                        "success_rate": _pct(100.0 * (a.get("success_count") or 0)
                                             / max(1, a["record_count"]))}
                       for a in failing[:10]],
        })

    actions.sort(key=lambda x: ({"danger": 0, "warning": 1, "info": 2}.get(x["level"], 3),
                                -x["impact_bytes"]))
    return {
        "actions": actions,
        "total_reclaimable_bytes": sum(a["impact_bytes"] for a in actions
                                       if a["id"] in ("archive_to_cold", "cleanup_disabled", "dedup")),
    }


# ----------------------------- 汇总出口 -----------------------------

def build_inventory(days: int = 30, sensitive: Dict[int, int] = None) -> Dict[str, Any]:
    """一次性产出：资产清单 + 汇总 KPI + 分布 + 趋势 + 重删 + 治理建议。"""
    sensitive = sensitive or {}
    assets = build_assets(days)
    for a in assets:
        a["sensitive_level"] = int(sensitive.get(a["task_id"], 0) or 0)
    assets = score_all(assets)
    redun = redundancy()

    total_bytes = sum(a["total_bytes"] for a in assets)
    instances = {}
    for a in assets:
        key = a["instance"] or "—"
        inst = instances.setdefault(key, {"instance": key, "db_types": set(),
                                          "assets": 0, "bytes": 0})
        inst["db_types"].add(a["db_type"])
        inst["assets"] += 1
        inst["bytes"] += a["total_bytes"]
    instance_list = [{
        "instance": k, "db_types": sorted(v["db_types"]), "asset_count": v["assets"],
        "bytes": v["bytes"],
    } for k, v in instances.items()]
    instance_list.sort(key=lambda x: -x["bytes"])

    db_types: Dict[str, dict] = {}
    for a in assets:
        d = db_types.setdefault(a["db_type"], {"db_type": a["db_type"],
                                               "asset_count": 0, "bytes": 0,
                                               "record_count": 0})
        d["asset_count"] += 1
        d["bytes"] += a["total_bytes"]
        d["record_count"] += a["record_count"]
    db_type_list = sorted(db_types.values(), key=lambda x: -x["bytes"])

    pt = trend(days)
    recent_30 = sum(p["bytes"] for p in pt)
    prev_start = datetime.now() - timedelta(days=days * 2)
    prev_row = _q("SELECT SUM(size_bytes) AS bytes FROM backup_records "
                  "WHERE status='success' AND substr(started_at,1,19)>=? "
                  "AND substr(started_at,1,19)<?",
                  (prev_start.strftime("%Y-%m-%d %H:%M:%S"),
                   (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")))
    prev_30 = int((prev_row[0]["bytes"] if prev_row else 0) or 0)
    growth = None
    # 上期样本太小（<1MB）时增长率无统计意义，返回 None 由前端显示"—"
    if prev_30 >= 1024 * 1024:
        growth = _pct(max(-100.0, min(999.0, 100.0 * (recent_30 - prev_30) / prev_30)))

    cold_dist = {"hot": 0, "warm": 0, "cold": 0, "frozen": 0}
    grade_dist = {"高价值": 0, "中价值": 0, "低价值": 0}
    for a in assets:
        cold_dist[a["cold_level"]] = cold_dist.get(a["cold_level"], 0) + 1
        grade_dist[a["value_grade"]] = grade_dist.get(a["value_grade"], 0) + 1

    return {
        "days": days,
        "summary": {
            "asset_count": len(assets),
            "instance_count": len(instance_list),
            "record_count": sum(a["record_count"] for a in assets),
            "total_bytes": total_bytes,
            "recent_bytes": recent_30,
            "growth_pct": growth,
            "protected_assets": sum(1 for a in assets if a["scheduled"] and a["enabled"]),
            "protected_bytes": sum(a["total_bytes"] for a in assets
                                   if a["scheduled"] and a["enabled"]),
            "verified_count": sum(a["verified_count"] for a in assets),
            "avg_score": int(round(sum(a["value_score"] for a in assets) / max(1, len(assets)))),
            "max_sensitive_level": max([a["sensitive_level"] for a in assets] + [0]),
        },
        "assets": assets,
        "instances": instance_list,
        "db_type_dist": db_type_list,
        "tier_dist": _tier_distribution(),
        "trend": pt,
        "redundancy": redun,
        "cold_dist": cold_dist,
        "grade_dist": grade_dist,
        "governance": governance(assets, redun, days),
        "cold_levels": COLD_LEVELS,
    }


if __name__ == "__main__":  # 手工验证
    import json
    print(json.dumps(build_inventory(30)["summary"], ensure_ascii=False, indent=2))
