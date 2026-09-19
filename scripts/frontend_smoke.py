#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""前端自动化冒烟（差异 G8），两种模式共用同一套断言：

``browser``（默认，CI 使用）
    真实 chromium + Playwright：登录 → 遍历全部页面路由（断言 200、标题非空、
    无 JS 异常、无 5xx 资源请求）→ 浏览器内直连 API 断言契约。
    需要 ``playwright install chromium``，且宿主机 glibc 需满足其 Node 驱动要求
    （Playwright 1.4x 需 glibc ≥ 2.27，麒麟 V10/老 glibc 环境跑不了，属预期）。

``http``
    无浏览器：用 cookie 会话走真实 HTTP，断言登录、全部页面 200、页面标题、
    以及同一套 API 契约。**离线环境/老 glibc 机器可跑**，用于提交前门禁与
    CI 兜底（注意：它不校验 JS 运行时错误，浏览器模式才覆盖前端脚本）。

用法::

    .venv/bin/python scripts/frontend_smoke.py                     # 自起隔离实例（browser）
    .venv/bin/python scripts/frontend_smoke.py --mode http         # 本地/离线可用
    .venv/bin/python scripts/frontend_smoke.py --base-url http://127.0.0.1:8080
"""
from __future__ import annotations

import argparse
import http.cookiejar
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PAGES = [
    "/", "/tasks", "/records", "/restore", "/restore-verify", "/storage", "/drills",
    "/logs", "/operations", "/inspection", "/deploy", "/plugins", "/sync",
    "/data-compare", "/realtime", "/rt-timeline", "/clone", "/vdb", "/vm",
    "/migration", "/protection", "/alert", "/agent", "/datamining", "/db-adapters",
    "/users", "/settings", "/file_backup", "/dr-link", "/cdc",
]

CONSOLE_IGNORE = ("favicon", "Download the React DevTools", "net::ERR_ABORTED")
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.S | re.I)


class Report:
    def __init__(self):
        self.passed: list[str] = []
        self.failed: list[str] = []

    def ok(self, name, detail=""):
        self.passed.append("%s%s" % (name, (" — " + detail) if detail else ""))

    def bad(self, name, detail=""):
        self.failed.append("%s%s" % (name, (" — " + detail) if detail else ""))


# ---------------------------------------------------------------- 服务拉起

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_http(url: str, timeout: float = 60.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            with urllib.request.urlopen(url, timeout=3) as r:
                if r.status < 500:
                    return True
        except urllib.error.HTTPError as e:
            if e.code < 500:
                return True
        except Exception:
            time.sleep(0.6)
    return False


class Server:
    """隔离实例：临时元数据库/备份目录 + 独立端口。"""

    def __init__(self, port: int, workdir: str):
        self.port = port
        self.workdir = workdir
        self.proc = None
        self.log = os.path.join(workdir, "server.log")

    def start(self):
        env = dict(os.environ)
        env.update({
            "WEB_PORT": str(self.port),
            "WEB_HOST": "127.0.0.1",
            "META_DB_PATH": os.path.join(self.workdir, "meta.db"),
            "BACKUP_ROOT": os.path.join(self.workdir, "backups"),
            "INSTANCE_DIR": os.path.join(self.workdir, "instance"),
            "LOG_DIR": os.path.join(self.workdir, "logs"),
            "SCHEDULER_ENABLED": "0",
            "CODEBUDDY_SAFE_DELETE_ENABLED": "0",
            "PYTHONUNBUFFERED": "1",
        })
        self._fh = open(self.log, "w", encoding="utf-8")
        self.proc = subprocess.Popen([sys.executable, "app.py"], cwd=ROOT, env=env,
                                     stdout=self._fh, stderr=subprocess.STDOUT)
        if not _wait_http("http://127.0.0.1:%d/login" % self.port):
            raise RuntimeError("平台未在超时内启动，日志见 %s" % self.log)

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        try:
            self._fh.close()
        except Exception:
            pass


# ---------------------------------------------------------------- HTTP 模式

class HttpSession:
    def __init__(self, base: str, login: bool = True):
        self.base = base
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar))
        self.logged_in = False
        if login:
            self.login()

    def login(self):
        data = urllib.parse.urlencode({"username": "admin", "password": "admin123"})
        req = urllib.request.Request(self.base + "/login", data=data.encode())
        self.opener.open(req, timeout=20).read()
        self.logged_in = any(c.name == "session" for c in self.jar)

    def get(self, path: str, timeout: float = 30.0, raw: bool = False):
        """返回 (status, headers, text|json)。"""
        try:
            with self.opener.open(self.base + path, timeout=timeout) as r:
                body = r.read().decode("utf-8", "replace")
                return r.status, dict(r.headers), (body if raw else _maybe_json(body))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            return e.code, dict(e.headers), (body if raw else _maybe_json(body))


def _maybe_json(text: str):
    import json
    try:
        return json.loads(text)
    except Exception:
        return text


def check_api_contract(sess: HttpSession, rep: Report, anon: HttpSession | None = None):
    """两种模式共用的 API 契约断言。"""
    status, _h, meta = sess.get("/api/v1/meta")
    if status == 200 and isinstance(meta, dict) \
            and meta.get("api", {}).get("version") == "v1":
        rep.ok("API /api/v1/meta", "platform=%s %s" % (
            meta.get("platform", {}).get("name"),
            meta.get("platform", {}).get("version")))
    else:
        rep.bad("API /api/v1/meta", "%s %s" % (status, str(meta)[:120]))

    status, _h, spec = sess.get("/api/v1/openapi.json")
    paths = len(spec.get("paths", {})) if isinstance(spec, dict) else 0
    codes = len(spec.get("x-error-codes", [])) if isinstance(spec, dict) else 0
    if status == 200 and paths > 50 and codes > 10:
        rep.ok("API OpenAPI 规范", "%d 路径 / %d 错误码" % (paths, codes))
    else:
        rep.bad("API OpenAPI 规范", "%s paths=%s codes=%s" % (status, paths, codes))

    status, _h, body = sess.get("/api/v1/records?page=1&size=5")
    if status == 200 and isinstance(body, dict) \
            and {"items", "total", "page", "size", "has_more"} <= set(body):
        rep.ok("API v1 分页信封", "total=%s size=%s" % (body.get("total"), body.get("size")))
    else:
        rep.bad("API v1 分页信封", "%s %s" % (status, str(body)[:120]))

    status, _h, body = sess.get("/api/v1/records?limit=5&offset=5")
    if status == 200 and isinstance(body, dict) and body.get("page") == 2:
        rep.ok("API limit/offset 兼容换算", "page=%s size=%s" % (body.get("page"), body.get("size")))
    else:
        rep.bad("API limit/offset 兼容换算", "%s %s" % (status, str(body)[:120]))

    status, _h, body = sess.get("/api/v1/records/999999")
    if status == 404 and isinstance(body, dict) and str(body.get("code", "")).startswith("AIDBM-"):
        rep.ok("API 三段式错误", "%s / %s" % (body.get("code"), body.get("message")))
        if not isinstance(body.get("details"), (dict, list)):
            rep.bad("API 错误 details 字段", str(body.get("details"))[:80])
    else:
        rep.bad("API 三段式错误", "%s %s" % (status, str(body)[:120]))

    status, headers, body = sess.get("/api/meta")
    if headers.get("Deprecation") == "true" and "/api/v1/meta" in (headers.get("Link") or ""):
        rep.ok("兼容前缀弃用头", "Deprecation: true + Link successor")
    else:
        rep.bad("兼容前缀弃用头", "headers=%s" % {k: v for k, v in headers.items()
                                              if k.lower() in ("deprecation", "link")})

    status, _h, body = sess.get("/api/records?envelope=1&size=5")
    if status == 200 and isinstance(body, dict) and "items" in body:
        rep.ok("兼容前缀可选信封", "?envelope=1 生效")
    else:
        rep.bad("兼容前缀可选信封", "%s %s" % (status, str(body)[:120]))

    if anon is not None:
        status, _h, body = anon.get("/api/v1/records")
        if status == 401 and isinstance(body, dict) and body.get("code") == "AIDBM-1002":
            rep.ok("未登录错误契约", "401 + AIDBM-1002")
        else:
            rep.bad("未登录错误契约", "%s %s" % (status, str(body)[:120]))


def run_http_mode(base: str, rep: Report, keep: bool):
    sess = HttpSession(base)
    if sess.logged_in:
        rep.ok("登录成功（HTTP 会话）")
    else:
        rep.bad("登录成功（HTTP 会话）", "未拿到 session cookie")

    for path in PAGES:
        status, _h, html = sess.get(path, raw=True)
        if status >= 400:
            rep.bad("页面 " + path, "HTTP %s" % status)
            continue
        title = ""
        m = _TITLE_RE.search(html or "")
        if m:
            title = m.group(1).strip()
        if "Internal Server Error" in html or "服务器内部错误" in html:
            rep.bad("页面 " + path, "页面含服务端错误文案")
        elif not title:
            rep.bad("页面 " + path, "标题为空（可能渲染失败）")
        else:
            rep.ok("页面 " + path, title[:40])

    anon = HttpSession(base, login=False)
    check_api_contract(sess, rep, anon)


# ---------------------------------------------------------------- 浏览器模式

def run_browser_mode(base: str, rep: Report, headed: bool, slow_mo: int):
    from playwright.sync_api import sync_playwright

    console_errors: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed, slow_mo=slow_mo)
        ctx = browser.new_context(ignore_https_errors=True)
        page = ctx.new_page()
        page.on("console", lambda m: console_errors.append(m.text)
                if m.type == "error" and not any(k in m.text for k in CONSOLE_IGNORE)
                else None)
        page.on("pageerror", lambda e: console_errors.append("pageerror: %s" % e))
        page.on("response", lambda r: console_errors.append("http%d %s" % (r.status, r.url))
                if r.status >= 500 else None)

        try:
            page.goto(base + "/login", wait_until="domcontentloaded", timeout=30000)
            page.fill("#username", "admin")
            page.fill("#password", "admin123")
            page.click("#loginForm button[type=submit]")
            page.wait_for_load_state("domcontentloaded", timeout=30000)
            page.wait_for_timeout(800)
            if page.url.rstrip("/").endswith("/login"):
                rep.bad("登录成功（浏览器）", "仍停留在登录页")
            else:
                rep.ok("登录成功（浏览器）", page.url)
        except Exception as e:
            rep.bad("登录成功（浏览器）", str(e)[:200])

        for path in PAGES:
            try:
                resp = page.goto(base + path, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(250)
                title = (page.title() or "").strip()
                body = page.inner_text("body")[:400] if page.query_selector("body") else ""
                status = resp.status if resp else 0
                if status >= 400:
                    rep.bad("页面 " + path, "HTTP %s" % status)
                elif "Internal Server Error" in body or "服务器内部错误" in body:
                    rep.bad("页面 " + path, "页面含服务端错误文案")
                elif not title:
                    rep.bad("页面 " + path, "标题为空（可能渲染失败）")
                else:
                    rep.ok("页面 " + path, title[:40])
            except Exception as e:
                rep.bad("页面 " + path, str(e)[:160])

        js = """
        async () => {
          const out = {};
          const meta = await fetch('/api/v1/meta', {credentials:'same-origin'});
          out.meta = [meta.status, await meta.json()];
          const spec = await fetch('/api/v1/openapi.json', {credentials:'same-origin'});
          out.specStatus = spec.status;
          const s = await spec.json();
          out.specPaths = Object.keys(s.paths || {}).length;
          out.specCodes = (s['x-error-codes'] || []).length;
          const rec = await fetch('/api/v1/records?page=1&size=5', {credentials:'same-origin'});
          out.rec = [rec.status, await rec.json()];
          const nf = await fetch('/api/v1/records/999999', {credentials:'same-origin'});
          out.nf = [nf.status, await nf.json()];
          const lg = await fetch('/api/records?envelope=1&size=5', {credentials:'same-origin'});
          out.legacyHeaders = lg.headers.get('deprecation');
          return out;
        }
        """
        try:
            r = page.evaluate(js)
            if r["meta"][0] == 200 and r["meta"][1].get("api", {}).get("version") == "v1":
                rep.ok("API /api/v1/meta", "platform=%s" % r["meta"][1].get("platform", {}).get("name"))
            else:
                rep.bad("API /api/v1/meta", str(r["meta"])[:160])
            if r["specStatus"] == 200 and r["specPaths"] > 50 and r["specCodes"] > 10:
                rep.ok("API OpenAPI 规范", "%d 路径 / %d 错误码" % (r["specPaths"], r["specCodes"]))
            else:
                rep.bad("API OpenAPI 规范", str(r)[:160])
            body = r["rec"][1]
            if r["rec"][0] == 200 and isinstance(body, dict) and "items" in body \
                    and {"total", "page", "size", "has_more"} <= set(body):
                rep.ok("API v1 分页信封", "total=%s size=%s" % (body.get("total"), body.get("size")))
            else:
                rep.bad("API v1 分页信封", str(body)[:160])
            nf = r["nf"][1]
            if r["nf"][0] == 404 and str(nf.get("code", "")).startswith("AIDBM-"):
                rep.ok("API 三段式错误", nf.get("code"))
            else:
                rep.bad("API 三段式错误", str(r["nf"])[:160])
            if r["legacyHeaders"] == "true":
                rep.ok("兼容前缀弃用头", "Deprecation: true")
            else:
                rep.bad("兼容前缀弃用头", "缺少 Deprecation 头")
        except Exception as e:
            rep.bad("API 契约断言（浏览器内）", str(e)[:200])

        ctx.close()
        browser.close()

    if console_errors:
        uniq = sorted(set(console_errors))[:10]
        rep.bad("浏览器控制台/网络错误", "; ".join(u[:120] for u in uniq))
    else:
        rep.ok("浏览器控制台/网络无错误")


def main() -> int:
    ap = argparse.ArgumentParser(description="AIDBM 前端自动化冒烟")
    ap.add_argument("--mode", choices=("browser", "http"), default="browser",
                    help="browser=真实浏览器（CI）；http=无浏览器（本地/离线）")
    ap.add_argument("--base-url", default="", help="复用已运行实例（默认自起隔离实例）")
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--slow-mo", type=int, default=0)
    args = ap.parse_args()

    workdir = tempfile.mkdtemp(prefix="aidbm_ui_")
    server = None
    base = args.base_url.rstrip("/")
    rep = Report()
    try:
        if not base:
            port = _free_port()
            server = Server(port, workdir)
            server.start()
            base = "http://127.0.0.1:%d" % port
            print("隔离实例已启动: %s（工作目录 %s）" % (base, workdir))
        else:
            print("复用已有实例: %s" % base)

        if args.mode == "http":
            run_http_mode(base, rep, False)
        else:
            run_browser_mode(base, rep, args.headed, args.slow_mo)

        print("\n===== 前端冒烟结果（mode=%s）=====" % args.mode)
        for line in rep.passed:
            print("  [PASS] " + line)
        for line in rep.failed:
            print("  [FAIL] " + line)
        print("汇总: PASS %d / FAIL %d" % (len(rep.passed), len(rep.failed)))
        return 1 if rep.failed else 0
    except Exception as e:
        print("执行失败: %s" % e)
        if server:
            print("服务日志尾部：")
            try:
                with open(server.log, encoding="utf-8", errors="replace") as f:
                    print("".join(f.readlines()[-25:]))
            except Exception:
                pass
        return 2
    finally:
        if server:
            server.stop()
        if server is None:
            print("复用实例，未清理")
        else:
            shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
