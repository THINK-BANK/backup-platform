# -*- coding: utf-8 -*-
"""OpenAPI 3.0 规范自动生成 + 离线 API 文档页（差异 G6）。

设计取舍（离线自足优先，见 docs/api_conventions.md §5）：

- **不引入 apispec/flask-smorest 等新依赖**，直接从 Flask 的 ``url_map`` +
  视图函数 docstring / 源码静态分析生成规范，零构建、零联网；
- **不引入 CDN 版 Swagger UI**：离线环境拿不到外部静态资源，
  ``/api/docs`` 使用本地自绘的轻量文档页（内联 CSS/JS，无外链）；
- 规范只输出 ``/api/v1`` 路径（规范契约）；``/api`` 是兼容前缀，不重复收录。

生成内容：路径 / 方法 / 摘要 / 路径参数 / 查询参数 / 是否含 JSON 请求体 /
统一错误响应（引用 ``#/components/schemas/Error``）/ 鉴权方式。
"""
from __future__ import annotations

import inspect
import re

from flask import current_app, jsonify, render_template, request

import config
from auth import login_required
from core import error_codes
from . import api_bp, contract

#: 文档自身端点不收录进规范
_SELF_PATHS = {"/docs", "/openapi.json"}

_QUERY_RE = re.compile(r"request\.args\.get\(\s*[\"']([A-Za-z0-9_\-]+)[\"']")
_QUERY_RE2 = re.compile(r"request\.args\[\s*[\"']([A-Za-z0-9_\-]+)[\"']\s*\]")
_BODY_RE = re.compile(r"request\.(get_json|json)\b")


def _view_source(rule) -> str:
    view = current_app.view_functions.get(rule.endpoint)
    if view is None:
        return ""
    try:
        return inspect.getsource(view)
    except (OSError, TypeError):
        return ""


def _summary_of(rule) -> str:
    view = current_app.view_functions.get(rule.endpoint)
    doc = inspect.getdoc(view) if view else None
    if not doc:
        return ""
    for line in doc.splitlines():
        line = line.strip()
        if line:
            return line[:120]
    return ""


def _tag_of(path: str) -> str:
    """按 v1 之后的第一个路径段分组（资源名即分组名）。"""
    rest = path[len(contract.V1_PREFIX):].strip("/")
    seg = rest.split("/")[0] if rest else "root"
    return seg or "root"


def _parameters(rule, source: str) -> list:
    params = []
    for arg in sorted(rule.arguments):
        params.append({
            "name": arg,
            "in": "path",
            "required": True,
            "schema": {"type": "integer"} if _rule_arg_is_int(rule, arg)
                      else {"type": "string"},
        })
    seen = set()
    for rx in (_QUERY_RE, _QUERY_RE2):
        for name in rx.findall(source):
            if name in seen:
                continue
            seen.add(name)
            params.append({"name": name, "in": "query", "required": False,
                           "schema": {"type": "string"}})
    # 分页是全局约定，凡检测到 limit/page 的接口补上规范参数说明
    if {"limit", "page", "size", "offset"} & seen:
        if "page" not in seen:
            params.append({"name": "page", "in": "query", "required": False,
                           "schema": {"type": "integer", "minimum": 1},
                           "description": "页码（规范分页参数，1 起）"})
        if "size" not in seen:
            params.append({"name": "size", "in": "query", "required": False,
                           "schema": {"type": "integer", "minimum": 1,
                                      "maximum": contract.MAX_PAGE_SIZE},
                           "description": "每页条数（上限 %d）"
                                          % contract.MAX_PAGE_SIZE})
    return params


def _rule_arg_is_int(rule, arg: str) -> bool:
    # Werkzeug 规则里 int 转换器体现为 "<int:name>"
    return ("<int:%s>" % arg) in str(rule)


def build_spec() -> dict:
    """由当前应用的路由表生成 OpenAPI 3.0 文档。"""
    paths: dict[str, dict] = {}
    tags: dict[str, str] = {}
    for rule in sorted(current_app.url_map.iter_rules(), key=lambda r: str(r)):
        path = str(rule)
        if not path.startswith(contract.V1_PREFIX + "/"):
            continue
        suffix = path[len(contract.V1_PREFIX):]
        if suffix in _SELF_PATHS or path.endswith("/static/<path:filename>"):
            continue
        source = _view_source(rule)
        tag = _tag_of(path)
        tags.setdefault(tag, "")
        ops = paths.setdefault(path, {})
        for method in sorted(rule.methods - {"HEAD", "OPTIONS"}):
            op_id = "%s_%s" % (rule.endpoint.split(".")[-1], method.lower())
            op = {
                "tags": [tag],
                "operationId": op_id,
                "summary": _summary_of(rule) or "%s %s" % (method, path),
                "parameters": _parameters(rule, source),
                "responses": {
                    "200": {"description": "成功",
                            "content": {"application/json": {"schema": {}}}},
                    "default": {
                        "description": "错误（三段式：code / message / details）",
                        "content": {"application/json": {
                            "schema": {"$ref": "#/components/schemas/Error"}}},
                    },
                },
                "security": [{"sessionAuth": []}, {"bearerAuth": []}],
            }
            if method in ("POST", "PUT", "PATCH") and _BODY_RE.search(source):
                op["requestBody"] = {
                    "content": {"application/json": {"schema": {"type": "object"}}},
                    "required": False,
                }
            ops[method.lower()] = op

    return {
        "openapi": "3.0.3",
        "info": {
            "title": "%s API" % config.PLATFORM_NAME,
            "version": config.PLATFORM_VERSION,
            "description": (
                "AI 原生智能数据库灾备管理平台对外 REST API。\n\n"
                "- 规范前缀 `/api/v1`；旧前缀 `/api` 为兼容路径（响应带 "
                "`Deprecation` / `Link` 头），大版本升级后下线。\n"
                "- 错误响应统一为 `{code, message, details}`，`code` 取 "
                "`AIDBM-xxxx`（错误码表见 `x-error-codes`）。\n"
                "- 列表接口统一分页：`page/size`（推荐）或 `limit/offset`（兼容），"
                "v1 返回 `{items, total, page, size, has_more}`。"
            ),
        },
        "servers": [{"url": "/"}],
        "tags": [{"name": t} for t in sorted(tags)],
        "paths": paths,
        "components": {
            "securitySchemes": {
                "sessionAuth": {"type": "apiKey", "in": "cookie",
                                "name": "session",
                                "description": "浏览器登录会话"},
                "bearerAuth": {"type": "http", "scheme": "bearer",
                               "description": "外部调用令牌（api_tokens 表）"},
            },
            "schemas": {
                "Error": {
                    "type": "object",
                    "required": ["code", "message"],
                    "properties": {
                        "success": {"type": "boolean", "example": False},
                        "code": {"type": "string", "example": "AIDBM-1004"},
                        "message": {"type": "string", "example": "记录不存在"},
                        "details": {"type": "object",
                                    "additionalProperties": True},
                        "error": {"type": "string",
                                  "description": "历史字段，等于 message（兼容保留）"},
                    },
                },
                "Page": {
                    "type": "object",
                    "properties": {
                        "items": {"type": "array", "items": {}},
                        "total": {"type": "integer"},
                        "page": {"type": "integer"},
                        "size": {"type": "integer"},
                        "has_more": {"type": "boolean"},
                    },
                },
            },
        },
        "x-error-codes": [
            {"code": code, "name": spec[0], "http": spec[1], "message": spec[2]}
            for code, spec in sorted(error_codes.ERROR_TABLE.items())
        ],
        "x-conventions": {
            "pagination": {"canonical": ["page", "size"],
                           "compatible": ["limit", "offset"],
                           "response_keys": ["items", "total", "page", "size", "has_more"]},
            "naming": "小写 kebab-case，资源用复数，动作用动词段（docs/api_conventions.md §1）",
            "deprecation": "旧前缀 /api 的响应带 Deprecation/Link 头",
        },
    }


@api_bp.route("/openapi.json", methods=["GET"])
@login_required
def openapi_spec():
    """OpenAPI 3.0 规范（JSON）。"""
    return jsonify(build_spec())


@api_bp.route("/docs", methods=["GET"])
@login_required
def api_docs_page():
    """离线 API 文档页（无外部 CDN 依赖）。"""
    base = request.path.rstrip("/")
    return render_template("api_docs.html", spec_url=base + "/openapi.json")
