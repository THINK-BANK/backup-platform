#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""目标端免装取证命令行工具（P0，见 docs/agentless_architecture_20260919.md）。

对纳管主机做**只读**检查，证明"AIDBM 没有在被保护对象上装东西、没有留下常驻进程"。

用法::

    .venv/bin/python scripts/no_agent_audit.py --all
    .venv/bin/python scripts/no_agent_audit.py --host-id 3
    .venv/bin/python scripts/no_agent_audit.py --all --json artifacts/no_agent.json

退出码：0=全部 PASS/WARN；1=存在 FAIL；2=取证执行失败（UNKNOWN）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def main() -> int:
    p = argparse.ArgumentParser(description="AIDBM 目标端免装取证")
    p.add_argument("--host-id", type=int, action="append", default=[])
    p.add_argument("--all", action="store_true", help="检查全部纳管主机")
    p.add_argument("--timeout", type=int, default=60)
    p.add_argument("--json", default="", help="报告落盘路径")
    args = p.parse_args()

    from core import ssh_hosts  # noqa: E402  （延迟到 argparse 之后再导入）
    from core.agentless import audit as _audit  # noqa: E402

    if not args.all and not args.host_id:
        p.error("需要 --all 或至少一个 --host-id")
        return 2

    if args.all:
        hosts = [h for h in ssh_hosts.list_hosts(include_secret=True)]
        if args.host_id:
            ids = set(args.host_id)
            hosts = [h for h in hosts if h.get("id") in ids]
    else:
        hosts = []
        for hid in args.host_id:
            h = ssh_hosts.get_host(hid, include_secret=True)
            if not h:
                print("纳管主机不存在: %s" % hid)
                return 2
            hosts.append(h)

    if not hosts:
        print("没有可检查的纳管主机")
        return 2

    reports, worst = [], 0
    for h in hosts:
        rep = _audit.audit_host(h, timeout=args.timeout)
        reports.append(rep)
        verdict = rep.get("verdict")
        worst = max(worst, {"PASS": 0, "WARN": 0, "FAIL": 1, "UNKNOWN": 2}[verdict])
        print("\n" + _audit.audit_report_text(rep))

    print("\n===== 汇总 =====")
    for rep in reports:
        print("  %-40s %s" % (rep.get("host"), rep.get("verdict")))

    if args.json:
        os.makedirs(os.path.dirname(os.path.abspath(args.json)), exist_ok=True)
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump({"checked_at": reports[0].get("checked_at"),
                       "reports": reports}, f, ensure_ascii=False, indent=2)
        print("报告已落盘: %s" % args.json)

    return 1 if worst >= 1 else (2 if worst == 2 else 0)


if __name__ == "__main__":
    sys.exit(main())
