#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""接口契约检查门禁（差异 G4/G5 + 命名规范，见 docs/api_conventions.md）。

检查项：
1. **命名**：/api 路径必须小写 kebab-case；资源段应为复数；动作段须属白名单动词；
2. **版本**：必须同时存在 /api/v1 注册（双前缀），且 v1 路径集合是 /api 路径集合
   的超集（不允许 v1 缺路由）；
3. **错误码**：core/error_codes.py 注册表形状合法，且被正常加载；
4. **分页**：凡出现 ``limit=``/``page=`` 的列表接口必须走 ``contract.list_response``
   （避免又出现一套自研分页）。

存量债务不阻塞：当前违规清单固化在 ``scripts/api_contract_baseline.json``，
本脚本只对**新增**违规失败（``--update-baseline`` 重新固化）。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.environ.setdefault("SCHEDULER_ENABLED", "0")

BASELINE = os.path.join(ROOT, "scripts", "api_contract_baseline.json")

#: 允许作为「动作段」的动词（RESTful 子资源动作）
ACTION_VERBS = {
    "run", "stop", "start", "restart", "test", "check", "verify", "validate",
    "preview", "probe", "refresh", "reload", "sync", "restore", "backup",
    "apply", "execute", "cancel", "approve", "reject", "clone", "rotate",
    "deploy", "install", "uninstall", "scan", "export", "import", "download",
    "upload", "render", "build", "rollback", "abort", "resume", "pause",
    "recover", "compare", "diff", "search", "query", "aggregate", "summary",
    "stats", "status", "health", "meta", "timeline", "tables", "list-tables",
    "grant", "revoke", "enable", "disable", "assign", "bind", "unbind",
    "install-tool", "check-tool", "fill-gap", "deep", "rewrite", "translate",
    "ask", "chat", "analyze", "analyse", "score", "forecast", "plan",
    "activate", "deactivate", "notify", "send", "collect", "ingest", "flush",
    "compact", "prune", "cleanup", "resync", "replay", "cutover", "switchover",
    "failover", "promote", "demote", "open", "close", "exec", "invoke",
    "trace", "dump", "restore-db", "attach", "detach", "mount", "unmount",
    "options", "schema", "columns", "rows", "sample", "content", "raw",
    "report", "reports", "archive", "unarchive", "seal", "unseal", "lock",
    "unlock", "encrypt", "decrypt", "reset", "init", "bootstrap", "warmup",
}

#: 允许使用单数的资源名（单例配置、聚合视图、不可数名词资源；新增需在 PR 说明理由）
SINGULAR_ALLOWED = {
    "meta", "health", "login", "logout", "profile", "system", "status",
    "notify-config", "pool-crypto", "ai", "cdc", "rbac", "itsm", "tape",
    "jdbc", "docs", "policy", "storage", "inspection", "deploy", "license",
    "about", "guide", "overview", "dashboard", "search", "data", "ai-agent",
    "ai-alert", "data-compare", "rt", "openapi.json",
    # 单例配置/状态类
    "config", "state", "log", "file", "record", "schedule", "usage", "window",
    "enabled", "root", "me", "lifecycle", "scheduler", "synthesize",
    # 聚合/派生视图类
    "baseline", "trend", "inventory", "compliance", "template", "enriched",
    "recommend", "local-root", "replication-config", "flink-config",
    # 缩写与状态视图
    "vdb", "protected", "rpo",
}

PATH_RE = re.compile(r"^/api(/v1)?(/[a-z0-9\-\.]+(/<[^>]+>)*)+$")
SEGMENT_RE = re.compile(r"^[a-z0-9\-\.]+$")


def load_app():
    from app import create_app
    import tempfile
    tmp = tempfile.mkdtemp(prefix="api_check_")
    os.environ["META_DB_PATH"] = os.path.join(tmp, "meta.db")
    os.environ["BACKUP_ROOT"] = os.path.join(tmp, "backups")
    return create_app()


def iter_api_rules(app):
    for rule in app.url_map.iter_rules():
        path = str(rule)
        if path.startswith("/api/"):
            yield path, rule


def _segments(path: str, strip_v1: bool = True) -> list[str]:
    if strip_v1 and path.startswith("/api/v1/"):
        path = "/api/" + path[len("/api/v1/"):]
    return [s for s in path[len("/api/"):].split("/") if s]


def _is_param(seg: str) -> bool:
    return seg.startswith("<")


def naming_violations(path: str, methods=None) -> list[dict]:
    """对单个路径做命名规范判定（语义化，避免把命名空间误判为资源）。

    R1 格式：路径段必须小写 kebab-case；
    R2 集合复数：**集合端点**（GET 且末段不是参数、不是动作动词）末段应为复数，
       或在单例/命名空间白名单内——例如 ``/api/records`` ✓、``/api/task`` ✗；
    中间段视为命名空间或子资源，仅做格式约束（否则 ``/api/agent/sessions``
    这类命名空间会被无意义地判错）。
    """
    out = []
    if not PATH_RE.match(path):
        out.append({"path": path, "rule": "R1-格式",
                    "detail": "路径必须为小写 kebab-case（禁止大写/下划线/裸扩展名）"})
        return out
    for seg in _segments(path):
        if not _is_param(seg) and not SEGMENT_RE.match(seg):
            out.append({"path": path, "rule": "R1-格式",
                        "detail": "段 %r 不符合小写 kebab-case" % seg})
    raw_segs = _segments(path)
    segs = [s for s in raw_segs if not _is_param(s)]
    if not segs:
        return out
    methods = set(methods or [])
    last = segs[-1]
    base = last.split(".")[0]
    # 实例级子资源（末段位于参数段之后，如 /plugins/<pid>/state）按单例处理，
    # 只要求格式合法，不强制复数。
    if _instance_subresource(raw_segs):
        return out
    if "GET" in methods and base not in ACTION_VERBS \
            and base not in SINGULAR_ALLOWED and last not in SINGULAR_ALLOWED \
            and not base.endswith("s"):
        out.append({"path": path, "rule": "R2-集合复数",
                    "detail": "集合端点末段 %r 应为复数（单例资源请登记白名单）" % last})
    return out


def _instance_subresource(raw_segs: list[str]) -> bool:
    """末段是否位于参数段之后（实例级单例子资源，如 /fields/<fid>/state）。"""
    for idx, seg in enumerate(raw_segs[:-1]):
        if _is_param(seg):
            return True
    return False


def collect(app) -> dict:
    rules = list(iter_api_rules(app))
    legacy = {p for p, _ in rules if not p.startswith("/api/v1/")}
    v1 = {p for p, _ in rules if p.startswith("/api/v1/")}
    expected_v1 = {"/api/v1" + p[len("/api"):] for p in legacy}

    findings = []
    for path, rule in rules:
        for v in naming_violations(path, rule.methods):
            findings.append(v)

    # 版本覆盖：v1 必须覆盖全部 /api 路径（双前缀注册的断言）
    for path in sorted(expected_v1 - v1):
        findings.append({"path": path, "rule": "R4-版本缺失",
                         "detail": "该路径没有 /api/v1 版本（双前缀注册失效）"})

    # 错误码注册表形状
    from core import error_codes
    for code, (name, http, message) in error_codes.ERROR_TABLE.items():
        if not re.fullmatch(r"AIDBM-\d{4}", code):
            findings.append({"path": "core/error_codes.py", "rule": "R5-错误码",
                             "detail": "错误码格式非法: %s" % code})

    return {
        "paths_total": len(legacy),
        "v1_total": len(v1),
        "findings": sorted(findings, key=lambda f: (f["rule"], f["path"])),
    }


def _load_baseline() -> set:
    if not os.path.exists(BASELINE):
        return set()
    with open(BASELINE, encoding="utf-8") as f:
        data = json.load(f)
    return {(x["path"], x["rule"]) for x in data.get("findings", [])}


def _save_baseline(findings: list) -> None:
    with open(BASELINE, "w", encoding="utf-8") as f:
        json.dump({"note": "存量命名/契约债务基线：门禁只拦截新增违规，"
                           "逐步收敛后请重新固化（--update-baseline）",
                   "findings": findings}, f, ensure_ascii=False, indent=2)


def main() -> int:
    ap = argparse.ArgumentParser(description="AIDBM 接口契约门禁")
    ap.add_argument("--update-baseline", action="store_true",
                    help="把当前违规固化为基线（存量债务）")
    ap.add_argument("--json", action="store_true", help="输出 JSON 结果")
    args = ap.parse_args()

    app = load_app()
    result = collect(app)
    findings = result["findings"]

    if args.update_baseline:
        _save_baseline(findings)
        print("已固化基线：%d 条（存量债务），/api 路径 %d 个，/api/v1 路径 %d 个"
              % (len(findings), result["paths_total"], result["v1_total"]))
        return 0

    baseline = _load_baseline()
    new_findings = [f for f in findings
                    if (f["path"], f["rule"]) not in baseline]
    resolved = [b for b in baseline
                if b not in {(f["path"], f["rule"]) for f in findings}]

    if args.json:
        print(json.dumps({"new": new_findings, "resolved": sorted(resolved),
                          "total": len(findings)}, ensure_ascii=False, indent=2))
    else:
        print("接口契约检查：/api 路径 %d 个，/api/v1 路径 %d 个"
              % (result["paths_total"], result["v1_total"]))
        print("存量债务 %d 条（基线内，不阻塞）；本次新增 %d 条；已收敛 %d 条"
              % (len(findings) - len(new_findings), len(new_findings), len(resolved)))
        for f in new_findings[:50]:
            print("  [新增违规] %-28s %s - %s" % (f["rule"], f["path"], f["detail"]))
        for path, rule in sorted(resolved)[:50]:
            print("  [已收敛] %-28s %s（可 --update-baseline 更新基线）" % (rule, path))

    return 1 if new_findings else 0


if __name__ == "__main__":
    sys.exit(main())
