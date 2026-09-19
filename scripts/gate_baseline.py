#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""全量回归的基线比对（质量门禁的一部分，见 scripts/test_gate.sh --full）。

为什么要基线而不是「全绿」：本平台仓库存在**存量**环境依赖失败（缺真实数据库
目标、SSH 目标、外部客户端等），这些失败与本次改动无关。门禁的职责是
**禁止新增失败**，而不是要求存量立刻全绿——所以把当前统计固化为基线，
每次回归只允许「失败数不增、通过数不减」。

用法::

    python scripts/gate_baseline.py --junit artifacts/junit-full.xml --mode check
    python scripts/gate_baseline.py --junit artifacts/junit-full.xml --mode update
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import sys
import xml.etree.ElementTree as ET


def parse_junit(path: str) -> dict:
    root = ET.parse(path).getroot()
    total = failed = errors = skipped = 0
    by_module: dict[str, dict] = collections.defaultdict(
        lambda: {"total": 0, "failed": 0})
    for tc in root.iter("testcase"):
        cn = tc.get("classname") or ""
        mod = ".".join(cn.split(".")[:2]) if cn.startswith("tests.") else cn.split(".")[0]
        bad = [c.tag for c in tc if c.tag in ("failure", "error")]
        skipped_here = any(c.tag == "skipped" for c in tc)
        total += 1
        by_module[mod]["total"] += 1
        if bad:
            if "error" in bad:
                errors += 1
            else:
                failed += 1
            by_module[mod]["failed"] += 1
        elif skipped_here:
            skipped += 1
    passed = total - failed - errors - skipped
    return {"total": total, "passed": passed, "failed": failed,
            "errors": errors, "skipped": skipped,
            "failing_modules": {k: v for k, v in by_module.items() if v["failed"]},
            "modules_total": len(by_module)}


def load(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save(path: str, data: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)


def main() -> int:
    ap = argparse.ArgumentParser(description="全量回归基线比对门禁")
    ap.add_argument("--junit", required=True)
    ap.add_argument("--baseline", required=True)
    ap.add_argument("--mode", choices=("check", "update"), default="check")
    args = ap.parse_args()

    now = parse_junit(args.junit)
    if args.mode == "update":
        now["note"] = ("存量环境依赖失败基线（缺真实 DB/SSH 目标等）。"
                       "门禁要求：failed/errors 不高于此值，passed 不低于此值。"
                       "修复存量失败后请重跑 --update-baseline 收紧基线。")
        save(args.baseline, now)
        print("基线已更新：%s" % args.baseline)
        print("  total=%d passed=%d failed=%d errors=%d skipped=%d"
              % (now["total"], now["passed"], now["failed"], now["errors"],
                 now["skipped"]))
        return 0

    if not os.path.exists(args.baseline):
        print("缺少基线文件 %s（先执行 scripts/test_gate.sh --update-baseline）"
              % args.baseline)
        return 1
    base = load(args.baseline)

    problems = []
    if now["failed"] > base.get("failed", 0):
        problems.append("失败数增加：%d -> %d" % (base.get("failed", 0), now["failed"]))
    if now["errors"] > base.get("errors", 0):
        problems.append("错误数增加：%d -> %d" % (base.get("errors", 0), now["errors"]))
    if now["passed"] < base.get("passed", 0):
        problems.append("通过数下降：%d -> %d（可能有用例被删除/跳过）"
                        % (base.get("passed", 0), now["passed"]))

    improvement = []
    if now["failed"] < base.get("failed", 0) or now["errors"] < base.get("errors", 0):
        improvement.append("存量失败减少：failed %d->%d, errors %d->%d（建议收紧基线）"
                           % (base.get("failed", 0), now["failed"],
                              base.get("errors", 0), now["errors"]))

    print("全量回归比对（基线 %s）" % os.path.basename(args.baseline))
    print("  基线: total=%d passed=%d failed=%d errors=%d skipped=%d"
          % (base.get("total", 0), base.get("passed", 0), base.get("failed", 0),
             base.get("errors", 0), base.get("skipped", 0)))
    print("  本次: total=%d passed=%d failed=%d errors=%d skipped=%d"
          % (now["total"], now["passed"], now["failed"], now["errors"],
             now["skipped"]))
    base_mods = base.get("failing_modules", {})
    for mod, info in sorted(now["failing_modules"].items()):
        old = base_mods.get(mod, {}).get("failed", 0)
        mark = "新增!" if info["failed"] > old else ("已减少" if info["failed"] < old else "")
        if mark:
            print("    %-46s 失败 %d（基线 %d） %s" % (mod, info["failed"], old, mark))
    for note in improvement:
        print("  [提示] " + note)
    if problems:
        for p in problems:
            print("  [门禁失败] " + p)
        return 1
    print("  结论：无新增失败、无通过数下降，零回归 ✔")
    return 0


if __name__ == "__main__":
    sys.exit(main())
