# -*- coding: utf-8 -*-
"""
工具注册表 + 7 个 MVP 工具定义。

每个工具的 executor 现在直接调用本地 Python API（models / scheduler /
inspection / db），不再走内部 HTTP 调用，避免 session/cookie 认证问题并
显著降低延迟。
"""

import os
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional, Any


@dataclass
class Tool:
    """单个工具定义。

    Attributes:
        name: 工具名（唯一标识）
        description: 工具描述（给 LLM 看的）
        parameters: 参数 schema（OpenAI tools JSON Schema 格式）
        requires_confirm: 是否需要用户二次确认才执行（静态；条件确认请用 confirm_if）
        confirm_if: 条件确认回调 (args) -> bool，返回 True 时要求二次确认。
            用于"同一工具不同参数风险不同"的场景（如巡检 quick 免确认、full 需确认）。
        risk_level: 风险等级 low/medium/high，用于前端步骤卡着色与审计日志
        api_method: HTTP 方法（兼容旧字段，保留）
        api_path: 内部 API 端点路径（兼容旧字段，保留）
        executor: 实际执行函数 (args: dict, context: dict) -> dict
    """
    name: str
    description: str
    parameters: Dict[str, Any]
    requires_confirm: bool = False
    confirm_if: Optional[Callable[[Dict], bool]] = None
    risk_level: str = "low"
    api_method: str = "GET"
    api_path: str = ""
    executor: Optional[Callable[[Dict, Dict], Dict]] = None


def needs_confirm(tool: "Tool", args: Optional[Dict] = None) -> bool:
    """判定一次工具调用是否需要用户二次确认。

    策略（借鉴业界 agent 的分级授权思路）：
    - 高风险操作（恢复、删除、全量巡检等）：必须确认
    - 低风险操作（查询、备份）：用户显式下达指令即直接执行，减少无谓打断
    - 部署侧可通过配置 AI_AGENT_EXEC_MODE=always_confirm 切换为"全部确认"的保守模式

    Args:
        tool: 工具定义
        args: 本次调用参数（条件确认用）

    Returns:
        True 表示需要用户确认后才执行
    """
    mode = "auto"
    try:
        import config
        mode = str(getattr(config, "AI_AGENT_EXEC_MODE", "auto") or "auto").lower()
    except Exception:
        pass
    if mode == "always_confirm":
        return True
    if mode == "auto_execute":
        return False
    if tool.confirm_if is not None:
        try:
            return bool(tool.confirm_if(args or {}))
        except Exception:
            return True
    return bool(tool.requires_confirm)


class ToolRegistry:
    """工具注册表：注册、查找、导出工具列表。"""

    def __init__(self) -> None:
        self._tools: Dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        """注册一个工具。重复注册会覆盖。"""
        self._tools[tool.name] = tool

    def get(self, name: str) -> Optional[Tool]:
        """按名称查找工具，不存在返回 None。"""
        return self._tools.get(name)

    def list_all(self) -> List[Tool]:
        """返回所有已注册工具列表。"""
        return list(self._tools.values())

    def to_openai_tools(self) -> List[Dict[str, Any]]:
        """导出为 OpenAI function calling 格式的 tools 列表。

        Returns:
            [{"type": "function", "function": {"name", "description", "parameters"}}]
        """
        result = []
        for tool in self._tools.values():
            result.append({
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                },
            })
        return result

    def tools_description_for_prompt(self) -> str:
        """生成给 ReAct prompt 的工具描述文本段。

        格式：
        - tool_name: 描述（⚠需确认）。参数: {param_schema}
        """
        lines = []
        for tool in self._tools.values():
            if tool.confirm_if is not None:
                confirm_mark = "（⚠特定参数需用户确认）"
            elif tool.requires_confirm:
                confirm_mark = "（⚠需用户确认）"
            else:
                confirm_mark = "（可直接执行，无需确认）"
            # 从 parameters 中提取简洁的参数摘要
            props = tool.parameters.get("properties", {})
            required = tool.parameters.get("required", [])
            param_parts = []
            for pname, pdef in props.items():
                ptype = pdef.get("type", "string")
                req_mark = "(必填)" if pname in required else ""
                desc = pdef.get("description", "")
                param_parts.append(f"{pname}:{ptype}{req_mark}")
            param_str = ", ".join(param_parts) if param_parts else "无参数"
            lines.append(f"- {tool.name}: {tool.description}{confirm_mark}。参数: {param_str}")
        return "\n".join(lines)


# ======================== 本地工具执行器 ========================

def _safe_int(value, default=None):
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (ValueError, TypeError):
        return default


def _task_brief(task: Dict) -> Dict:
    """任务摘要（用于歧义澄清候选列表与回答展示）。"""
    return {
        "id": task.get("id"),
        "name": task.get("name"),
        "db_type": task.get("db_type_display") or task.get("db_type"),
        "host": task.get("host"),
        "port": task.get("port"),
        "db_name": task.get("db_name"),
        "backup_type": task.get("backup_type"),
        "enabled": bool(task.get("enabled")),
        "last_status": task.get("last_status"),
    }


def _match_tasks_scored(keyword: str, limit: int = 10) -> List[tuple]:
    """按自然语言关键字模糊匹配备份任务，返回 (相关度分数, 任务) 列表。

    评分口径（越高越相关）：
        100 任务名完全相同
         90 库名/实例名完全相同
         80 任务名包含关键字
         70 库名/实例名包含关键字
         50 业务系统包含关键字
         45 实例别名包含关键字
         35 主机包含关键字
         30 数据库类型名包含关键字
         20 数据库类型中文别名命中
    """
    from core import models
    kw = (keyword or "").strip().lower()
    if not kw:
        return []
    try:
        tasks = models.list_tasks()
    except Exception:
        return []

    scored: List[tuple] = []
    for t in tasks:
        name = str(t.get("name") or "").lower()
        db_name = str(t.get("db_name") or "").lower()
        biz = str(t.get("biz_system") or "").lower()
        host = str(t.get("host") or "").lower()
        inst = str(t.get("instance_name") or "").lower()
        dtype = str(t.get("db_type") or "").lower()
        dtype_disp = str(t.get("db_type_display") or "").lower()

        score = 0
        if name == kw:
            score = 100
        elif db_name and db_name == kw:
            score = 90
        elif name and kw in name:
            score = 80
        elif db_name and kw in db_name:
            score = 70
        elif biz and kw in biz:
            score = 50
        elif inst and kw in inst:
            score = 45
        elif host and kw in host:
            score = 35
        elif (dtype and kw in dtype) or (dtype_disp and kw in dtype_disp):
            score = 30
        else:
            alias = _DB_TYPE_ALIASES.get(dtype, "")
            if alias and alias in kw:
                score = 20
        if score > 0:
            scored.append((score, t))

    scored.sort(key=lambda x: (-x[0], -(x[1].get("id") or 0)))
    return scored[:limit]


def _match_tasks(keyword: str, limit: int = 10) -> List[Dict]:
    """按自然语言关键字模糊匹配备份任务（只返回任务列表）。"""
    return [t for _, t in _match_tasks_scored(keyword, limit)]


# 备份类型强意图词（歧义消解用：用户说"逻辑备份"就不该选中物理备份任务）
_INTENT_TOKENS = {
    "physical": ("物理", "phys", "xtrabackup", "rman", "basebackup", "mariabackup"),
    "logical": ("逻辑", "logic", "dump"),
    "incremental": ("增量", "incr"),
    "full": ("全量", "全实例"),
    "realtime": ("实时", "cdc", "binlog"),
    "object_storage": ("对象存储", "oss", "s3", "bucket"),
    "file": ("文件备份", "文件"),
}
# 通用意图词（权重较低，仅用于区分"备份/恢复"等大类）
_INTENT_WEAK_TOKENS = ("备份", "backup")

# 数据库类型中文别名（用于自然语言匹配）
_DB_TYPE_ALIASES = {
    "mysql": "mysql",
    "mariadb": "mariadb",
    "postgresql": "postgres",
    "oracle": "oracle",
    "sqlserver": "sqlserver",
    "dameng": "达梦",
    "kingbase": "金仓",
    "mongodb": "mongo",
    "redis": "redis",
    "opengauss": "opengauss",
    "object_storage": "对象存储",
    "file": "文件",
}


def _run_backup_task_executor(args: Dict, context: Dict) -> Dict:
    """立即运行备份任务。

    支持两种定位方式（自然语言驱动）：
    - task_id：任务 ID，直接执行
    - task_name：任务名/业务系统/主机/库名关键字，唯一命中则执行；
      多个命中时返回候选列表让用户澄清（绝不猜测执行）；无命中时给出纠正提示。
    """
    from core import scheduler
    task_id = _safe_int(args.get("task_id"))
    task_name = str(args.get("task_name") or "").strip()
    backup_type = args.get("task_type") or args.get("backup_type") or "full"

    # 1) 通过名称模糊定位任务
    if not task_id and task_name:
        scored = _match_tasks_scored(task_name, limit=10)
        candidates = [t for _, t in scored]
        if not candidates:
            try:
                from core import models
                sample = [f"#{t.get('id')} {t.get('name')}" for t in models.list_tasks()[:8]]
            except Exception:
                sample = []
            return {
                "ok": False,
                "error": f"没有找到与「{task_name}」匹配的备份任务",
                "hint": "请确认任务名或库名，也可先用 list_tasks 查看全部任务",
                "available_tasks": sample,
                "tool": "run_backup_task",
                "args": args,
            }
        if len(candidates) > 1:
            top_score = scored[0][0]
            second_score = scored[1][0]
            if top_score >= 70 and top_score > second_score:
                # a) 明显最佳：强匹配且领先次优 → 直接执行，避免无谓打断
                candidates = [candidates[0]]
            else:
                # b) 相关度相同：用意图词（逻辑/物理/增量/实时…）消歧
                ranked, unique_intent = _apply_intent_tiebreak(
                    scored, "{} {}".format(task_name, backup_type))
                if unique_intent:
                    candidates = [ranked[0][1]]
                else:
                    # c) 真歧义：交回用户澄清，绝不猜测执行
                    briefs = []
                    for idx, (sc, t) in enumerate(ranked[:6]):
                        brief = _task_brief(t)
                        brief["match_score"] = sc
                        brief["recommended"] = idx == 0
                        briefs.append(brief)
                    names = "、".join(f"#{b['id']} {b['name']}" for b in briefs[:6])
                    return {
                        "ok": False,
                        "needs_clarify": True,
                        "error": f"「{task_name}」匹配到 {len(briefs)} 个备份任务，需要你确认执行哪一个：{names}",
                        "data": briefs,
                        "candidates": briefs,
                        "tool": "run_backup_task",
                        "args": args,
                    }
        matched_task = candidates[0]
        task_id = matched_task.get("id")
        # 关键字命中的任务名回填，便于回答中直接展示
        args = dict(args)
        args.setdefault("task_name", matched_task.get("name"))

    if not task_id:
        return {
            "ok": False,
            "error": "缺少 task_id 或 task_name，无法确定要执行的备份任务",
            "tool": "run_backup_task",
            "args": args,
        }

    if backup_type not in ("full", "incremental", "differential", "log"):
        backup_type = "full"

    try:
        started = datetime.now()
        record = scheduler.run_task_now(task_id, backup_type=backup_type, operator="ai_agent")
        elapsed = round((datetime.now() - started).total_seconds(), 1)
        if not record:
            return {"ok": False, "error": f"任务 #{task_id} 不存在或启动失败",
                    "tool": "run_backup_task", "args": args}

        # 文件备份等异步 accepted 模式
        if isinstance(record, dict) and record.get("accepted"):
            return {
                "ok": True,
                "message": f"备份任务 #{task_id} 已提交后台执行",
                "data": {"task_id": task_id, "status": "running", "accepted": True},
                "tool": "run_backup_task",
                "args": args,
            }

        status = (record.get("status") or "unknown").lower()
        ok = status == "success"
        # 关键：task_name / db_type / duration 一并回传，保证"无需二次查询"即可给出完整回答
        task_name_final = record.get("task_name") or record.get("biz_label")
        if not task_name_final:
            try:
                from core import models as _models
                t = _models.get_task(task_id)
                task_name_final = (t or {}).get("name")
            except Exception:
                task_name_final = None
        data = {
            "task_id": task_id,
            "task_name": task_name_final,
            "backup_type": backup_type,
            "status": record.get("status"),
            "record_id": record.get("id"),
            "started_at": record.get("started_at"),
            "finished_at": record.get("finished_at"),
            "duration_sec": elapsed,
            "size_bytes": record.get("size_bytes"),
            "size_human": _human_size(record.get("size_bytes")),
            "backup_path": record.get("backup_path"),
            "message": record.get("message") or record.get("error_msg"),
        }
        return {
            "ok": ok,
            "message": f"备份任务「{task_name_final or ('#' + str(task_id))}」执行{('成功' if ok else '失败')}",
            "data": data,
            "tool": "run_backup_task",
            "args": args,
        }
    except Exception as e:
        return {"ok": False, "error": str(e) or type(e).__name__,
                "tool": "run_backup_task", "args": args}


def _human_size(size) -> str:
    """字节数转可读字符串。"""
    try:
        n = float(size or 0)
    except (TypeError, ValueError):
        return "-"
    if n <= 0:
        return "0 B"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.2f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.2f} TB"


def _run_inspection_executor(args: Dict, context: Dict) -> Dict:
    """执行巡检并返回汇总结果。"""
    from core import inspection
    scope = args.get("scope", "quick")
    raw_ids = args.get("task_ids") or ""
    task_ids = None
    if raw_ids:
        ids = []
        for part in str(raw_ids).split(","):
            part = part.strip()
            if part:
                try:
                    ids.append(int(part))
                except ValueError:
                    pass
        if len(ids) == 1:
            task_ids = ids[0]
        elif len(ids) > 1:
            # inspection.run_inspection 只支持单个 task_id 或 None
            # 这里取第一个并提示
            task_ids = ids[0]
    try:
        summary = inspection.run_inspection(task_id=task_ids, triggered_by="ai_agent")
        # 巡检"执行成功"与"巡检发现的问题"是两回事：只要巡检流程跑完、拿到汇总，
        # 就算执行成功（ok=True）；具体通过/警告/失败项数由 message/data 体现给用户。
        ok = summary is not None and "total" in summary
        total = summary.get("total", 0)
        passed = summary.get("pass", 0)
        warned = summary.get("warn", 0)
        failed = summary.get("fail", 0)
        return {
            "ok": ok,
            "message": f"巡检已执行完成：共 {total} 项，通过 {passed}，警告 {warned}，失败 {failed}",
            "data": summary,
            "tool": "run_inspection",
            "args": args,
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "tool": "run_inspection", "args": args}


def _list_recent_records_executor(args: Dict, context: Dict) -> Dict:
    """查询最近的备份执行记录，支持只看失败、按关键字/状态过滤。"""
    from core import models
    task_id = _safe_int(args.get("task_id"))
    limit = _safe_int(args.get("limit"), 20)
    keyword = str(args.get("keyword") or "").strip().lower()
    only_failed = str(args.get("only_failed") or "").strip().lower() in ("1", "true", "yes", "是", "失败")
    status_filter = str(args.get("status") or "").strip().lower()
    try:
        # 关键字/失败过滤需要更大的取数窗口，否则过滤后可能为空
        fetch_limit = 200 if (keyword or only_failed or status_filter) else limit
        rows = models.list_records(task_id=task_id, limit=fetch_limit)
        # 精简字段，避免 LLM token 过大
        simplified = []
        for r in rows:
            name = r.get("task_name") or r.get("biz_label") or "-"
            status = (r.get("status") or "").lower()
            if keyword and keyword not in str(name).lower():
                continue
            if only_failed and status not in ("failed", "error"):
                continue
            if status_filter and status != status_filter:
                continue
            simplified.append({
                "id": r.get("id"),
                "task_name": name,
                "db_type": r.get("db_type_display") or r.get("db_type"),
                "backup_type": r.get("backup_type_display") or r.get("backup_type"),
                "status": r.get("status"),
                "size_bytes": r.get("size_bytes"),
                "size_human": _human_size(r.get("size_bytes")),
                "started_at": r.get("started_at"),
                "finished_at": r.get("finished_at"),
                "is_simulated": bool(r.get("is_simulated")),
                "message": r.get("message"),
            })
            if len(simplified) >= limit:
                break
        msg = f"查询到 {len(simplified)} 条备份记录"
        if only_failed:
            msg = f"查询到 {len(simplified)} 条失败的备份记录"
        return {
            "ok": True,
            "message": msg,
            "data": simplified,
            "tool": "list_recent_records",
            "args": args,
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "tool": "list_recent_records", "args": args}


def _list_alert_predictions_executor(args: Dict, context: Dict) -> Dict:
    """查询 AI 预测告警列表。"""
    from core import models
    metric = args.get("metric")
    days = _safe_int(args.get("days"), 7)
    try:
        rows = models.list_alert_predictions(metric=metric, limit=200)
        # 按 predicted_at 过滤最近 N 天
        if days and days > 0:
            cutoff = datetime.now(timezone.utc) - timedelta(days=days)
            filtered = []
            for r in rows:
                pa = r.get("predicted_at")
                if pa:
                    try:
                        # 兼容带 Z / +00:00 的 ISO 格式
                        if isinstance(pa, str):
                            pa_dt = datetime.fromisoformat(pa.replace("Z", "+00:00"))
                            if pa_dt.tzinfo is None:
                                pa_dt = pa_dt.replace(tzinfo=timezone.utc)
                            if pa_dt >= cutoff:
                                filtered.append(r)
                        else:
                            filtered.append(r)
                    except Exception:
                        filtered.append(r)
                else:
                    filtered.append(r)
            rows = filtered
        simplified = []
        for r in rows[:50]:
            simplified.append({
                "id": r.get("id"),
                "metric": r.get("metric"),
                "risk_level": r.get("risk_level"),
                "risk_score": r.get("risk_score"),
                "predicted_at": r.get("predicted_at"),
                "predicted_content": r.get("predicted_content"),
                "basis": r.get("basis"),
            })
        return {
            "ok": True,
            "message": f"查询到 {len(simplified)} 条 AI 预测告警",
            "data": simplified,
            "tool": "list_alert_predictions",
            "args": args,
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "tool": "list_alert_predictions", "args": args}


def _get_storage_usage_executor(args: Dict, context: Dict) -> Dict:
    """查询本地存储空间用量；如指定 target_id 则查该目标，否则查默认本地目标。"""
    from core import db
    target_id = _safe_int(args.get("target_id"))
    try:
        if target_id:
            row = db.query_one("SELECT endpoint, type, name FROM storage_targets WHERE id=?", (target_id,))
        else:
            row = db.query_one(
                "SELECT endpoint, type, name FROM storage_targets WHERE type='local' AND enabled=1 "
                "ORDER BY is_default DESC, id LIMIT 1"
            )
        path = "./backups"
        target_name = "默认本地存储"
        if row:
            path = row.get("endpoint") or path
            target_name = row.get("name") or target_name
        path = os.path.abspath(path)
        du = shutil.disk_usage(path)
        used_percent = round(du.used / du.total * 100, 1) if du.total else 0
        data = {
            "target_id": target_id,
            "target_name": target_name,
            "path": path,
            "total_bytes": du.total,
            "used_bytes": du.used,
            "free_bytes": du.free,
            "total_gb": round(du.total / (1024 ** 3), 2),
            "used_gb": round(du.used / (1024 ** 3), 2),
            "free_gb": round(du.free / (1024 ** 3), 2),
            "used_percent": used_percent,
        }
        return {
            "ok": True,
            "message": f"存储 {target_name} 使用率 {used_percent}%",
            "data": data,
            "tool": "get_storage_usage",
            "args": args,
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "tool": "get_storage_usage", "args": args}


def _list_tasks_executor(args: Dict, context: Dict) -> Dict:
    """列出所有备份任务，可按类型/启用状态过滤。"""
    from core import models
    db_type = args.get("type") or args.get("db_type")
    enabled_raw = args.get("enabled")
    enabled = None
    if enabled_raw is not None:
        enabled = str(enabled_raw).strip() in ("1", "true", "True", "是", "yes")
    try:
        rows = models.list_tasks(db_type=db_type, enabled=enabled)
        simplified = []
        for r in rows:
            simplified.append({
                "id": r.get("id"),
                "name": r.get("name"),
                "db_type": r.get("db_type"),
                "backup_mode": r.get("backup_mode"),
                "host": r.get("host"),
                "port": r.get("port"),
                "enabled": bool(r.get("enabled")),
                "last_status": r.get("last_status"),
                "last_run_at": r.get("last_run_at"),
                "policy_name": r.get("policy_name"),
            })
        return {
            "ok": True,
            "message": f"共有 {len(simplified)} 个备份任务",
            "data": simplified,
            "tool": "list_tasks",
            "args": args,
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "tool": "list_tasks", "args": args}


def _get_inspection_report_executor(args: Dict, context: Dict) -> Dict:
    """获取最新巡检报告详情。"""
    from core import models, db
    record_id = _safe_int(args.get("record_id"))
    try:
        if record_id:
            row = db.query_one("SELECT * FROM inspection_records WHERE id=?", (record_id,))
        else:
            row = db.query_one("SELECT * FROM inspection_records ORDER BY id DESC LIMIT 1")
        if not row:
            return {"ok": False, "error": "暂无巡检记录", "tool": "get_inspection_report", "args": args}
        detail = row.get("detail") or "{}"
        if isinstance(detail, str):
            try:
                detail = json.loads(detail)
            except Exception:
                pass
        data = {
            "id": row.get("id"),
            "task_name": row.get("task_name"),
            "db_type": row.get("db_type"),
            "status": row.get("status"),
            "started_at": row.get("started_at"),
            "finished_at": row.get("finished_at"),
            "triggered_by": row.get("triggered_by"),
            "detail": detail,
        }
        return {
            "ok": True,
            "message": f"最新巡检记录 #{data['id']} 状态: {data['status']}",
            "data": data,
            "tool": "get_inspection_report",
            "args": args,
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "tool": "get_inspection_report", "args": args}


# ---- 工具实例 ----

TOOL_DEFINITIONS: List[Tool] = [
    Tool(
        name="run_backup_task",
        description=(
            "立即执行一次备份任务。可用 task_name 直接传用户口语中的任务名/业务系统/库名/主机"
            "（如 \"bpm1\"、\"生产MySQL\"、\"对象存储附件桶\"），系统会自动定位任务；"
            "唯一命中直接执行，多个命中会返回候选列表要求澄清，绝不猜测执行"
        ),
        parameters={
            "type": "object",
            "properties": {
                "task_name": {
                    "type": "string",
                    "description": "任务名/业务系统/库名/主机关键字（推荐；用户用自然语言描述时使用）",
                },
                "task_id": {
                    "type": "string",
                    "description": "备份任务 ID（用户明确给出 ID 时使用）",
                },
                "task_type": {
                    "type": "string",
                    "description": "备份类型（full/incremental/log），默认 full",
                },
            },
        },
        # 备份是低风险操作（只读源库、写平台存储），用户显式下达指令即直接执行
        requires_confirm=False,
        risk_level="low",
        api_method="POST",
        api_path="/api/tasks/{task_id}/run",
        executor=_run_backup_task_executor,
    ),
    Tool(
        name="run_inspection",
        description="立即执行巡检，可指定任务或全量巡检；quick 快速巡检直接执行，full 全量巡检需用户确认",
        parameters={
            "type": "object",
            "properties": {
                "task_ids": {
                    "type": "string",
                    "description": "巡检任务 ID 列表（逗号分隔），可选",
                },
                "scope": {
                    "type": "string",
                    "enum": ["quick", "full"],
                    "description": "巡检范围：quick 快速巡检 / full 全量巡检",
                },
            },
        },
        # 条件确认：快速巡检免确认；全量巡检对数据库有性能影响，必须确认
        requires_confirm=False,
        confirm_if=lambda args: str(args.get("scope") or "quick").lower() == "full",
        risk_level="medium",
        api_method="POST",
        api_path="/api/inspection/run",
        executor=_run_inspection_executor,
    ),
    Tool(
        name="list_recent_records",
        description="查询最近的备份执行记录，可只看失败记录或按任务名关键字过滤",
        parameters={
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "按任务 ID 过滤，可选",
                },
                "keyword": {
                    "type": "string",
                    "description": "按任务名关键字过滤（如 \"bpm1\"），可选",
                },
                "status": {
                    "type": "string",
                    "description": "按状态过滤（success/failed/running），可选",
                },
                "only_failed": {
                    "type": "string",
                    "description": "仅返回失败记录（true/false），可选",
                },
                "limit": {
                    "type": "integer",
                    "description": "返回记录数量上限，默认 20",
                    "default": 20,
                },
            },
        },
        requires_confirm=False,
        risk_level="low",
        api_method="GET",
        api_path="/api/records",
        executor=_list_recent_records_executor,
    ),
    Tool(
        name="list_alert_predictions",
        description="查询 AI 预测告警列表",
        parameters={
            "type": "object",
            "properties": {
                "metric": {
                    "type": "string",
                    "description": "按指标类型过滤（backup_fail/storage_full/link_degraded/drill_overdue/rpo_breach），可选",
                },
                "days": {
                    "type": "integer",
                    "description": "查询最近 N 天的预测，默认 7",
                    "default": 7,
                },
            },
        },
        requires_confirm=False,
        api_method="GET",
        api_path="/api/alerts/predictions",
        executor=_list_alert_predictions_executor,
    ),
    Tool(
        name="get_storage_usage",
        description="查询本地存储空间用量，不指定 target_id 时查默认本地存储",
        parameters={
            "type": "object",
            "properties": {
                "target_id": {
                    "type": "string",
                    "description": "存储目标 ID，可选（不指定则查默认本地存储）",
                },
            },
        },
        requires_confirm=False,
        api_method="GET",
        api_path="/api/storage/usage",
        executor=_get_storage_usage_executor,
    ),
    Tool(
        name="list_tasks",
        description="列出备份任务；可用 keyword 按任务名/库名/主机模糊搜索（推荐），也可按数据库类型/启用状态过滤",
        parameters={
            "type": "object",
            "properties": {
                "keyword": {
                    "type": "string",
                    "description": "任务名/业务系统/库名/主机关键字，可选",
                },
                "type": {
                    "type": "string",
                    "description": "按数据库类型过滤（mysql/postgresql/oracle 等），可选",
                },
                "enabled": {
                    "type": "string",
                    "description": "按启用状态过滤（1=启用/0=禁用），可选",
                },
            },
        },
        requires_confirm=False,
        risk_level="low",
        api_method="GET",
        api_path="/api/tasks",
        executor=_list_tasks_executor,
    ),
    Tool(
        name="get_inspection_report",
        description="获取最新巡检报告详情",
        parameters={
            "type": "object",
            "properties": {
                "record_id": {
                    "type": "string",
                    "description": "巡检记录 ID，可选（不指定则取最新一条）",
                },
            },
        },
        requires_confirm=False,
        api_method="GET",
        api_path="/api/inspection/records",
        executor=_get_inspection_report_executor,
    ),
]


def create_default_registry() -> ToolRegistry:
    """创建并注册 7 个 MVP 工具的默认注册表。"""
    registry = ToolRegistry()
    for tool_def in TOOL_DEFINITIONS:
        registry.register(tool_def)
    return registry
