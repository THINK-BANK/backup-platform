# -*- coding: utf-8 -*-
"""目标端「无 Agent」能力 API（capabilities 声明 + 免装取证）。

路由遵循 docs/api_conventions.md（双前缀注册、吸纳桔合 split）：

- ``GET /api/v1/capabilities``          通道清单与侵入等级（A0–A3 / 禁止项 X）
- ``GET /api/v1/targets/<id>/no-agent-audit``  对纳管 SSH 主机做只读取证
- ``GET /api/v1/agentless/tasks``       每个任务本次走了哪条通道、最高侵入等级
- ``GET /api/v1/tasks/<id>/agentless-plan``  单任务的通道计划（含目标端前置要求）
"""
from __future__ import annotations

from collections import Counter

from flask import jsonify, request

from auth import login_required
from core import models, ssh_hosts
from core.agentless import audit as _audit
from core.agentless import channels as _channels
from core.agentless import plan as _plan
from . import api_bp, contract

#: 取证超时上限（避免工控机/慢主机把请求拖住）
_MAX_TIMEOUT = 120


@api_bp.route("/capabilities", methods=["GET"])
@login_required
def api_capabilities():
    """列出平台到目标端的全部通道、侵入等级与目标端前置配置要求。"""
    return jsonify(_channels.capabilities())


@api_bp.route("/targets/<int:host_id>/no-agent-audit", methods=["GET"])
@login_required
def api_no_agent_audit(host_id: int):
    """对一台纳管主机做目标端免装取证（只读）。

    返回 ``verdict``：``PASS`` 无残留 / ``WARN`` 有已清理但他提醒项 /
    ``FAIL`` 出现可归因于本平台的常驻或残留 / ``UNKNOWN`` 取证执行失败（不可采信）。
    """
    host = ssh_hosts.get_host(host_id, include_secret=True)
    if not host:
        return contract.error_response("AIDBM-1004", f"纳管主机不存在: {host_id}")

    timeout = request.args.get("timeout", type=int) or 60
    timeout = max(5, min(int(timeout), _MAX_TIMEOUT))
    try:
        report = _audit.audit_host(host, timeout=timeout)
    except Exception as e:  # SSH/网络类失败：如实报错，不降级为 PASS
        return contract.error_response(
            "AIDBM-4001", f"SSH 取证失败: {e}",
            details={"host": host.get("host_key"), "hint":
                     "确认主机在线、账号可用且允许非交互执行"})
    if report.get("verdict") == "UNKNOWN":
        return contract.error_response(
            "AIDBM-4001", "取证未取回有效输出，结论不可采信",
            details={"host": report.get("host"),
                     "returncode": report.get("returncode"),
                     "stderr": report.get("stderr")})
    report["request_timeout"] = timeout
    return jsonify(report)


def _ssh_cache() -> dict:
    """{host_id: 主机字典}（不含明文口令，判定只需要 host_key）。"""
    return {int(h["id"]): h for h in ssh_hosts.list_hosts(include_secret=False)}


@api_bp.route("/agentless/tasks", methods=["GET"])
@login_required
def api_agentless_tasks():
    """任务 × 本次通道 × 侵入等级（无 Agent 面板的数据源）。"""
    db_type = request.args.get("db_type") or None
    plans = _plan.plan_tasks(models.list_tasks(db_type=db_type), _ssh_cache())
    return jsonify({"items": plans, "summary": _plan.summarize(plans),
                    "levels": _channels.invasion_levels()})


@api_bp.route("/tasks/<int:task_id>/agentless-plan", methods=["GET"])
@login_required
def api_task_agentless_plan(task_id: int):
    """单任务的通道计划：走哪几条通道、最高侵入等级、目标端需开哪些配置。"""
    import json as _json

    task = models.get_task(task_id)
    if not task:
        return contract.error_response("AIDBM-1004", f"任务不存在: {task_id}")
    extra = task.get("extra_options")
    if isinstance(extra, str):
        try:
            extra = _json.loads(extra) if extra else {}
        except Exception:
            extra = {}
    hid = (extra or {}).get("ssh_host_id") or task.get("ssh_host_id")
    host = ssh_hosts.get_host(int(hid), include_secret=False) if hid else None
    return jsonify(_plan.plan_for_task(task, host))
