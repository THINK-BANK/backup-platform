# -*- coding: utf-8 -*-
"""统一业务错误码（API 三段式契约）。

对外契约（见 docs/api_conventions.md §3）::

    {
      "code":    "AIDBM-1004",        # 稳定业务错误码，与 HTTP 状态解耦
      "message": "记录不存在",          # 面向人的一句话原因
      "details": {"record_id": 42},   # 机器可读上下文（可为空对象）
      "error":   "记录不存在",          # 历史字段，等于 message（向后兼容）
      "success": false
    }

命名规则 ``AIDBM-<域><序号>``：

===========  ==============================
域            含义
===========  ==============================
1xxx         请求、鉴权、权限
2xxx         连接与数据库
3xxx         备份与恢复
4xxx         存储、主机与通道
5xxx         服务端内部
9xxx         未知
===========  ==============================

设计原则：
1. **错误码只增不改**——已发布的码含义永久冻结，废弃走 ``deprecated`` 标记；
2. 错误码与 HTTP 状态**解耦**：同一个 HTTP 状态可以有多个业务码（如 400 既有
   参数错误也有业务校验错误），同一个业务码也可在不同端点复用；
3. 未显式指定业务码时，由 :func:`code_for_status` 按 HTTP 状态给出**兜底码**，
   保证任何错误响应都带 ``code``，集成方不必做文本匹配（补齐差距 G4）。
"""
from __future__ import annotations

#: 错误码注册表：code -> (名称, HTTP 状态, 默认 message)
ERROR_TABLE: dict[str, tuple[str, int, str]] = {
    # ---- 1xxx 请求、鉴权、权限 ----
    "AIDBM-1000": ("BAD_REQUEST", 400, "请求无效"),
    "AIDBM-1001": ("PARAM_INVALID", 400, "参数校验失败"),
    "AIDBM-1002": ("UNAUTHENTICATED", 401, "未登录或会话已过期"),
    "AIDBM-1003": ("PERMISSION_DENIED", 403, "无权限执行该操作"),
    "AIDBM-1004": ("NOT_FOUND", 404, "资源不存在"),
    "AIDBM-1005": ("METHOD_NOT_ALLOWED", 405, "请求方法不被允许"),
    "AIDBM-1006": ("CONFLICT", 409, "资源冲突（名称已存在或状态不允许）"),
    "AIDBM-1007": ("PAYLOAD_TOO_LARGE", 413, "请求体过大"),
    "AIDBM-1008": ("TOO_MANY_REQUESTS", 429, "请求过于频繁，请稍后重试"),
    # ---- 2xxx 连接与数据库 ----
    "AIDBM-2001": ("DB_CONNECT_FAILED", 400, "数据库连接或认证失败"),
    "AIDBM-2002": ("DB_NOT_FOUND", 400, "目标数据库不存在"),
    "AIDBM-2003": ("DB_CLIENT_MISSING", 400, "数据库客户端工具缺失"),
    # ---- 3xxx 备份与恢复 ----
    "AIDBM-3001": ("BACKUP_FAILED", 500, "备份执行失败"),
    "AIDBM-3002": ("RESTORE_FAILED", 500, "恢复执行失败"),
    "AIDBM-3003": ("NO_SPACE_LEFT", 500, "磁盘空间不足"),
    "AIDBM-3004": ("ARTIFACT_INVALID", 400, "备份产物缺失或校验不通过"),
    # ---- 4xxx 存储、主机与通道 ----
    "AIDBM-4001": ("SSH_FAILED", 400, "SSH 连接或远程执行失败"),
    "AIDBM-4002": ("STORAGE_FAILED", 400, "存储后端访问失败"),
    "AIDBM-4003": ("PATH_ILLEGAL", 400, "路径不合法或被拒绝"),
    "AIDBM-4004": ("SSH_HOST_MISSING", 400, "未纳管可用的 SSH 主机"),
    # ---- 5xxx 服务端 ----
    "AIDBM-5001": ("INTERNAL_ERROR", 500, "服务端内部错误"),
    "AIDBM-5002": ("NOT_IMPLEMENTED", 501, "该能力尚未实现"),
    "AIDBM-5003": ("DEPENDENCY_MISSING", 500, "服务端依赖缺失"),
    # ---- 9xxx 未知 ----
    "AIDBM-9000": ("UNKNOWN", 500, "未知错误"),
}

#: HTTP 状态 -> 兜底业务码（未显式指定业务码时使用）
_STATUS_FALLBACK: dict[int, str] = {
    400: "AIDBM-1000",
    401: "AIDBM-1002",
    403: "AIDBM-1003",
    404: "AIDBM-1004",
    405: "AIDBM-1005",
    409: "AIDBM-1006",
    413: "AIDBM-1007",
    429: "AIDBM-1008",
    500: "AIDBM-5001",
    501: "AIDBM-5002",
}


def exists(code: str) -> bool:
    """错误码是否已在注册表中。"""
    return code in ERROR_TABLE


def name(code: str) -> str:
    return (ERROR_TABLE.get(code) or ("UNKNOWN", 500, "未知错误"))[0]


def http_status(code: str) -> int:
    """错误码对应的建议 HTTP 状态（未注册返回 500）。"""
    return (ERROR_TABLE.get(code) or ("UNKNOWN", 500, "未知错误"))[1]


def default_message(code: str) -> str:
    return (ERROR_TABLE.get(code) or ("UNKNOWN", 500, "未知错误"))[2]


def code_for_status(status: int) -> str:
    """HTTP 状态 -> 兜底业务码；未登记的状态按 4xx/5xx 归类。"""
    if status in _STATUS_FALLBACK:
        return _STATUS_FALLBACK[status]
    if 400 <= status < 500:
        return "AIDBM-1000"
    if status >= 500:
        return "AIDBM-5001"
    return "AIDBM-9000"


def make_payload(code: str, message: str | None = None,
                 details: dict | list | None = None) -> dict:
    """构造三段式错误体（同时带历史 ``error`` 字段）。"""
    if not exists(code):
        code = "AIDBM-9000"
    msg = (message or default_message(code) or "").strip() or default_message(code)
    if details is None:
        details = {}
    return {
        "success": False,
        "code": code,
        "message": msg,
        "details": details,
        # 向后兼容：历史前端与调用方读 "error"
        "error": msg,
    }
