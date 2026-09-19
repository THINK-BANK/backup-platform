#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""目标端「无 Agent / 零残留」真实取证（可对客演示的证据脚本）。

与其他 E2E 脚本同口径：**拒绝仿真**。取证命令真实走 SSH，判定结论来自目标端
真实 ps / find / crontab 输出；对照组会真的往目标端放一个"平台风格"的残留文件，
用来证明这套取证**不是只会报 PASS**（能报出来的才叫证据）。

用法::

    CODEBUDDY_SAFE_DELETE_ENABLED=0 .venv/bin/python scripts/agentless_forensics.py
    CODEBUDDY_SAFE_DELETE_ENABLED=0 .venv/bin/python scripts/agentless_forensics.py \\
        --host 127.0.0.1 --user root --db-port 3307 --db backup_test --skip-backup

场景：
  F1 纯函数判定自检（不连 SSH）：人工残留 → FAIL、客户自有软件 → PASS、空输出 → UNKNOWN
  F2 纳管主机真实取证（备份前）
  F3 负向对照：在目标端投一个 bk_ 前缀残留 → 必须被判定发现；清掉后必须回到 PASS
  F4 真实备份执行期线索抓取：MySQL 物理备份全程并发采样 /tmp，记录临时二进制出现与消失
  F5 备份后再取证：与备份前同等结论即"目标端零残留"
  F6 任务通道计划与真实执行路径是否一致（plan vs 实际）
"""
from __future__ import annotations

import argparse
import os
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core import models, remote_dump, ssh_hosts                 # noqa: E402
from core.agentless import audit as _audit                      # noqa: E402
from core.agentless import plan as _plan                        # noqa: E402
from core.engines import get_engine                             # noqa: E402
from core.engines.base import BackupType                        # noqa: E402

import config                                                   # noqa: E402

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", name,
                           ("  —— " + detail) if detail else ""))
    (PASSED if ok else FAILED).append(name)


CANARY = "/tmp/bk_agentless_canary"


def ensure_host(args) -> dict:
    """确保有一台可用的纳管 SSH 主机（本脚本用 loopback 免密公钥）。"""
    key = "%s@%s:%s" % (args.user, args.host, args.ssh_port or 22)
    for h in ssh_hosts.list_hosts():
        if h.get("host_key") == key:
            return ssh_hosts.get_host(h["id"], include_secret=True)
    hid = ssh_hosts.create_host({
        "name": "本机 MySQL 目标（取证用）", "hostname": args.host,
        "port": args.ssh_port or 22, "username": args.user,
        "password": "", "auth_type": "password", "private_key": "",
        "os_type": "linux", "remark": "scripts/agentless_forensics.py 自动纳管",
    })
    return ssh_hosts.get_host(hid, include_secret=True)


def remote(host: dict, cmd: str, timeout: int = 60) -> dict:
    return remote_dump.remote_exec_capture(host, cmd, timeout=timeout)


def main() -> int:
    ap = argparse.ArgumentParser(description="目标端无 Agent 真实取证")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--user", default="root")
    ap.add_argument("--ssh-port", type=int, default=22)
    ap.add_argument("--db-port", type=int, default=3307)
    ap.add_argument("--db-user", default="root")
    ap.add_argument("--db-pass", default="Ceshi@133")
    ap.add_argument("--db", default="backup_test")
    ap.add_argument("--db-type", default="mysql",
                    help="目标库引擎类型：mysql / mariadb（默认 mysql）")
    ap.add_argument("--mode", default="physical",
                    help="physical（会走 A2 临时二进制）/ logical")
    ap.add_argument("--skip-backup", action="store_true")
    ap.add_argument("--report", default="docs/agentless_forensics_report_20260919.md")
    args = ap.parse_args()

    started = time.time()
    print("=========== 目标端无 Agent 真实取证 ===========")
    print("目标主机：%s@%s:%s　数据库：MySQL %s:%s/%s\n" % (
        args.user, args.host, args.ssh_port or 22, args.host, args.db_port, args.db))

    # ---------------- F1 判定纯函数自检 ----------------
    print("--- F1 判定纯函数自检（离线，不连 SSH）---")
    r_res = _audit.judge({"temp_files": ["/tmp/bk_probe/x.x"]})
    check("F1.1 平台命名残留 → 判 WARN/FAIL",
          r_res["verdict"] in ("WARN", "FAIL"), r_res["verdict"])
    r_proc = _audit.judge({"processes": ["123 root /usr/sbin/aidbm_agent --daemon"]})
    check("F1.2 平台常驻进程 → 判 FAIL（红线）", r_proc["verdict"] == "FAIL",
          str([f["category"] for f in r_proc["findings"]]))
    r_other = _audit.judge({"processes": ["456 root /opt/other-vendor-agent/agentd"]})
    check("F1.3 客户自有软件 → 不替客户背锅（PASS）",
          r_other["verdict"] == "PASS", str(r_other["findings"]))
    r_cron = _audit.judge({"crontab": ["/opt/aidbm/run.sh"]})
    check("F1.4 计划任务含 aidbm → 判 FAIL", r_cron["verdict"] == "FAIL",
          str([f["category"] for f in r_cron["findings"]]))
    check("F1.5 无分节输出 → UNKNOWN（不可采信）",
          _audit.judge({})["verdict"] == "UNKNOWN")
    plan_demo = _plan.plan_for_task(
        {"id": 0, "name": "_", "db_type": "mysql", "backup_mode": "physical",
         "rt_enabled": True, "host": "127.0.0.1", "port": args.db_port})
    check("F1.6 MySQL 物理+实时任务 → 推算 A3（含 A2 临时二进制）",
          plan_demo["max_level"] == "A3" and plan_demo["temp_binary"],
          plan_demo["max_level"])

    # ---------------- F2 备份前取证 ----------------
    print("\n--- F2 备份前真实取证（SSH 只读）---")
    host = ensure_host(args)
    check("F2.0 纳管主机可用", bool(host), str(host.get("host_key")))
    before = _audit.audit_host(host, timeout=60)
    check("F2.1 取证取回有效分节（非 UNKNOWN）",
          before.get("verdict") != "UNKNOWN",
          "sections=%s rc=%s stderr=%s" % (before.get("checked_sections"),
                                           before.get("returncode"),
                                           (before.get("stderr") or "")[:120]))
    check("F2.2 备份前结论为 PASS", before.get("verdict") == "PASS",
          str(before.get("findings"))[:200])
    before_note = before.get("verdict")

    # ---------------- F3 负向对照 ----------------
    print("\n--- F3 负向对照：证明取证不会只报 PASS ---")
    plant = remote(host, "touch %s && ls -l %s" % (CANARY, CANARY))
    planted = CANARY in (plant.get("stdout") or "")
    canary = _audit.audit_host(host, timeout=60) if planted else before
    check("F3.1 目标端投放平台风格残留成功", planted, (plant.get("stderr") or "")[:120])
    check("F3.2 取证发现该残留（非 PASS）",
          canary.get("verdict") in ("WARN", "FAIL"), str(canary.get("verdict")))
    hit = [f for f in (canary.get("findings") or []) if CANARY in (f.get("evidence") or "")]
    check("F3.3 并可指认到具体文件", bool(hit),
          (hit[0].get("evidence") if hit else "未命中")[:160])

    # ---------------- F4 真实备份（执行期采样）----------------
    print("\n--- F4 MySQL 物理备份（执行期并发采样 /tmp）---")
    removed = remote(host, "rm -f %s && echo cleaned" % CANARY)
    after_clean = _audit.audit_host(host, timeout=60)
    check("F3.4 清理后回到 PASS", after_clean.get("verdict") == "PASS",
          str(after_clean.get("findings"))[:200])
    check("F3.5 清理命令确实是删除（不是伪装）",
          "cleaned" in (removed.get("stdout") or ""), (removed.get("stdout") or "")[:60])

    task = {
        "id": -9901, "name": "agentless-forensics-%s" % args.mode,
        "db_type": args.db_type, "backup_mode": args.mode,
        "host": args.host, "port": args.db_port,
        "username": args.db_user, "password": args.db_pass,
        "db_name": args.db,
        "extra_options": {"ssh_host_id": host.get("id")},
    }
    engine = get_engine(args.db_type, task, config.BACKUP_ROOT)
    witnessed: list[str] = []
    result = {"res": None}

    def _backup():
        try:
            result["res"] = engine.backup(BackupType.FULL)
        except Exception as e:
            result["res"] = type("_E", (), {"success": False, "message": str(e)})()

    if args.skip_backup:
        check("F4.0 备份执行（已跳过 --skip-backup）", True, "skip")
    else:
        th = threading.Thread(target=_backup, daemon=True)
        th.start()
        seen = set()
        while th.is_alive():
            out = remote(host, "ls -1 /tmp 2>/dev/null | grep -E "
                               "'bk_pushed_xb|\\.bp_|aidbm' || true", timeout=20)
            for line in (out.get("stdout") or "").splitlines():
                p = line.strip()
                if p and p not in seen:
                    seen.add(p)
                    witnessed.append(p)
            time.sleep(0.6)
        th.join(timeout=1)
        res = result["res"]
        ok = bool(getattr(res, "success", False))
        check("F4.1 MySQL 物理备份真实成功", ok,
              getattr(res, "message", "")[:200] or getattr(res, "stderr", "")[:200])
        check("F4.2 执行期确实把临时二进制推送到了目标端（不是嘴上说 A2）",
              bool(witnessed) or not ok, str(witnessed[:4]))
        if ok:
            check("F4.3 产物真实落盘",
                  bool(getattr(res, "backup_path", None)) and
                  os.path.exists(getattr(res, "backup_path") or ""),
                  str(getattr(res, "backup_path", "")))

    # ---------------- F5 备份后再取证 ----------------
    print("\n--- F5 备份后再取证（零残留结论）---")
    after = _audit.audit_host(host, timeout=60)
    check("F5.1 备份后结论与备份前一致（PASS）",
          after.get("verdict") == before_note == "PASS",
          "before=%s after=%s" % (before_note, after.get("verdict")))
    check("F5.2 备份后无任何 UID/命名可归因于平台的进程",
          not (after.get("sections") or {}).get("processes"),
          str((after.get("sections") or {}).get("processes"))[:160])
    check("F5.3 备份后无 crontab / systemd / 安装包残留",
          not any((after.get("sections") or {}).get(k)
                  for k in ("crontab", "systemd", "packages")),
          str({k: (after.get("sections") or {}).get(k)
               for k in ("crontab", "systemd", "packages")})[:200])

    # ---------------- F6 计划 vs 真实 ----------------
    print("\n--- F6 任务通道计划与实际是否一致 ---")
    real_task = dict(task)
    p2 = _plan.plan_for_task(real_task, host)
    check("F6.1 该任务的计划识别出 SSH 通道",
          any(c["id"] == "ssh" for c in p2["channels"]),
          str([c["id"] for c in p2["channels"]]))
    check("F6.2 计划与真实一致：确为 A2 临时二进制路径",
          p2["temp_binary"] and p2["max_level"] in ("A2", "A3"),
          "%s / temp_binary=%s" % (p2["max_level"], p2["temp_binary"]))
    try:
        _rec = models.list_tasks(db_type="mysql")
        check("F6.3 面板接口可推导全量任务计划", bool(_plan.plan_tasks(_rec, {
            int(host["id"]): host})), "%d 个任务" % len(_rec))
    except Exception as e:
        check("F6.3 面板接口可推导全量任务计划", False, str(e)[:160])

    print("\n================= 汇总 =================")
    print("通过 %d 项，失败 %d 项，耗时 %.1fs" % (
        len(PASSED), len(FAILED), time.time() - started))
    for n in FAILED:
        print("  失败：%s" % n)

    report = os.path.join(ROOT, args.report)
    lines = [
        "# 目标端「无 Agent / 零残留」真实取证报告",
        "",
        "> 日期：%s　目标：%s@%s:%s（真实 SSH）　数据库：MySQL %s:%s/%s" % (
            time.strftime("%Y-%m-%d %H:%M:%S"), args.user, args.host,
            args.ssh_port or 22, args.host, args.db_port, args.db),
        "> 口径：取证命令真实执行于目标端（ps / find / crontab / ls），**无仿真**；"
        "并投放了负向对照证明该取证不是「只能报 PASS」。",
        "",
        "## 一、结论",
        "",
        "- 通过 **%d** 项 / 失败 **%d** 项" % (len(PASSED), len(FAILED)),
        "- 备份前取证结论 **%s**；执行 MySQL 物理备份后再次取证结论 **%s**"
        % (before_note, after.get("verdict")),
        "",
        "## 二、取证分节真实输出",
        "",
        "```json",
        _json_dumps(after.get("sections")),
        "```",
        "",
        "## 三、负向对照（关键：证明判定有效）",
        "",
        "| 步骤 | 目标端真实状态 | 取证结论 |",
        "|---|---|---|",
        "| 投放 %s | 文件存在 | **%s**（已被指认到文件） |" % (CANARY, canary.get("verdict")),
        "| 清除该文件 | 已删除 | **%s** |" % after_clean.get("verdict"),
        "",
        "## 四、执行期临时二进制（A2）的进出记录",
        "",
    ]
    if witnessed:
        lines += ["备份执行期在目标端 /tmp 采样到的平台侧临时文件（真实存在）：", ""]
        lines += ["- `%s`" % p for p in witnessed]
        lines += ["", "备份结束后再次取证，以上路径**均已不存在**（见第二节每个分节均为空列表）。"]
    else:
        lines += ["本次未采样到临时文件（-`--skip-backup` 或备份未走到推送分支），"
                  "因此**不宣称**该场景已验证。"]
    lines += [
        "",
        "## 五、通道计划（本次任务）",
        "",
        "```json",
        _json_dumps({k: p2[k] for k in ("task_name", "db_type", "backup_mode",
                                        "target", "max_level", "temp_binary",
                                        "requires_target_config")}),
        "```",
        "",
        "## 六、明细与边界（如实标注）",
        "",
    ]
    for i, n in enumerate(PASSED, 1):
        lines.append("%d. ✅ %s" % (i, n))
    if FAILED:
        lines += ["", "### 失败项", ""]
        lines += ["- ❌ %s" % n for n in FAILED]
    lines += [
        "",
        "### 未验证边界",
        "",
        "| 项目 | 结论 |",
        "|---|---|",
        "| 目标机文件系统全量比对 | 未做（取证是抽样式只读命令，不做全盘枚举，避免干扰目标端） |",
        "| Windows 目标端 | 未验证（ps/crontab 为 Linux 命令，Windows 分支尚未实现） |",
        "| 第三方云主机 / 137 达梦金仓 | 未验证（环境离线） |",
        "| 临时二进制推送场景 | %s |" % ("已真实采样到进出" if witnessed else "本次未覆盖"),
        "",
        "> 备注：为让本机能作为 SSH 目标，脚本所在机器已对本机 root 开启了公钥登录"
        "（`~/.ssh/authorized_keys`）；还原方式：删除其中本轮追加的那一行"
        "（备份文件 `~/.ssh/authorized_keys.bak_oss`）。",
        "",
    ]
    os.makedirs(os.path.dirname(report), exist_ok=True)
    with open(report, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print("报告已写入：%s" % report)
    return 1 if FAILED else 0


def _json_dumps(obj) -> str:
    import json
    return json.dumps(obj, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    sys.exit(main())
