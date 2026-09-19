# -*- coding: utf-8 -*-
"""API 统一契约层：错误码、分页、版本前缀与响应归一化。

本模块把「接口规范」从文档落到代码，供所有 Blueprint 复用（见
``docs/api_conventions.md``）：

1. **三段式错误**：任何 ``/api`` JSON 响应只要带 ``error`` 或非 2xx，就保证含
   ``code`` / ``message`` / ``details``（:func:`normalize_response` 统一补齐），
   存量 200+ 处 ``jsonify({"error": ...})`` 无需逐个改写；
2. **统一分页**：``page/size`` 与 ``limit/offset`` 双兼容（:func:`pagination_args`），
   ``/api/v1`` 一律返回信封 ``{items, total, page, size, has_more}``
   （:func:`list_response`），旧 ``/api`` 路径保持历史形状不变；
3. **版本前缀**：``/api`` 为兼容路径（响应带 ``Deprecation`` + ``Link`` 头），
   ``/api/v1`` 为规范路径；
4. **命名规范**：不合规资源的规范别名见 :data:`CANONICAL_ALIASES`，
   规则与门禁见 ``scripts/api_contract_check.py``。
"""
from __future__ import annotations

import json

from flask import Blueprint, Response, g, jsonify, request

from core import error_codes

#: 当前对外 API 版本
API_VERSION = "v1"

#: 规范前缀与兼容前缀
V1_PREFIX = "/api/v1"
LEGACY_PREFIX = "/api"

#: 分页上限（保护平台与浏览器，避免一次拉全表）
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 500

#: 历史命名 → 规范命名。规范名称为正式契约，历史名称保留为**别名**（同函数注册，
#: 行为完全一致），后续大版本可下线历史名。新增不合规资源必须走别名收敛，
#: 存量债务清单见 docs/api_naming_baseline.md。
CANONICAL_ALIASES = {
    "/db-migrate": "/migration-plans",
    "/migration": "/migration-protection-plans",
    "/custom-script/template": "/custom-scripts/template",
}

#: 错误响应中允许出现的业务上下文键（其余透传进 details）
_ERROR_CONTEXT_KEYS = ("details",)


# ---------------------------------------------------------------- 错误响应

def error_response(code: str, message: str | None = None,
                   details: dict | list | None = None,
                   status: int | None = None) -> Response:
    """返回三段式错误响应（显式指定业务码时用本函数）。"""
    payload = error_codes.make_payload(code, message, details)
    http = status if status is not None else error_codes.http_status(code)
    return jsonify(payload), http


def mark_error_code(code: str) -> None:
    """在视图内标记本次请求的业务码（供 :func:`normalize_response` 采纳）。

    用于存量 ``return jsonify({"error": ...}), 400`` 写法：不改变响应体写法，
    只补一个业务码，比逐个改写返回语句安全。
    """
    if error_codes.exists(code):
        g.api_error_code = code


# ---------------------------------------------------------------- 分页

def pagination_args(default_size: int = DEFAULT_PAGE_SIZE,
                    max_size: int = MAX_PAGE_SIZE) -> tuple[int, int, int]:
    """解析分页参数，返回 ``(page, size, offset)``。

    双兼容（规范见 docs/api_conventions.md §2）：
    - ``page/size``：1 起页码 + 每页条数（推荐）；
    - ``limit/offset``：历史写法，等价换算为 page/size；
    - 两者同时出现时以 ``page/size`` 为准（新规范优先）。
    """
    page = request.args.get("page", type=int)
    size = request.args.get("size", type=int)
    if page or size:
        page = max(1, int(page or 1))
        size = max(1, min(int(size or default_size), max_size))
        return page, size, (page - 1) * size
    limit = request.args.get("limit", type=int)
    offset = request.args.get("offset", type=int) or 0
    size = max(1, min(int(limit or default_size), max_size))
    offset = max(0, int(offset))
    return (offset // size) + 1, size, offset


def envelope_wanted() -> bool:
    """是否需要返回统一信封。

    规范路径 ``/api/v1`` 一律返回信封；兼容路径 ``/api`` 仅当显式 ``?envelope=1``
    （或调用方带 ``page/size`` 参数）时返回，保证老前端零改动。
    """
    if request.path.startswith(V1_PREFIX):
        return True
    if (request.args.get("envelope") or "").lower() in ("1", "true", "yes"):
        return True
    return bool(request.args.get("page") or request.args.get("size"))


def list_response(rows: list, total: int | None = None,
                  legacy=None, **extra):
    """列表接口统一响应。

    :param rows: 当前页数据
    :param total: 总数；为 None 时用 ``len(rows)`` 推断（此时 has_more 由「是否满页」判定）
    :param legacy: 兼容路径下返回的历史响应体；缺省返回 ``rows`` 本身
    :param extra: 附加到信封顶层的字段（信封模式下）
    """
    if not envelope_wanted():
        return jsonify(legacy if legacy is not None else rows)
    page, size, _offset = pagination_args()
    if total is None:
        total = len(rows)
        full_page = len(rows) >= size
        has_more = full_page
    else:
        has_more = page * size < int(total)
    payload = {"items": rows, "total": int(total), "page": page, "size": size,
               "has_more": bool(has_more)}
    payload.update(extra or {})
    return jsonify(payload)


# ---------------------------------------------------------------- 归一化钩子

def _is_json(resp: Response) -> bool:
    return (resp.mimetype or "").lower() == "application/json"


def _looks_like_error(status: int, body) -> bool:
    if status >= 400:
        return True
    if not isinstance(body, dict):
        return False
    if body.get("success") is False or body.get("ok") is False:
        return True
    # 仅含 error 字段的 2xx（部分端点用 200 报业务失败）
    return "error" in body and not any(k in body for k in ("success", "ok"))


def normalize_response(resp: Response) -> Response:
    """补齐三段式错误体 + 版本头（注册在 blueprint 的 after_request）。"""
    path = request.path or ""
    if not path.startswith(LEGACY_PREFIX + "/"):
        return resp

    if _is_json(resp):
        body = resp.get_json(silent=True)
        if _looks_like_error(resp.status_code, body):
            if isinstance(body, dict):
                if not body.get("code"):
                    code = getattr(g, "api_error_code", None) \
                        or error_codes.code_for_status(resp.status_code)
                    body = _upgrade_error_body(body, code)
                elif not body.get("error"):
                    body["error"] = body.get("message") or error_codes.default_message(body["code"])
            else:
                # 非 dict（如裸字符串）错误体：包装为规范结构
                body = _upgrade_error_body({"error": body}, _fallback_code(resp))
            resp.set_data(json.dumps(body, ensure_ascii=False))
            resp.headers["Content-Type"] = "application/json; charset=utf-8"

    _apply_version_headers(resp, path)
    return resp


def _fallback_code(resp: Response) -> str:
    return getattr(g, "api_error_code", None) or error_codes.code_for_status(resp.status_code)


def _upgrade_error_body(body: dict, code: str) -> dict:
    """把历史错误体升级为三段式（保留原有字段，不丢信息）。"""
    if not error_codes.exists(code):
        code = "AIDBM-9000"
    raw = body.get("error")
    if raw is None:
        raw = body.get("message")
    if isinstance(raw, str):
        message = raw.strip()
    elif raw is None:
        message = error_codes.default_message(code)
    else:
        message = json.dumps(raw, ensure_ascii=False)
    message = message or error_codes.default_message(code)

    out = dict(body)
    out["success"] = False
    out["code"] = code
    out["message"] = message
    out["error"] = raw if isinstance(raw, str) and raw.strip() else message
    details = out.get("details")
    if not isinstance(details, (dict, list)):
        out["details"] = {}
    # 历史响应里跟错误同级的上下文（errors/warnings/...）统一收进 details
    for k in list(out.keys()):
        if k in ("success", "code", "message", "error", "details"):
            continue
        if k in ("errors", "warnings", "hint", "fields", "conflicts"):
            out.setdefault("details", {})
            if isinstance(out["details"], dict):
                out["details"].setdefault(k, out[k])
            out.pop(k, None)
    return out


def _apply_version_headers(resp: Response, path: str) -> None:
    resp.headers["X-API-Version"] = API_VERSION
    if path.startswith(V1_PREFIX):
        return
    # 兼容路径：按 RFC 8594 标注弃用，并给出后继版本地址
    resp.headers["Deprecation"] = "true"
    successor = V1_PREFIX + path[len(LEGACY_PREFIX):]
    resp.headers["Link"] = '<%s>; rel="successor-version"' % successor
    resp.headers["Warning"] = '299 - "Deprecated API prefix /api, use /api/v1"'
    resp.headers["X-API-Deprecated-Prefix"] = "true"


def register(bp: Blueprint) -> None:
    """在 API 蓝图注册归一化钩子。"""
    bp.after_request(normalize_response)
