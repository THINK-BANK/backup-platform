# -*- coding: utf-8 -*-
"""目标端「无 Agent」取证审计（P0）。

见 docs/agentless_architecture_20260919.md §3/§4：把"不装 Agent"变成可验证结论。

三条硬原则：
1. **只读取证**：只用 ps / find / 列表类命令读取目标端状态，绝不写入或删除任何东西；
2. **判定是纯函数**：``judge()`` 不依赖 SSH，输入"分节文本"输出判定，可离线单测；
3. **只归因自己的东西**：只有能归属本平台的模式（``bk_*``、``.bp_*``、``*.bkdone``、
   ``xtrabackup/mariabackup`` 临时副本、``aidbm``）才判 FAIL/WARN；客户自行安装的
   备份软件只给_INFO_——没装过的东西我们不能替客户背书，反之也不能替客户背锅。
"""
from __future__ import annotations

import datetime as _dt
import re

from .channels import INVASION_LEVELS, FORBIDDEN

_SECTION_MARK = "#####"

#: 平台在目标端可能留下的可归因命名模式
_ATTR_RESIDUE = re.compile(
    r"(xtrabackup|mariabackup|/\.bp_|/bp_work|/bk_|\.bkdone|aidbm[\-_])", re.I)
#: 只作为参照物列出、不做判定的行（客户自己的东西）
_INFO_ONLY = re.compile(r"^info:", re.I)

#: 取证分项：(名称, 只读命令)
AUDIT_COMMANDS: list[tuple[str, str]] = [
    ("processes",
     "ps -eo pid,user,args --no-headers 2>/dev/null | grep -E "
     "'xtrabackup|mariabackup|\\.bp_|bp_work|aidbm[-_]|bk_dump' | grep -v grep || true"),
    ("temp_files",
     "find /tmp /var/tmp -maxdepth 3 \\( -name 'bk_*' -o -name '.bp_*' "
     "-o -name 'bp_work*' -o -name '*xtrabackup*' -o -name '*mariabackup*' "
     "-o -name '*.bkdone' \\) -print 2>/dev/null || true"),
    ("crontab",
     "(crontab -l 2>/dev/null || true) | grep -E 'aidbm|bk_|\\.bp_' | grep -v grep || true; "
     "grep -rIl 'aidbm' /var/spool/cron /etc/cron.d 2>/dev/null || true"),
    ("systemd",
     "ls /etc/systemd/system 2>/dev/null | grep -iE 'aidbm|bp_backup' || true"),
    ("packages",
     "(command -v rpm >/dev/null 2>&1 && rpm -qa 2>/dev/null | grep -iE 'aidbm' || true); "
     "(command -v dpkg-query >/dev/null 2>&1 && dpkg-query -W -f='${Package}\\n' "
     "2>/dev/null | grep -iE 'aidbm' || true)"),
]

#: 每一项的判定语义。``attribution`` 为该节的归因正则：默认是严格模式
#: （必须出现平台命名才归因）；crontab/systemd/packages 三节的取证命令已带
#: ``aidbm`` 过滤，命中该词即算归因，否则 ``/opt/aidbm/run.sh`` 这类路径会漏判。
_SECTION_RULES: dict[str, dict] = {
    "processes": {"category": "目标端残留进程", "severity": "critical",
                  "level": "X"},
    "temp_files": {"category": "执行期临时文件未清理", "severity": "warn",
                   "level": "A2"},
    "crontab": {"category": "目标端计划任务", "severity": "critical", "level": "X",
                "attribution": re.compile(r"aidbm|/\.bp_|/bk_", re.I)},
    "systemd": {"category": "目标端自启服务", "severity": "critical", "level": "X",
                "attribution": re.compile(r"aidbm|bp_backup", re.I)},
    "packages": {"category": "目标端安装了平台侧软件", "severity": "critical",
                 "level": "X", "attribution": re.compile(r"aidbm", re.I)},
}


# ---------------------------------------------------------------- 取证脚本

def build_audit_shell() -> str:
    """生成只读取证脚本（分节输出，供 :func:`parse_sections` 解析）。"""
    parts = ["echo '%s begin %s'" % (_SECTION_MARK, _SECTION_MARK)]
    for name, cmd in AUDIT_COMMANDS:
        parts.append("echo '%s %s %s'" % (_SECTION_MARK, name, _SECTION_MARK))
        parts.append(cmd)
    parts.append("echo '%s end %s'" % (_SECTION_MARK, _SECTION_MARK))
    return "\n".join(parts)


def parse_sections(text: str) -> dict[str, list[str]]:
    """把分节输出解析为 ``{name: [非空行]}``。"""
    out: dict[str, list[str]] = {}
    cur: str | None = None
    for line in (text or "").splitlines():
        line = line.rstrip()
        m = re.match(r"^%s\s+(begin|end|[\w]+)\s+%s$" % (_SECTION_MARK, _SECTION_MARK),
                     line.strip())
        if m:
            token = m.group(1)
            cur = None if token in ("begin", "end") else token
            if cur:
                out.setdefault(cur, [])
            continue
        if cur and line.strip():
            out[cur].append(line.strip())
    return out


# ---------------------------------------------------------------- 判定（纯函数）

def judge(sections: dict[str, list[str]]) -> dict:
    """依据取证分节输出判定None-Agent 结论。

    返回 ``{"verdict": PASS|WARN|FAIL|UNKNOWN, "findings": [...],
    "checked_sections": [...]}``。
    """
    if not sections:
        return {"verdict": "UNKNOWN", "findings": [
            {"category": "取证失败", "severity": "unknown", "evidence":
             "未取回任何分节输出（SSH 失败或目标端不支持相关命令），结论不可信"}],
            "checked_sections": []}

    findings = []
    for name, lines in sorted(sections.items()):
        rule = _SECTION_RULES.get(name)
        for line in lines:
            if not line:
                continue
            if _INFO_ONLY.match(line):
                continue
            if rule is None:
                continue
            typ = rule.get("attribution") or _ATTR_RESIDUE
            if not typ.search(line):
                # 命中正则范围但未归因到平台（例如客户自己的备份软件），只记录信息
                continue
            findings.append({
                "category": rule["category"],
                "severity": rule["severity"],
                "invasion_level": rule["level"],
                "evidence": line[:400],
            })

    if any(f["severity"] == "critical" for f in findings):
        verdict = "FAIL"
    elif findings:
        verdict = "WARN"
    else:
        verdict = "PASS"
    return {"verdict": verdict, "findings": findings,
            "checked_sections": sorted(sections.keys())}


# ---------------------------------------------------------------- 远程执行

def audit_host(ssh_host: dict, timeout: int = 60) -> dict:
    """对一台 SSH 主机做免装取证。

    :param ssh_host: ``core/ssh_hosts.get_host(..., include_secret=True)`` 的字典
    :return: 报告 ``{host, checked_at, returncode, sections, verdict, findings, ...}``
    """
    from core import remote_dump  # 延迟导入：审计脚本不应拖累常规启动

    target = ssh_host.get("host_key") or ssh_host.get("host") or "?"
    res = remote_dump.remote_exec_capture(ssh_host, build_audit_shell(),
                                          timeout=timeout)
    sections = parse_sections(res.get("stdout") or "")
    verdict = judge(sections)
    report = {
        "host": target,
        "checked_at": _dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "returncode": res.get("returncode"),
        "stderr": (res.get("stderr") or "")[:500],
        "read_only": True,
        "invasion_levels": INVASION_LEVELS,
        "forbidden": list(FORBIDDEN),
        "sections": sections,
    }
    report.update(verdict)
    return report


def audit_report_text(report: dict) -> str:
    """把报告渲染成人可读文本（CLI / 报告文件使用）。"""
    lines = [
        "目标端免装取证报告",
        "主机: %s" % report.get("host"),
        "时间: %s" % report.get("checked_at"),
        "命令返回码: %s（取证命令均为只读）" % report.get("returncode"),
        "结论: %s" % report.get("verdict"),
        "已检查项: %s" % ", ".join(report.get("checked_sections") or []),
        "",
    ]
    findings = report.get("findings") or []
    if not findings:
        lines.append("未发现可归因于 AIDBM 的目标端残留（无进程、无计划任务、"
                     "无自启服务、无遗留临时文件）。")
    else:
        lines.append("发现 %d 项：" % len(findings))
        for f in findings:
            lines.append("  [%s] %s（侵入等级 %s）" % (
                f.get("severity"), f.get("category"), f.get("invasion_level")))
            lines.append("      证据: %s" % f.get("evidence"))
    return "\n".join(lines)
