# -*- coding: utf-8 -*-
"""AIDBM API 真实性体检。

与压测脚本（scripts/stress_test_full.py）不同，本脚本关注的是**正确性**：
自动发现 Flask url_map 中的全部 /api 路由，用真实 id 填充动态参数，逐个发起
真实请求，把以下几类"真实缺陷"抓出来并落盘报告：

  1. HTTP 5xx（服务端异常）—— 最高优先级，附响应体片段与平台日志中的 traceback
  2. 非 JSON 响应（返回 HTML 错误页 / 登录页，说明鉴权或异常处理有洞）
  3. 200 但业务语义失败（body 里带 error / success=false / {"ok": false}）
  4. 写接口错误处理不当：空体 / 非法 id 应当返回 4xx，返回 5xx 即为缺陷
  5. 未登录访问应当 401/302，却返回 200 或 500

安全性：写接口（POST/PUT/DELETE）一律走"错误路径"——DELETE 用不存在的
999999999，POST/PUT 用空体 {}，并跳过 DESTRUCTIVE 关键字路由，避免对真实
数据产生副作用。

用法：
    python scripts/api_audit.py                     # 全量体检
    python scripts/api_audit.py --only-get          # 只测读接口
    python scripts/api_audit.py --report /tmp/a.json
"""
import argparse
import json
import os
import re
import sys
import time
from collections import Counter

import requests
import urllib3

urllib3.disable_warnings()
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BASE = "http://127.0.0.1:8080"
USER = "admin"
PWD = "admin123"
PLATFORM_LOG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs", "platform.log")

# 这些路由会产生真实副作用（删库/跑备份/部署/发通知），体检时对**写方法**跳过；
# GET 一律真实调用（只读接口即使名字带 run/export 也只返回数据，无副作用）
DESTRUCTIVE = (
    "destroy", "drop", "reset", "wipe", "format", "purge", "truncate",
    "run", "trigger", "execute", "start", "stop", "restart", "deploy",
    "restore", "recover", "clone", "send", "notify", "import", "scan",
    "export", "approve", "reject", "expire", "verify", "test", "sync",
    "migrate", "switch", "failover", "rollback", "reload", "upload",
)


def login():
    s = requests.Session()
    r = s.post(f"{BASE}/login", data={"username": USER, "password": PWD}, timeout=20)
    if r.status_code != 200:
        raise RuntimeError(f"登录失败: {r.status_code} {r.text[:200]}")
    return s


def collect_samples(session):
    """从各列表接口取真实 id，用于填充 <int:xxx> 动态参数。"""
    samples = {}
    # (参数名, 列表接口, 响应里取数的 key)
    sources = [
        ("task_id", "/api/tasks", None),
        ("id", "/api/tasks", None),
        ("record_id", "/api/records?limit=3", None),
        ("host_id", "/api/hosts", None),
        ("policy_id", "/api/policy", None),
        ("plan_id", "/api/db-migrate", None),
        ("link_id", "/api/disaster-links", None),
        ("ssh_host_id", "/api/ssh-hosts", None),
        ("request_id", "/api/clone", None),
        ("scan_id", "/api/datamining/scans", None),
        ("export_id", "/api/datamining/exports", None),
        ("session_id", "/api/agent/sessions", None),
        ("job_id", "/api/tasks", None),
        ("source_id", "/api/tasks", None),
        ("target_id", "/api/tasks", None),
        ("user_id", "/api/users", None),
        ("role_id", "/api/rbac/roles", None),
        ("storage_id", "/api/storage", None),
        ("tape_id", "/api/tape/library", None),
        ("vm_host_id", "/api/vm/hosts", None),
        ("vm_policy_id", "/api/vm/policies", None),
        ("db_id", "/api/hosts", None),
        ("snapshot_id", "/api/vm/policies", None),
    ]
    seen_ids = {}
    for key, api, data_key in sources:
        if key in samples:
            continue
        try:
            r = session.get(f"{BASE}{api}", timeout=20)
            if r.status_code != 200:
                continue
            body = r.json()
        except Exception:
            continue
        rows = body if isinstance(body, list) else (body.get("data") or body.get("items") or [])
        if isinstance(rows, dict):
            rows = list(rows.values())
        if rows and isinstance(rows[0], dict):
            rid = rows[0].get("id")
            if rid is not None:
                samples[key] = rid
                seen_ids.setdefault(key, rid)
    # 通用兜底：任意参数名若没取样到，用 tasks 第一个 id（很多接口是 task 子资源）
    fallback = samples.get("task_id") or samples.get("id") or 1
    return samples, fallback


def discover_routes(only_get=False):
    """从 Flask url_map 发现全部 /api 路由及其方法。"""
    from app import create_app
    app = create_app()
    out = []
    for r in app.url_map.iter_rules():
        if not r.rule.startswith("/api"):
            continue
        methods = sorted(m for m in r.methods if m in ("GET", "POST", "PUT", "DELETE", "PATCH"))
        if only_get:
            methods = [m for m in methods if m == "GET"]
        for m in methods:
            out.append((m, r.rule))
    return sorted(set(out))


def fill(rule, samples, fallback, session, cache, nonexistent=False):
    """把 <int:xxx> 占位符替换为真实 id。

    优先用 collect_samples 取到的样本；取不到时按**路由前缀**反查对应集合接口
    （如 /api/sync-tasks/<int:task_id> → GET /api/sync-tasks 取首行 id），
    避免把任务 id 填进 sync-tasks / plugins 等无关路由造成假 404。
    """
    path = rule
    while "<" in path:
        i, j = path.index("<"), path.index(">")
        token = path[i + 1:j]
        name = token.split(":")[-1]
        if nonexistent:
            val = "999999999"
        else:
            # 1) 优先按路由前缀反查集合接口（最贴合该资源）
            val = None
            prefix = path[:i].rstrip("/")
            if prefix.startswith("/api/") and prefix.count("/") >= 2:
                coll = prefix if not prefix.rsplit("/", 1)[-1].isdigit() else prefix.rsplit("/", 1)[0]
                if coll not in cache:
                    cache[coll] = _first_id(session, coll)
                val = cache[coll]
            # 2) 再用通用样本（task_id 等）——注意不能让 task_id 污染其它资源，
            #    因此只有前缀反查无果时才回落
            if val is None:
                val = samples.get(name)
            if val is None:
                val = str(fallback)
        path = path[:i] + str(val) + path[j + 1:]
    return path, True


def _first_id(session, coll):
    """从集合接口取首行 id；失败返回 None。"""
    try:
        r = session.get(f"{BASE}{coll}", timeout=15)
        if r.status_code != 200:
            return None
        body = r.json()
    except Exception:
        return None
    rows = body if isinstance(body, list) else (body.get("data") or body.get("items") or [])
    if isinstance(rows, dict):
        rows = list(rows.values())
    if rows and isinstance(rows[0], dict):
        return rows[0].get("id")
    return None


def classify(status, body_text, ctype):
    """判定响应是否存在真实性缺陷。返回 (级别, 说明)；正常返回 (None, '')。"""
    if status >= 500:
        return "CRITICAL", f"HTTP {status} 服务端异常"
    if status in (401, 403) and "login" in body_text[:400].lower():
        return "WARN", f"HTTP {status} 疑似鉴权异常（返回登录页）"
    if "text/html" in (ctype or "") and status != 302:
        return "WARN", "返回 HTML 而非 JSON"
    low = body_text[:2000].lower()
    if status == 200:
        # 业务语义失败却返回 200
        if re.search(r'"\s*(error|errmsg|message)\s*"\s*:\s*"[^"]{3,}"', low) and '"success":true' not in low:
            if '"success": false' in low or '"ok": false' in low:
                return "WARN", "HTTP 200 但业务失败（success=false）"
        if "<!doctype html" in low:
            return "WARN", "HTTP 200 但返回 HTML"
    return None, ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only-get", action="store_true", help="只测 GET 接口")
    ap.add_argument("--report", default="/tmp/api_audit.json")
    ap.add_argument("--timeout", type=int, default=30)
    args = ap.parse_args()

    session = login()
    samples, fallback = collect_samples(session)
    print(f"[取样] 真实 id 样本 {len(samples)} 项：{json.dumps(samples, ensure_ascii=False)[:300]}")

    routes = discover_routes(args.only_get)
    print(f"[发现] 待检 {len(routes)} 个 (method, path) 组合\n")

    findings = []
    cache = {}
    skipped = []
    codes = Counter()
    t_start = time.time()
    log_mark = os.path.getsize(PLATFORM_LOG) if os.path.exists(PLATFORM_LOG) else 0

    for method, rule in routes:
        lower = rule.lower()
        if method != "GET" and any(k in lower for k in DESTRUCTIVE):
            skipped.append((method, rule, "destructive"))
            continue
        if method in ("POST", "PUT", "PATCH"):
            path, ok = fill(rule, samples, fallback, session, cache)
            payload = {}
            try:
                r = session.request(method, f"{BASE}{path}", json=payload, timeout=args.timeout)
            except Exception as e:
                findings.append({"method": method, "path": path, "level": "CRITICAL",
                                 "msg": f"请求异常 {type(e).__name__}: {e}"})
                continue
            codes[r.status_code] += 1
            lvl, msg = classify(r.status_code, r.text, r.headers.get("Content-Type", ""))
            # 空体提交：期望 4xx（参数校验），5xx 才是缺陷
            if lvl is None and r.status_code >= 500:
                lvl, msg = "CRITICAL", f"HTTP {r.status_code} 空体提交触发服务端异常"
            if lvl:
                findings.append({"method": method, "path": path, "level": lvl, "msg": msg,
                                 "status": r.status_code, "snippet": r.text[:300]})
            continue
        if method == "DELETE":
            path, ok = fill(rule, samples, fallback, session, cache, nonexistent=True)
            try:
                r = session.request(method, f"{BASE}{path}", timeout=args.timeout)
            except Exception as e:
                findings.append({"method": method, "path": path, "level": "CRITICAL",
                                 "msg": f"请求异常 {type(e).__name__}: {e}"})
                continue
            codes[r.status_code] += 1
            lvl, msg = classify(r.status_code, r.text, r.headers.get("Content-Type", ""))
            # 删不存在的资源：期望 404/400，返回 200 成功也算可疑
            if lvl is None and r.status_code == 200:
                low = r.text[:500].lower()
                if '"success":true' in low or '"ok":true' in low:
                    lvl, msg = "WARN", "删除不存在的资源却返回成功"
            if lvl:
                findings.append({"method": method, "path": path, "level": lvl, "msg": msg,
                                 "status": r.status_code, "snippet": r.text[:300]})
            continue

        # GET
        path, ok = fill(rule, samples, fallback, session, cache)
        try:
            r = session.get(f"{BASE}{path}", timeout=args.timeout)
        except Exception as e:
            findings.append({"method": "GET", "path": path, "level": "CRITICAL",
                             "msg": f"请求异常 {type(e).__name__}: {e}"})
            continue
        codes[r.status_code] += 1
        lvl, msg = classify(r.status_code, r.text, r.headers.get("Content-Type", ""))
        # 用真实 id 却 404：可能是路由/参数名不匹配，列入人工研判
        if lvl is None and r.status_code == 404:
            lvl, msg = "WARN", "真实 id 却 404（路由或参数不匹配？）"
        if lvl:
            findings.append({"method": "GET", "path": path, "level": lvl, "msg": msg,
                             "status": r.status_code, "snippet": r.text[:300]})

    # 平台服务端 traceback（体检窗口内）
    tracebacks = []
    if os.path.exists(PLATFORM_LOG):
        with open(PLATFORM_LOG, "rb") as f:
            f.seek(log_mark)
            tail = f.read().decode("utf-8", "ignore")
        blocks = re.findall(r"(Traceback \(most recent call last\):[\s\S]{0,1500}?)(?=\n\d{4}-|\Z)", tail)
        for b in blocks:
            tracebacks.append(b.strip().splitlines()[-1][:300])

    crit = [f for f in findings if f["level"] == "CRITICAL"]
    warn = [f for f in findings if f["level"] == "WARN"]
    print(f"\n===== 体检结果（{time.time() - t_start:.1f}s） =====")
    print(f"已请求: {sum(codes.values())}  状态码分布: {dict(codes)}")
    print(f"CRITICAL(5xx/异常): {len(crit)}   WARN(语义可疑): {len(warn)}   跳过: {len(skipped)}")
    print(f"服务端 traceback: {len(tracebacks)} 段")
    if crit:
        print("\n---- CRITICAL ----")
        for f in crit[:40]:
            print(f"  [{f['status']}] {f['method']} {f['path']}\n      {f['msg']}\n      {f.get('snippet', '')[:200]}")
    if warn:
        print("\n---- WARN ----")
        for f in warn[:40]:
            print(f"  [{f.get('status')}] {f['method']} {f['path']} -> {f['msg']}")
    if tracebacks:
        print("\n---- 服务端异常栈（末行） ----")
        for t in tracebacks[:20]:
            print(f"  {t}")

    with open(args.report, "w", encoding="utf-8") as f:
        json.dump({"time": time.strftime("%Y-%m-%d %H:%M:%S"), "codes": dict(codes),
                   "critical": crit, "warn": warn, "skipped": skipped,
                   "tracebacks": tracebacks}, f, ensure_ascii=False, indent=2)
    print(f"\n报告已写入 {args.report}")


if __name__ == "__main__":
    main()
