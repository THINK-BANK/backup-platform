#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AIDBM 代码图谱生成器（Code Graph Generator）。

用途
----
在长期迭代的项目里，"改一个功能要先通读几万行代码"是最大的时间黑洞。
本脚本用纯标准库（ast / os / json / argparse）对整个仓库做静态扫描，
生成一份**关系图谱**，替代"从头读代码"，直接回答这些问题：

  * 系统分几层？每层有哪些模块？各模块职责一句话是什么？
  * 模块 A 依赖谁、被谁依赖？改 A 会不会炸到别人？
  * 有没有循环依赖 / 越层依赖？
  * 所有 REST 接口的 METHOD + URL + handler 位置 + 它调用了哪些能力模块？
  * 页面 → 前端 JS → 调用的后端接口，这条链是怎么接的？
  * 有哪些数据表，被哪些模块读写？
  * 关键业务链路（备份/恢复/实时/同步/迁移/克隆…）依次经过哪些文件？
  * 想改 XXX 功能，应该先看哪几个文件？
  * 与上一次生成相比，代码结构发生了什么变化（--diff）？

产出（默认写到 docs/）
----------------------
  code_graph.md    人类 / LLM 阅读主入口（分层图 + 模块卡片 + 索引 + 链路）
  code_graph.json  机器可读全量索引（incremental 迭代与 --diff 依赖它）

用法
----
  python scripts/gen_code_graph.py                  # 重新生成图谱
  python scripts/gen_code_graph.py --diff           # 只输出相对上次的结构变更摘要
  python scripts/gen_code_graph.py --include-all    # 连 tests/、scripts/ 也纳入
  python scripts/gen_code_graph.py --json-only      # 只更新机器可读索引
  python scripts/gen_code_graph.py --out-dir docs   # 指定输出目录
  python scripts/gen_code_graph.py --strict         # 存在不可信数据时以非 0 退出（CI 用）

可信度约定：图谱里凡不是 AST 确证的条目都会记入 §0.1 的「可信度账本」，
要么标 ⚠/❌ 显式提示，要么在 --strict 下让命令失败——不允许"看起来正确"的推断数据。

设计约束
--------
  * 零第三方依赖（离线自足，与平台整体约束一致）
  * 不执行被分析的代码（只 ast.parse），只读，不写源码
  * 领域注解（分层规则 / 业务链路 / 改动指引）集中在本文件的 DOMAIN_* 常量里，
    生成时会做**真实性校验**：写明的文件或函数不存在会在图谱里显式标注失效，
    防止图谱随代码演进而悄悄腐化。
"""
import argparse
import ast
import datetime as _dt
import io
import json
import os
import re
import sys
from collections import defaultdict

# ============================================================================
# 一、扫描范围配置
# ============================================================================

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 默认排除的目录（性能 + 噪声：产物、依赖、第三方 vendor、交付包）
EXCLUDE_DIRS = {
    "__pycache__", ".git", ".github", ".venv", "venv", "node_modules",
    ".idea", ".vscode", ".pytest_cache", ".mypy_cache", ".codebuddy",
    "drivers", "docs", "packages", "backups", "logs", ".tools",
    "dist", "build", "resources", "vendor",
}

# 默认分析 roots（相对项目根）
DEFAULT_ROOTS = ["core", "api", "templates", "static/js"]
# 根目录散落的入口 .py
ROOT_PY_FILES = ["app.py", "auth.py", "config.py", "init_db.py"]
# 可选纳入（--include-all）
OPTIONAL_ROOTS = ["tests", "scripts", "tools"]

# ============================================================================
# 二、领域注解（人工维护，生成时做存在性校验）
# ============================================================================

# ----------------------------------------------------------------------------
# 可信度账本：图谱里凡【不是 AST 确证】的东西都必须进这个列表。
#   原则 —— 要么真实，要么显式报错；不允许出现"看起来正确但其实靠猜"的数据。
#   ISSUES 里的条目会写进 §0.1，并在 `--strict` 下让脚本以非 0 退出。
# ----------------------------------------------------------------------------
ISSUES = []           # [(severity, 类型, 说明)]，severity ∈ {"WARN", "ERROR"}


def _issue(sev, kind, detail):
    ISSUES.append((sev, kind, detail))


def _is_placeholder(path):
    """路径里含 <xxx> 属于「模板占位」（如 core/engines/<新引擎>.py），不是真实文件。"""
    return "<" in path and ">" in path

# 分层规则：(层名, 前缀列表)。自左向右 = 【由高层到低层】（数字越大越底层）。
# 匹配用**最长前缀**，所以子包规则（如 core/engines/）要写在父包（core/）之前也无所谓。
#   L0 是全局基座：任何层都允许直接依赖它，不参与越层判定。
#   L1~L5 的规矩：只能依赖【更低的层】（层号更大），或 L0。
LAYER_RULES = [
    ("L0 全局基座", [
        "config.py", "auth.py", "core/__init__.py", "core/db.py", "core/models.py",
        "core/logging_setup.py",
    ]),
    ("L1 入口装配", ["app.py", "init_db.py"]),
    ("L2 接口层", ["api/"]),
    ("L4 能力执行层", [
        "core/engines/", "core/cdc/", "core/rt_backup/", "core/sync/",
        "core/vm/", "core/storage_backends/", "core/ai_agent/",
        "core/logical_full.py",
    ]),
    ("L5 基础设施层", [
        "core/remote_dump.py", "core/storage.py", "core/rbac.py", "core/policy.py",
        "core/jdbc.py", "core/native_conn.py", "core/probe.py", "core/ssh_hosts.py",
        "core/crypto_pool.py", "core/db_adapters.py", "core/plugin_runtime.py",
        "core/plugin_catalog.py", "core/plugin_installer.py",
        "core/notifier.py", "core/webhooks.py", "core/oplog.py", "core/itsm.py",
    ]),
    ("L3 业务编排层", ["core/"]),
    ("L6 其他/未归类", []),
]

# 显式的层高低顺序：**值越大越底层**。
# 单独定义是为了让 LAYER_RULES 只负责匹配、本书不影响语义；
# 依赖合规 iff order[dst] > order[src]（依赖更底层）或 dst 是 L0。
LAYER_ORDER = {
    "L0 全局基座": 0,
    "L1 入口装配": 1,
    "L2 接口层": 2,
    "L3 业务编排层": 3,
    "L4 能力执行层": 4,
    "L5 基础设施层": 5,
    "L6 其他/未归类": 6,
}
LAYER0 = LAYER_RULES[0][0]
# 未归类层：落到这里说明 LAYER_RULES 没覆盖到，属于规则缺口而不是业务范围
LAYER_OTHER = "L6 其他/未归类"

# 关键业务链路：(链路名, [(标签, 相对路径, 可选函数名或可空)])
# 生成时逐个校验：文件不存在→标红，函数不存在→标注"函数名已变"。
CHAINS = [
    ("定时/手动备份主链路", [
        ("HTTP 触发", "api/tasks.py", None),
        ("调度器", "core/scheduler.py", "_execute_backup_core"),
        ("任务数据读写", "core/models.py", None),
        ("引擎分发", "core/engines/__init__.py", None),
        ("引擎基类契约", "core/engines/base.py", "BackupEngine.run_backup"),
        ("具体引擎", "core/engines/mysql.py", None),
        ("远端/本机执行", "core/remote_dump.py", None),
        ("产物落盘", "core/storage.py", None),
    ]),
    ("数据恢复主链路", [
        ("HTTP 触发", "api/restore.py", None),
        ("本地恢复", "core/restore_extras.py", None),
        ("跨机恢复", "core/cross_host.py", None),
        ("引擎恢复实现", "core/engines/base.py", "BackupEngine.run_restore"),
    ]),
    ("实时备份（CDC/CDP）链路", [
        ("HTTP 入口", "api/rt.py", None),
        ("任务编排", "core/rt_backup/db_rt.py", None),
        ("变更捕获工厂", "core/cdc/__init__.py", None),
        ("MySQL binlog", "core/cdc/mysql_binlog.py", None),
        ("行级回放", "core/cdc/rowlevel.py", None),
        ("PITR 恢复点", "core/rt_backup/pitr.py", None),
        ("日志段台账", "core/rt_backup/journal.py", None),
        ("进程外看护", "core/rt_backup/supervisor.py", None),
    ]),
    ("数据同步链路", [
        ("HTTP 入口", "api/sync.py", None),
        ("同步引擎", "core/sync/engine.py", None),
        ("预检查", "core/sync/precheck.py", None),
        ("数据源插件", "core/sync/plugins/mysql.py", None),
        ("类型映射", "core/sync/type_mapper.py", None),
        ("连接层", "core/native_conn.py", None),
    ]),
    ("数据迁移链路", [
        ("HTTP 入口", "api/migration.py", None),
        ("迁移编排", "core/db_migrate.py", None),
        ("同步引擎复用", "core/sync/engine.py", None),
        ("源端探测", "core/probe.py", None),
    ]),
    ("克隆（VDB 直通）链路", [
        ("HTTP 入口", "api/clone.py", None),
        ("克隆服务", "core/clone_service.py", None),
        ("引擎克隆", "core/restore_extras_clone.py", None),
    ]),
    ("容灾联动链路", [
        ("HTTP 入口", "api/link.py", None),
        ("联动编排", "core/disaster_link.py", None),
        ("演练", "core/drill.py", None),
        ("备用还原", "core/synthetic.py", None),
    ]),
    ("虚拟机备份链路", [
        ("HTTP 入口", "api/vm.py", None),
        ("虚拟机能力层", "core/vm/", None),
    ]),
    ("AI 助手链路", [
        ("HTTP 入口", "api/ai_agent.py", None),
        ("Agent 内核", "core/ai_agent/", None),
        ("告警分析", "core/ai_alert.py", None),
    ]),
    ("生命周期/保留策略", [
        ("HTTP 入口", "api/lifecycle.py", None),
        ("策略引擎", "core/policy.py", None),
        ("GFS 保留", "core/retention_gfs.py", None),
        ("生命周期执行", "core/lifecycle.py", None),
    ]),
]

# 改动指引：(我想改什么, [涉及文件])
CHANGE_GUIDE = {
    "新增一种数据库类型的备份能力": [
        "core/engines/<新引擎>.py", "core/engines/__init__.py",
        "core/plugin_catalog.py", "core/probe.py", "config.py",
    ],
    "新增/修改一个 REST 接口": ["api/<模块>.py", "api/__init__.py"],
    "调整备份产物存放与多后端存储": [
        "core/storage.py", "core/storage_backends/", "api/storage.py",
    ],
    "调整调度与并发行为": ["core/scheduler.py", "core/models.py", "config.py"],
    "调整远端命令执行（免安装/工具解析）": [
        "core/remote_dump.py", "core/ssh_hosts.py",
    ],
    "新增/修改 CDC 数据源": [
        "core/cdc/<新源>.py", "core/cdc/__init__.py", "core/cdc/rowlevel.py",
    ],
    "新增/修改实时备份（CDP/PITR）行为": [
        "core/rt_backup/db_rt.py", "core/rt_backup/pitr.py",
        "core/rt_backup/journal.py", "core/rt_backup/supervisor.py",
    ],
    "同步/迁移类型映射或整表迁移": [
        "core/sync/engine.py", "core/sync/type_mapper.py",
        "core/sync/type_matrix.py", "core/sync/precheck.py",
    ],
    "新增数据目标（同步写入端）插件": [
        "core/sync/plugins/<目标>.py", "core/sync/plugins/base.py",
    ],
    "改造前端页面": ["templates/<页>.html", "static/js/app.js", "api/<对应>.py"],
    "改动元数据库表结构": ["core/db.py", "core/models.py", "init_db.py"],
    "恢复校验报告": ["core/restore_verify.py", "api/restore_verify.py"],
    "安全/鉴权/CSRF/审计": ["auth.py", "api/__init__.py", "core/rbac.py", "core/oplog.py"],
}

# 运行时拓扑（进程/线程/守护），供阅读者建立"跑起来是什么样"的心智模型
RUNTIME_TOPOLOGY = [
    ("Flask Web 进程", "app.py:create_app",
     "注册 api_bp（api/__init__.py 内含全局鉴权+CSRF 钩子）与全部页面路由；"
     "生产建议 gunicorn 多 worker。" ),
    ("APScheduler 后台调度", "core/scheduler.py",
     "进程内调度器，按 cron/interval 触发备份任务；任务变更时 reload_scheduler 重建 job。"),
    ("实时任务看护进程", "core/rt_backup/supervisor.py",
     "独立于 Web 进程的 supervisor，按 rt_supervisor.lock 单实例运行，"
     "崩溃后自动拉起 rt 守护。" ),
    ("实时守护线程", "core/rt_backup/db_rt.py",
     "每个启用实时的任务一个守护线程，负责 CDC 捕获/落盘/封段。"),
    ("异步后台线程", "core/clone_service.py:_provision_async",
     "克隆拉起等长耗时操作用后台线程执行，前端轮询状态（core/db_migrate.py 同理）。"),
    ("请求即执行", "api/__init__.py",
     "大部分接口同步执行并返回结果（重任务由调度器/线程承接，接口只返回受理结果）。"),
]


# ============================================================================
# 三、静态分析核心
# ============================================================================

_PY_TOPLEVEL = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
_ROUTE_ATTR_RE = re.compile(r"^(.*)_bp$")

_MISSING = object()


def _cv(node):
    """取常量节点的字面值，兼容 py3.6(ast.Str/Num) 与 py3.8+(ast.Constant)。

    未命中返回 _MISSING（不能用 None，因为常量本身可能是 None）。
    """
    if hasattr(ast, "Constant") and isinstance(node, ast.Constant):
        return node.value
    if hasattr(ast, "Str") and isinstance(node, ast.Str):
        return node.s
    if hasattr(ast, "Num") and isinstance(node, ast.Num):
        return node.n
    return _MISSING


def _cv_str(node):
    v = _cv(node)
    return v if isinstance(v, str) else None


def _read_text(path):
    for enc in ("utf-8", "utf-8-sig", "gb18030", "latin-1"):
        try:
            with io.open(path, "r", encoding=enc) as fh:
                return fh.read()
        except UnicodeDecodeError:
            continue
        except OSError:
            return ""
    return ""


def _rel(p):
    return os.path.relpath(p, PROJECT_ROOT).replace(os.sep, "/")


def _loc(text):
    if not text:
        return 0
    return len(text.splitlines())


def path_to_module(rel):
    """core/engines/mysql.py -> core.engines.mysql；core/__init__.py -> core"""
    m = rel[:-3] if rel.endswith(".py") else rel
    parts = m.split("/")
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def iter_files(root, exts):
    """遍历项目内的目标文件（相对路径）。"""
    for base in root:
        abs_base = os.path.join(PROJECT_ROOT, base)
        if os.path.isfile(abs_base):
            if any(abs_base.endswith(e) for e in exts):
                yield base
            continue
        if not os.path.isdir(abs_base):
            continue
        for dirpath, dirnames, filenames in os.walk(abs_base):
            dirnames[:] = [d for d in dirnames
                           if d not in EXCLUDE_DIRS and not d.endswith(".egg-info")]
            for fn in filenames:
                if any(fn.endswith(e) for e in exts):
                    yield _rel(os.path.join(dirpath, fn))


class PyModule(object):
    """单个 Python 模块的 AST 分析结果。"""

    def __init__(self, rel):
        self.rel = rel
        self.module = path_to_module(rel)
        self.is_pkg = rel.endswith("__init__.py")
        self.loc = 0
        self.doc = ""
        self.imports_pkg = defaultdict(int)          # 内部模块 -> 引用次数
        self.imports_toplevel = set()                # 顶层 import 的内部模块
        self.imports_ext = defaultdict(int)          # 外部模块 -> 出现次数
        self.lazy_imports = set()                    # 仅在函数内 import 的内部模块
        self.symbols = []                            # 顶层 + 类方法（扁平化）
        self.toplevel_names = set()
        self.routes = []                             # (rule_full, methods, handler, lineno)
        self.renders = []                            # (template, lineno) render_template 实参
        self.blueprints = {}                         # var -> url_prefix（实读自 Blueprint(url_prefix=..)）
        self.blueprint_vars = set()                  # 本文件里被 @xx.route 用过的蓝图变量名
        self.sql_read = defaultdict(int)
        self.sql_write = defaultdict(int)
        self.attr_roots = set()                      # 调用链根名，二遍解析
        self.alias_map = {}                          # 别名 -> 目标字符串
        self.parse_error = ""
        self.thread_usage = 0                        # threading.Thread 使用次数


def _first_doc_line(node):
    d = ast.get_docstring(node) or ""
    d = " ".join(d.split())
    return d[:160]


def _signature(node):
    args = node.args
    names = [a.arg for a in args.args]
    if args.vararg:
        names.append("*" + args.vararg.arg)
    if args.kwonlyargs:
        names += [a.arg for a in args.kwonlyargs]
    if args.kwarg:
        names.append("**" + args.kwarg.arg)
    return "(" + ", ".join(names) + ")"


def analyze_python(rel, all_modules, all_packages):
    """解析一个 Python 文件，返回 PyModule。"""
    mod = PyModule(rel)
    text = _read_text(os.path.join(PROJECT_ROOT, rel))
    mod.loc = _loc(text)
    try:
        tree = ast.parse(text, filename=rel)
    except SyntaxError as e:
        mod.parse_error = "SyntaxError: %s (line %s)" % (e.msg, e.lineno)
        return mod

    mod.doc = _first_doc_line(tree)
    pkg_parts = mod.module.split(".") if not mod.is_pkg else mod.module.split(".")
    if not mod.is_pkg:
        pkg_parts = pkg_parts[:-1]

    def _resolve_from(level, name):
        """把 from . x import y 解析为绝对模块名。"""
        if level:
            n = len(pkg_parts) - (level - 1)
            base = pkg_parts[:n] if n > 0 else []
        else:
            base = []
        target = ".".join(base + ([name] if name else []))
        return target

    # ---- import 语句：同时记录【顶层导入】与【延迟导入】----
    # 区分二者很重要：只有顶层 import 构成的环才会在解释器 import 期炸，
    # 含延迟 import 边的环属于"已知并被刻意打破"的环，不该紧急处理。
    top_imports = set()

    def _visit_imports(nodes):
        for node in nodes:
            if isinstance(node, ast.Import):
                for al in node.names:
                    tgt = al.name
                    mod.alias_map[al.asname or tgt.split(".")[0]] = tgt
                    if tgt in all_modules or tgt in all_packages or \
                            tgt.split(".")[0] in all_packages:
                        mod.imports_pkg[tgt] += 1
                    else:
                        mod.imports_ext[tgt.split(".")[0]] += 1
                        mod.attr_roots.add(al.asname or tgt.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                if node.module is None and not node.level:
                    continue
                base_mod = _resolve_from(node.level, node.module)
                for al in node.names:
                    cand = (base_mod + "." + al.name) if base_mod else al.name
                    if cand in all_modules:
                        # `from . import tasks` / `from core.sched import X`：目标是子模块
                        tgt = cand
                    elif base_mod in all_modules or base_mod in all_packages:
                        # 目标是某个模块里的属性（函数/类/常量）
                        tgt = base_mod
                    else:
                        tgt = cand
                    mod.alias_map[al.asname or al.name] = tgt
                    if tgt in all_modules or tgt in all_packages:
                        mod.imports_pkg[tgt] += 1
                    else:
                        root = base_mod.split(".")[0] if base_mod else (node.module or "")
                        if root:
                            mod.imports_ext[root] += 1
                            mod.attr_roots.add(al.asname or al.name)

    _visit_imports(tree.body)
    top_imports = set(mod.imports_pkg.keys())
    _visit_imports(ast.walk(tree))
    mod.imports_toplevel = top_imports
    mod.lazy_imports = set(mod.imports_pkg.keys()) - top_imports

    # ---- 蓝图变量 -> url_prefix ----
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            f = node.value.func
            fname = getattr(f, "id", None) or getattr(f, "attr", None)
            if fname == "Blueprint":
                prefix = ""
                for kw in node.value.keywords:
                    if kw.arg == "url_prefix":
                        _v = _cv_str(kw.value)
                        if _v is not None:
                            prefix = _v
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        mod.blueprints[t.id] = prefix

    # ---- 顶层符号 + 类方法 ----
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            mod.toplevel_names.add(node.name)
            mod.symbols.append({
                "kind": "func", "name": node.name, "line": node.lineno,
                "sig": _signature(node), "doc": _first_doc_line(node),
                "public": not node.name.startswith("_"),
                "decorators": [_expr_name(d) for d in node.decorator_list],
            })
        elif isinstance(node, ast.ClassDef):
            mod.toplevel_names.add(node.name)
            bases = [_expr_name(b) for b in node.bases]
            mod.symbols.append({
                "kind": "class", "name": node.name, "line": node.lineno,
                "sig": "(%s)" % ", ".join(b for b in bases if b),
                "doc": _first_doc_line(node), "public": True,
                "decorators": [_expr_name(d) for d in node.decorator_list],
            })
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    mod.symbols.append({
                        "kind": "method", "name": "%s.%s" % (node.name, sub.name),
                        "line": sub.lineno, "sig": _signature(sub),
                        "doc": _first_doc_line(sub),
                        "public": not sub.name.startswith("_"),
                        "decorators": [_expr_name(d) for d in sub.decorator_list],
                    })
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id.isupper():
                    mod.symbols.append({
                        "kind": "const", "name": t.id, "line": node.lineno,
                        "sig": "", "doc": "", "public": True, "decorators": [],
                    })

    # ---- 路由 ----
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            rule = None
            methods = None
            bp_var = None
            target = dec
            if isinstance(dec, ast.Call):
                target = dec.func
                _r0 = _cv_str(dec.args[0]) if dec.args else None
                if _r0 is not None:
                    rule = _r0
                for kw in dec.keywords:
                    if kw.arg == "methods" and isinstance(kw.value, (ast.List, ast.Tuple)):
                        methods = [str(_cv(e)) for e in kw.value.elts
                                   if _cv(e) is not _MISSING]
            if isinstance(target, ast.Attribute) and target.attr == "route":
                rule = rule or (isinstance(dec, ast.Attribute) and None)
                if hasattr(target.value, "id"):
                    bp_var = target.value.id
                methods = methods or ["GET"]
                if rule:
                    if bp_var:
                        mod.blueprint_vars.add(bp_var)
                    # 前缀必须是本文件里实读到的 Blueprint(url_prefix=..)，
                    # 读不到就标记为"未知"，绝不用 "/xxx_bp" 之类的猜测值拼出看似正确的 URL。
                    if bp_var is None or bp_var == "app":
                        prefix, known = "", True
                    elif bp_var in mod.blueprints:
                        prefix, known = mod.blueprints[bp_var], True
                    else:
                        prefix, known = "", False
                    mod.routes.append({
                        "rule": prefix + rule, "rule_raw": rule,
                        "methods": methods,
                        "handler": node.name, "line": node.lineno,
                        "bp": bp_var or "app", "prefix_known": known,
                    })

    # ---- render_template 实参（页面路由归属用，必须带行号才能精确定位）----
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "render_template":
            if node.args:
                v = _cv_str(node.args[0])
                if v:
                    mod.renders.append((v, node.lineno))

    # ---- SQL 表名（读/写）----
    sql_pat_read = re.compile(
        r"\b(?:from|join)\s+([a-z_][a-z0-9_]*)", re.I)
    sql_pat_write = re.compile(
        r"\b(?:insert\s+into|delete\s+from|create\s+table\s+if\s+not\s+exists|"
        r"create\s+table|alter\s+table|drop\s+table)\s+([a-z_][a-z0-9_]*)", re.I)
    # UPDATE 必须紧跟 SET，否则会把每一句 "update xxx" 正文都当成表名
    sql_pat_update = re.compile(
        r"\bupdate\s+([a-z_][a-z0-9_]*)\s+set\b", re.I)
    for node in ast.walk(tree):
        s = _cv_str(node) if isinstance(node, ast.expr) else None
        if s is not None:
            if len(s) > 4000:
                continue
            low = s.lower()
            if "select" in low or " from " in low or "insert" in low or "update " in low:
                for t in sql_pat_read.findall(s):
                    if t.lower() not in ("dual", "values"):
                        mod.sql_read[t.lower()] += 1
                for t in sql_pat_write.findall(s) + sql_pat_update.findall(s):
                    mod.sql_write[t.lower()] += 1

    # ---- 调用根名 & 线程 ----
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Attribute):
                root = f
                while isinstance(root, ast.Attribute):
                    root = root.value
                if isinstance(root, ast.Name):
                    mod.attr_roots.add(root.id)
            elif isinstance(f, ast.Name):
                mod.attr_roots.add(f.id)
            fname = getattr(f, "id", None) or getattr(f, "attr", None)
            if fname in ("Thread", "Timer"):
                mod.thread_usage += 1
    return mod


def _expr_name(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return "%s.%s" % (_expr_name(node.value), node.attr)
    if isinstance(node, ast.Call):
        return _expr_name(node.func)
    v = _cv_str(node)
    if v is not None:
        return v
    return ""


# ---------------------------------------------------------------------------
# 前端：模板 / JS
# ---------------------------------------------------------------------------

_RE_API = re.compile(r"""['"`](/api/[A-Za-z0-9_\-/{}$.]+)['"`]""")
# 形如：url_for('static', filename='js/app.js') 或 "/static/js/app.js?v=20260916"
# 注意要容忍版本号 query string，否则会漏掉 base.html 里的所有资源引用。
_RE_STATIC = re.compile(
    r"""(?:url_for\(\s*['"]static['"]\s*,\s*filename\s*=\s*['"]([^'"]+)['"]"""
    r"""|['"]/?(static/[A-Za-z0-9_\-/.]+)(?:[?#][^'"\s<>]+)?['"])""")
# 目标数据库侧的系统表/视图前缀（不是平台元库表，不算孤儿）
_REMOTE_OBJ_PREFIX = (
    "pg_", "information_schema", "mysql", "performance_schema", "sqlite_",
    "syscat", "sys", "v$", "dba_", "all_", "user_", "cdb_", "gv$", "master.",
)

_QUOTE = r"""['"]"""            # 单/双引号字符类
_NQUOTE = r"""[^'"]"""         # 非引号字符类
_RE_EXTENDS = re.compile(r"\{%\s*extends\s*" + _QUOTE + r"(" + _NQUOTE + r"+)" + _QUOTE)
_RE_RENDER = re.compile(r"render_template" + r"\(\s*" + _QUOTE + r"(" + _NQUOTE + r"+)" + _QUOTE)


def analyze_template(rel, text):
    return {
        "rel": rel,
        "loc": _loc(text),
        "extends": _RE_EXTENDS.findall(text),
        "assets": sorted({a for t in _RE_STATIC.findall(text) for a in t if a}),
        "apis": sorted(set(_RE_API.findall(text))),
    }


def analyze_js(rel, text):
    return {
        "rel": rel,
        "loc": _loc(text),
        "apis": sorted(set(_RE_API.findall(text))),
    }


# ============================================================================
# 四、图构建
# ============================================================================

def assign_layer(rel, mod_name):
    best = ("L6 其他/未归类", 0)
    for name, prefixes in LAYER_RULES:
        for p in prefixes:
            if rel == p or rel.startswith(p):
                if len(p) > best[1]:
                    best = (name, len(p))
    return best[0]


class CodeGraph(object):
    def __init__(self):
        self.py = {}
        self.templates = {}
        self.js = {}
        self.imported_by = defaultdict(set)
        self.call_edges = defaultdict(set)   # (src_mod, dst_mod) 由 attr 根名解析
        self.errors = []

    def _propagate_blueprints(self):
        """把 Blueprint(url_prefix=..) 的值跨文件补到使用它的路由上。

        只有在**全项目范围内该蓝图变量名的定义唯一**时才采用（这不是猜测：
        唯一定义 == 确定性来源）。同一变量名被定义成两个不同前缀时，宁可标记
        为未知，也不能随便挑一个。
        """
        owner = defaultdict(list)            # var -> [(定义所在文件, prefix)]
        for rel, m in self.py.items():
            for var, prefix in m.blueprints.items():
                owner[var].append((rel, prefix))
        for rel, m in self.py.items():
            for rt in m.routes:
                var = rt["bp"]
                if rt.get("prefix_known") or var in ("app", ""):
                    continue
                cands = owner.get(var, [])
                if len(cands) == 1 and cands[0][0] != rel:
                    rt["rule"] = cands[0][1] + rt.get("rule_raw", rt["rule"])
                    rt["prefix_known"] = True
                    rt["prefix_from"] = cands[0][0]
                elif len(cands) > 1 and len({c[1] for c in cands}) > 1:
                    rt["prefix_known"] = False
                    rt["prefix_conflict"] = sorted(c[0] for c in cands)
                    _issue("ERROR", "蓝图前缀歧义",
                           "变量 `%s` 在多个文件被定义成不同的 url_prefix（%s），"
                           "无法确定 `%s:%d` 这条路由的真实 URL"
                           % (var, ", ".join(sorted(c[0] for c in cands)),
                              rel, rt["line"]))

    # ---- pass 1: 发现模块全集 ----
    def discover(self, roots):
        py_files = []
        for rel in iter_files(roots, (".py",)):
            if rel.endswith(".pyc"):
                continue
            py_files.append(rel)
        for rel in ROOT_PY_FILES:
            if os.path.isfile(os.path.join(PROJECT_ROOT, rel)) and rel not in py_files:
                py_files.append(rel)
        self._modules = set()
        self._packages = set()
        for rel in py_files:
            m = path_to_module(rel)
            self._modules.add(m)
            if rel.endswith("__init__.py"):
                self._packages.add(m)
        # 目录型包（无 __init__.py 的命名空间包）
        for rel in py_files:
            parts = rel.split("/")[:-1]
            for i in range(1, len(parts) + 1):
                self._packages.add(".".join(parts[:i]))
        return py_files

    def build(self, roots, include_optional=False):
        if include_optional:
            roots = list(roots) + OPTIONAL_ROOTS

        py_files = self.discover(roots)
        # ---- pass 2: 解析 ----
        for rel in py_files:
            try:
                self.py[rel] = analyze_python(rel, self._modules, self._packages)
            except Exception as e:  # 单个文件解析失败不能拖垮全局
                self.errors.append("%s: %s" % (rel, e))
                m = PyModule(rel)
                m.parse_error = str(e)
                self.py[rel] = m
                # 解析失败 = 这个模块在图谱里没有可信数据，必须显式报出来而不是静默略过
                _issue("ERROR", "模块解析失败",
                       "`%s` 无法解析（%s: %s）——该模块在图谱中的所有统计均不可信"
                       % (rel, type(e).__name__, e))

        # 蓝图变量常定义在与 `@bp.route` 不同的文件里（如 api/__init__.py），
        # 单独解析一个文件拿不到 url_prefix，必须跨文件传播一次。
        self._propagate_blueprints()

        # ---- 前端 ----
        for rel in iter_files([r for r in roots if r in ("templates", "static/js")],
                              (".html", ".js")):
            text = _read_text(os.path.join(PROJECT_ROOT, rel))
            if rel.endswith(".html"):
                self.templates[rel] = analyze_template(rel, text)
            else:
                self.js[rel] = analyze_js(rel, text)

        # ---- 反向索引 / 调用边 ----
        mod_by_name = {}
        for rel, m in self.py.items():
            mod_by_name.setdefault(m.module, rel)
        for rel, m in self.py.items():
            base_layer = assign_layer(rel, m.module)
            for tgt in m.imports_pkg:
                # 归一化到包/模块所在文件
                trel = mod_by_name.get(tgt)
                if trel is None:
                    # 可能是包：映射到其 __init__.py
                    cand = tgt.replace(".", "/") + "/__init__.py"
                    trel = cand if cand in self.py else None
                if trel and trel != rel:
                    self.imported_by[trel].add(rel)
            # 调用边：xxx.yyy() 的根名 -> 模块
            for root in m.attr_roots:
                tgt = m.alias_map.get(root)
                if tgt and (tgt in self._modules or tgt in self._packages):
                    trel = mod_by_name.get(tgt) or (
                        tgt.replace(".", "/") + "/__init__.py")
                    if trel in self.py and trel != rel:
                        self.call_edges[trel].add(rel)
        return self

    # ---- 查询辅助 ----
    def layer_of(self, rel):
        return assign_layer(rel, self.py[rel].module if rel in self.py else "")

    def module_summary(self, rel):
        m = self.py[rel]
        return {
            "rel": rel, "module": m.module, "loc": m.loc, "doc": m.doc,
            "layer": self.layer_of(rel),
            "deps": sorted(m.imports_pkg.keys()),
            "deps_ext": sorted(m.imports_ext.keys()),
            "deps_count": len(m.imports_pkg),
            "rdeps_count": len(self.imported_by.get(rel, ())),
            "symbols": len(m.symbols),
            "error": m.parse_error,
        }


# ============================================================================
# 五、分析：循环依赖 / 越层依赖
# ============================================================================

def build_edges(graph):
    """模块级依赖边：{rel: [被依赖的 rel]}，同时给出每条边是否来自延迟 import。"""
    mod_by = {}
    for r, mm in graph.py.items():
        mod_by[mm.module] = r
    edges, lazy_map = {}, {}
    for rel, m in graph.py.items():
        outs = []
        for tgt in m.imports_pkg:
            trel = mod_by.get(tgt) or tgt.replace(".", "/") + "/__init__.py"
            # 注意：`from ..<本包> import X` 会被解析回本包的 __init__（自身），
            # 这不是真自环，必须丢弃。
            if trel in graph.py and trel != rel:
                outs.append(trel)
                lazy_map[(rel, trel)] = (tgt in m.lazy_imports)
        edges[rel] = sorted(set(outs))
    return edges, lazy_map


def find_sccs(edges):
    """Tarjan 强连通分量——**完整**的环信息，不受枚举上限影响。

    返回 [(members:sorted list, internal_edges:int)]，只保留 size>1 的分量
    （size==1 且无自环的不构成环）。
    """
    index = {}
    low = {}
    stack = []
    on_stack = set()
    result = []
    counter = [0]

    def strongconnect(root):
        # 迭代式 Tarjan，避免深递归
        work = [(root, iter(edges.get(root, ())))]
        index[root] = low[root] = counter[0]
        counter[0] += 1
        stack.append(root)
        on_stack.add(root)
        while work:
            node, it = work[-1]
            advanced = False
            for nxt in it:
                if nxt not in index:
                    index[nxt] = low[nxt] = counter[0]
                    counter[0] += 1
                    stack.append(nxt)
                    on_stack.add(nxt)
                    work.append((nxt, iter(edges.get(nxt, ()))))
                    advanced = True
                    break
                elif nxt in on_stack:
                    low[node] = min(low[node], index[nxt])
            if advanced:
                continue
            work.pop()
            if low[node] == index[node]:
                comp = []
                while True:
                    w = stack.pop()
                    on_stack.discard(w)
                    comp.append(w)
                    if w == node:
                        break
                if len(comp) > 1 or node in edges.get(node, ()):
                    result.append(sorted(comp))
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])

    for n in edges:
        if n not in index:
            strongconnect(n)
    out = []
    for comp in result:
        s = set(comp)
        internal = sum(1 for a in comp for b in edges.get(a, ()) if b in s)
        out.append((comp, internal))
    out.sort(key=lambda x: (-len(x[0]), x[0]))
    return out


def find_cycles(graph, max_nodes=5):
    """枚举**简单环**（DFS，长度 ≤ max_nodes）。

    返回 ([(cycle:list, lazy_edges:int)], truncated:int)
    lazy_edges = 环上被「函数内延迟 import」打破的边数。
    注意：简单环枚举有上限会丢数据，完整结论以 find_sccs 的强连通分量为准。
    """
    edges, lazy_map = build_edges(graph)

    cycles = []
    seen = set()
    truncated = [0]

    def dfs(path, start, depth):
        if depth > max_nodes or len(cycles) > 400:
            truncated[0] = 1
            return
        cur = path[-1]
        for nxt in edges.get(cur, []):
            if nxt == start:
                key = frozenset(path)
                if key not in seen:
                    seen.add(key)
                    cycles.append(list(path) + [nxt])
            elif nxt not in path:
                dfs(path + [nxt], start, depth + 1)

    for start in sorted(edges):
        dfs([start], start, 0)

    # 去重（同环不同起点的旋转）：用节点集合 + 长度做键
    uniq = []
    seen2 = set()
    for c in cycles:
        nodes = c[:-1]
        k = (frozenset(nodes), len(nodes))
        if k in seen2:
            continue
        seen2.add(k)
        lazy = sum(1 for i in range(len(c) - 1)
                   if lazy_map.get((c[i], c[i + 1])))
        uniq.append((c, lazy))
    uniq.sort(key=lambda x: (len(x[0]), x[1]))
    return uniq, bool(truncated[0])


def find_layer_violations(graph):
    """越层依赖：依赖了【更高层】的模块（L0 全局基座除外）。"""
    mod_by = {}
    for r, mm in graph.py.items():
        mod_by[mm.module] = r

    viols = []
    for rel, m in graph.py.items():
        src_l = graph.layer_of(rel)
        if src_l == LAYER0:
            continue
        si = LAYER_ORDER.get(src_l, 6)
        for tgt in m.imports_pkg:
            trel = mod_by.get(tgt) or tgt.replace(".", "/") + "/__init__.py"
            if trel not in graph.py or trel == rel:
                continue
            dst_l = graph.layer_of(trel)
            if dst_l == LAYER0:
                continue
            di = LAYER_ORDER.get(dst_l, 6)
            if di < si:  # 依赖了更高层
                viols.append((rel, trel, src_l, dst_l))
    return viols


# ============================================================================
# 六、Markdown 渲染
# ============================================================================

def _tbl(rows, header):
    out = ["| " + " | ".join(header) + " |",
           "|" + "|".join(["---"] * len(header)) + "|"]
    for r in rows:
        out.append("| " + " | ".join(str(c) for c in r) + " |")
    return "\n".join(out)


def _anchor(s):
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")


def _short(rel):
    """core/engines/base.py -> engines/base.py（去掉通用前缀，保留辨识度）。"""
    parts = rel.split("/")
    return "/".join(parts[-2:]) if len(parts) > 2 else rel


def _template_chain(graph, rel, seen=None):
    """沿 {% extends %} 链找出所有父模板。"""
    seen = seen or set()
    out = []
    for p in graph.templates.get(rel, {}).get("extends", []):
        tgt = p if p in graph.templates else ("templates/" + p)
        if tgt in graph.templates and tgt not in seen:
            seen.add(tgt)
            out.append(tgt)
            out += _template_chain(graph, tgt, seen)
    return out


def _inherited_assets(graph, rel):
    """[(父模板, 资源路径)]：页面通过父模板间接引入的静态资源。"""
    out = []
    for parent in _template_chain(graph, rel):
        for a in graph.templates[parent]["assets"]:
            out.append((parent, a))
    return out


def render_markdown(graph, old=None, include_all=False):
    """渲染图谱正文。

    跑两遍：第一遍用于把 ISSUES 账本填满（结果丢弃），第二遍带着**完整账本**
    生成正文，这样放在最前面的 §0.1 才能列出本次全部的可信度问题。
    """
    del ISSUES[:]
    _render_body(graph, None, include_all, None)
    snapshot = sorted(set(ISSUES))
    del ISSUES[:]
    return _render_body(graph, old, include_all, snapshot)


def _render_body(graph, old, include_all, ISSUE_ROWS):
    now = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    py_items = sorted(graph.py.items())
    total_loc = sum(m.loc for _, m in py_items)
    tpl_loc = sum(t["loc"] for t in graph.templates.values())
    js_loc = sum(j["loc"] for j in graph.js.values())

    L = []
    A = L.append
    A("# AIDBM 代码图谱（Code Graph）\n")
    A("> **自动生成，请勿手改**：由 `scripts/gen_code_graph.py` 基于 AST 静态分析生成。  ")
    A("> 生成时间：%s ｜ Python 文件 %d 个 ｜ 后端代码 %d 行 ｜ 模板 %d 个（%d 行） ｜ 前端 JS %d 个（%d 行）  " % (
        now, len(py_items), total_loc, len(graph.templates), tpl_loc,
        len(graph.js), js_loc))
    A("> 机器可读索引：`docs/code_graph.json`。增量变更：`python scripts/gen_code_graph.py --diff`。\n")
    A("---\n")

    # ---------- 0. 速读区 ----------
    A("## 0. 五分钟速读（先读这里）\n")
    A("**技术栈**：Python + Flask（Jinja2 模板 + Bootstrap 5 + 原生 JS，复杂页用 Preact+htm 免构建）"
      " ｜ 元数据库 SQLite ｜ APScheduler 进程内调度 ｜ 远程执行走 SSH/paramiko。\n")
    A("**核心架构约束**（改代码前必须知道）：")
    A("1. 客户端零安装：所有工具/驱动/依赖只装在**平台侧**，远端数据库服务器不装任何 agent。  ")
    A("2. 完全离线自足：禁止联网下载依赖，最终交付为离线包（含 PyInstaller 打包）。  ")
    A("3. 不仿真：备份/恢复必须真实执行，失败就报失败（只剩显式 DEMO 场景允许仿真）。\n")

    A("**最该先认识的 10 个模块**：\n")
    core10 = [
        ("app.py", "Flask 应用装配：注册蓝图、页面路由、全局异常/鉴权、登陆锁定"),
        ("config.py", "全局配置项与默认值（几乎所有环境变量开关在这里）"),
        ("core/db.py", "元数据库：连接、建表、加解密、日志写入"),
        ("core/models.py", "备份任务/记录/恢复记录的唯一数据读写出口"),
        ("core/scheduler.py", "APScheduler 调度：触发备份、并发控制、reload"),
        ("core/engines/base.py", "所有数据库引擎的基类：run_backup/run_restore 统一契约"),
        ("core/engines/__init__.py", "引擎注册表：db_type -> Engine 类的动态分发"),
        ("core/remote_dump.py", "远端/本机命令执行层：SSH、工具路径解析、流式落盘"),
        ("core/storage.py", "备份产物落盘与多后端存储抽象"),
        ("core/rt_backup/db_rt.py", "实时备份（CDC/CDP）任务编排核心"),
    ]
    rows = []
    for rel, why in core10:
        m = graph.py.get(rel)
        if m is None:
            _issue("ERROR", "速读区指向不存在的模块",
                   "`%s` 不在扫描结果里（文件缺失或已被改名）" % rel)
            rows.append(["`%s`" % rel, "❌ 不存在", why])
        else:
            _bad_paths_in(graph, why, "§0 速读区")
            rows.append(["`%s`" % rel, m.loc, why])
    A(_tbl(rows, ["模块", "行数", "为什么重要"]))
    A("")

    # ---------- 0.1 数据可信度 ----------
    rows = ISSUE_ROWS
    A("## 0.1 数据可信度（先看完这一段再信上面的表）\n")
    A("图谱里的条目来自三类来源，**可信度不同**：\n")
    A(_tbl([
        ["① AST 确证（可信事实）",
         "模块/符号/行号、import 依赖、@route 装饰器的 URL 与 handler、"
         "class/def 签名、docstring、CTE 之外的 SQL 表名",
         "直接读 `ast.parse` 结果，与源码严格一致"],
        ["② 行号/正则估计（可能偏）",
         "§8 的读写次数（字符串计数，同一模板多处复用会重复计数）、"
         "§7 模板引用的静态资源、§13 第三方包",
         "正则匹配，**可能重复或漏**，仅用于判断热度/范围，不是精确值"],
        ["③ 人工维护的领域注解（会过期）",
         "§0 核心模块、§5 运行时拓扑、§9 业务链路、§10 改动指引、§11 扩展点",
         "写在脚本常量里，**每次生成都会校验**，失效即标记 ⚠/❌"],
    ], ["来源", "涵盖内容", "说明"]))
    A("")
    if rows is None:
        pass                       # 第一遍预热，账本还没齐
    elif rows:
        n_err = sum(1 for r in rows if r[0] == "ERROR")
        A("### 本次生成发现 %d 个可信度问题（ERROR %d / WARN %d）\n"
          % (len(rows), n_err, len(rows) - n_err))
        A("> 出现 ERROR 意味着图谱里有**不可信或缺失**的数据，"
          "`--strict` 模式下脚本会以非 0 退出（可用于 CI/提交钩子）。\n")
        A(_tbl([[r[0], r[1], r[2]] for r in rows], ["级别", "类型", "说明"]))
        A("")
    else:
        A("### 本次生成：0 个可信度问题 ✅\n")
        A("全部条目均由 AST 确证，无推断得到的假数据。\n")

    # ---------- 1. 分层 ----------
    A("## 1. 分层架构\n")
    A("```mermaid")
    A("flowchart TB")
    layer_nodes = {}
    for name in sorted(LAYER_ORDER, key=lambda n: LAYER_ORDER[n]):
        layer_nodes[name] = "L" + _anchor(name)
        A('  %s["%s"]' % (layer_nodes[name], name))
    # 层之间的实际依赖聚合（过滤掉对 L0 全局基座的引用，否则天下大同、图无信息量）
    mod_by = {mm.module: r for r, mm in graph.py.items()}
    agg = set()
    for rel, m in graph.py.items():
        src = graph.layer_of(rel)
        if src == LAYER0:
            continue
        for tgt in m.imports_pkg:
            trel = mod_by.get(tgt) or tgt.replace(".", "/") + "/__init__.py"
            if trel not in graph.py:
                continue
            dst = graph.layer_of(trel)
            if dst == LAYER0 or LAYER_ORDER.get(src, 6) == LAYER_ORDER.get(dst, 6):
                continue
            agg.add((layer_nodes.get(src, "Lx"), layer_nodes.get(dst, "Lx")))
    for a, b in sorted(agg):
        A("  %s --> %s" % (a, b))
    A("```\n")
    A("层职责速查：\n")
    A(_tbl([
        ["L0 全局基座", "谁都能依赖的配置/元库/模型/鉴权", "config.py、auth.py、core/db.py、core/models.py"],
        ["L1 入口装配", "Flask 应用装配与启动", "app.py、init_db.py"],
        ["L2 接口层", "REST 路由、参数校验、JSON 出入参", "api/*.py"],
        ["L3 业务编排层", "备份/迁移/克隆/联动等跨模块流程", "core/ 下其余模块"],
        ["L4 能力执行层", "数据库引擎、CDC、实时、同步、虚拟机、存储后端", "core/engines、core/cdc、core/rt_backup、core/sync、core/vm"],
        ["L5 基础设施层", "远端执行、RBAC、连接驱动、通知、插件包", "core/remote_dump.py、core/rbac.py、core/jdbc.py …"]],
        ["层", "职责", "典型位置"]))
    A("")
    A("**依赖方向规矩**：`L1 → L2 → L3 → L4 → L5`（数字越大越底层；允许跨层向下，"
      "禁止反向依赖更高层；`L0` 全局基座任何层都可以直接用）。"
      "实际违规见 §4.2。\n")

    # ---------- 2. 模块清单 ----------
    A("## 2. 模块清单与依赖（按层）\n")
    A("列含义：**依赖↑** = 本文件 import 的内部模块数（出度）；**被依赖↓** = 有多少内部文件 import 了它（入度，越高越动不得）。\n")
    by_layer = defaultdict(list)
    for rel, m in py_items:
        by_layer[graph.layer_of(rel)].append((rel, m))
    others = sorted(by_layer.get(LAYER_OTHER, []), key=lambda x: -x[1].loc)
    if others:
        _issue("WARN", "模块未归类",
               "%d 个模块没被 `LAYER_RULES` 覆盖（%s）——层归类只作用在 §2/§4 的统计上，"
               "请补规则而不是无视"
               % (len(others), ", ".join("`%s`" % r for r, _ in others[:6])))
    for layer_name in sorted(by_layer, key=lambda n: LAYER_ORDER.get(n, 9)):
        items = sorted(by_layer[layer_name], key=lambda x: -x[1].loc)
        if not items:
            continue
        A("### %s（%d 个模块，%d 行）\n" % (
            layer_name, len(items), sum(x[1].loc for x in items)))
        rows = []
        for rel, m in items:
            summary = m.doc.replace("|", "/") if m.doc else "（无模块 docstring）"
            rows.append([
                "`%s`" % rel, m.loc,
                len(m.imports_pkg), len(graph.imported_by.get(rel, ())),
                summary[:110] if summary else "—",
            ])
        A(_tbl(rows, ["模块", "行数", "依赖↑", "被依赖↓", "职责（模块 docstring）"]))
        A("")

    # ---------- 3. 枢纽模块 ----------
    A("## 3. 枢纽模块（改之前先看这里）\n")
    A("入度最高的 20 个模块——它们被大量模块引用，**修改签名/返回值的影响面最大**。\n")
    hubs = sorted(py_items, key=lambda x: -len(graph.imported_by.get(x[0], ())))[:20]
    rows = []
    for rel, m in hubs:
        rdeps = sorted(graph.imported_by.get(rel, ()))
        rdeps_show = ", ".join("`%s`" % r.split("/")[-1] for r in rdeps[:8])
        if len(rdeps) > 8:
            rdeps_show += " … +%d" % (len(rdeps) - 8)
        rows.append(["`%s`" % rel, graph.layer_of(rel), len(rdeps), rdeps_show])
    A(_tbl(rows, ["模块", "层", "被依赖数", "主要依赖方"]))
    A("")

    # 出度最高
    A("**扇出最高（最像上帝对象的模块）**：\n")
    fans = sorted(py_items, key=lambda x: -len(x[1].imports_pkg))[:10]
    A(_tbl([["`%s`" % r, len(m.imports_pkg),
             ", ".join(sorted(m.imports_pkg)[:6]) + (" …" if len(m.imports_pkg) > 6 else "")]
            for r, m in fans], ["模块", "依赖数", "依赖的内部模块"]))
    A("")

    # ---------- 4. 结构健康度 ----------
    A("## 4. 结构健康度（环 / 越层 / 巨石 / 孤儿）\n")
    edges_all, _lazy_all = build_edges(graph)
    sccs = find_sccs(edges_all)
    cycles, cycles_truncated = find_cycles(graph)
    cycle_nodes = defaultdict(int)
    for c, _ in cycles:
        for n in set(c[:-1]):
            cycle_nodes[n] += 1
    hard = [x for x in cycles if x[1] == 0]
    soft = [x for x in cycles if x[1] > 0]

    # 环的【完整】结论来自强连通分量：SCC 不受任何枚举上限影响
    A("### 4.1 模块级循环依赖\n")
    A("**完整结论（强连通分量 SCC，Tarjan 算法，无采样、无截断）**："
      "共 **%d 个**互相纠缠的模块团。  " % len(sccs))
    A("一个 SCC = 团内任意两个模块都能通过依赖互相到达，也就是**团内任何一对模块之间都存在环**。\n")
    scc_rows = []
    real_cycles = []
    for members, internal in sccs:
        if len(members) <= 12:
            detail = ", ".join("`%s`" % _short(x) for x in sorted(members))
        else:
            detail = ", ".join("`%s`" % _short(x) for x in sorted(members)[:12]) \
                     + " … +%d" % (len(members) - 12)
        # 成因判定：团内若存在一个被绝大多数成员反向引用的 `__init__.py`，
        # 这个团大概率只是「包 __init__ 聚合子模块」的 Python 惯例，不是真耦合。
        ms = set(members)
        in_deg = defaultdict(int)
        for a in members:
            for b in edges_all.get(a, ()):
                if b in ms:
                    in_deg[b] += 1
        hub, cnt = (None, 0)
        for b, c in in_deg.items():
            if c > cnt or (c == cnt and hub and str(b) < str(hub)):
                hub, cnt = b, c
        if hub and hub.endswith("__init__.py") and cnt >= max(3, len(members) * 0.5):
            cause = ("包聚合：团内 %d/%d 个模块都反向引用 `%s` "
                     "（__init__ 导入子模块、子模块再 from . import xxx，Python 包惯例）"
                     % (cnt, len(members), hub))
        else:
            cause = "**真实互相依赖**（非 __init__ 星型聚合）"
            real_cycles.append((members, internal))
        scc_rows.append([len(members), internal, cause, detail])
    A(_tbl(scc_rows, ["团大小", "团内边数", "成因判定", "成员模块"]))
    A("")
    A("> **怎么读这张表**：标了「包聚合」的团，环来自 Python 包的 `__init__` 星型结构，"
      "拆它等于重构包组织方式，收益要权衡；  ")
    A("> 标了「**真实互相依赖**」的团才是职责边界真的没划清，优先处理。\n")
    if real_cycles:
        biggest = real_cycles[0]
        A("> 需要优先拆的是 **%d 个模块**的真实互相依赖团：%s。  "
          % (len(biggest[0]),
             ", ".join("`%s`" % _short(x) for x in sorted(biggest[0])[:6])
             + (" … +%d" % (len(biggest[0]) - 6) if len(biggest[0]) > 6 else "")))
    else:
        A("> 当前没有「真实互相依赖」的团，剩余环全部来自包 `__init__` 的星型聚合。  ")
    A("> 下面再列出具体走了哪几条边（**简单环**，只枚举长度 ≤5 的，可能不完整）。\n")

    A("#### 4.1.1 简单环明细（长度 ≤5）\n")
    if cycles_truncated:
        _issue("WARN", "环枚举不完整",
               "简单环枚举触达上限（只保留了长度 ≤5 的环），"
               "**完整结论请看上面的强连通分量**，不要把这里的数量当成全部")
        A("> ⚠ 简单环枚举已触达上限，下面不是全部环；完整结论以上方 SCC 为准。\n")
    A("> 「**导入期硬环**」= 环上所有边都是顶层 import，挪动/重构时会真的炸；  ")
    A("> 「**已被延迟 import 打破**」= 至少一条边写在函数体内，是项目刻意用来破环的手段。\n")
    A(_tbl([["导入期硬环", len(hard), "优先级高：拆分才能解"],
            ["已被延迟 import 打破", len(soft), "可接受：现状不会炸，但职责边界仍模糊"],
            ["合计", len(cycles), ""]],
           ["类别", "环数", "说明"]))
    A("")
    if hard:
        A("**导入期硬环明细**（按环长升序）：\n")
        rows = []
        for c, lz in hard[:30]:
            rows.append([" → ".join("`%s`" % _short(x) for x in c)])
        A(_tbl(rows, ["环"]))
        if len(hard) > 30:
            A("\n> 还有 %d 组未展示（见 `code_graph.json` 或放宽本脚本阈值）。\n" % (len(hard) - 30))
    else:
        A("无导入期硬环 ✅\n")
    A("")
    if soft:
        A("<details><summary><b>已被延迟 import 打破的环（%d 组）</b></summary>\n" % len(soft))
        rows = []
        for c, lz in soft[:40]:
            rows.append([len(c) - 1, lz, " → ".join("`%s`" % _short(x) for x in c)])
        A(_tbl(rows, ["环长", "延迟边数", "环"]))
        A("\n</details>\n")
    if cycle_nodes:
        A("**环的枢纽（出现在最多环中的模块——解环从这里下手）**：\n")
        top = sorted(cycle_nodes.items(), key=lambda x: -x[1])[:10]
        A(_tbl([["`%s`" % r, n] for r, n in top], ["模块", "出现在 N 个环里"]))
        A("")

    viols = find_layer_violations(graph)
    A("### 4.2 越层依赖（低层反向依赖更高层）\n")
    if viols:
        rows = []
        seenx = set()
        for s, d, sl, dl in viols:
            k = (s, d)
            if k in seenx:
                continue
            seenx.add(k)
            rows.append(["`%s`" % s, sl, "`%s`" % d, dl])
        A(_tbl(rows[:60], ["发起方", "所在层", "被依赖方", "所在层"]))
        A("\n> 处理方式：优先把共用逻辑下沉到更底层，或在函数内延迟 import 并加注释说明。")
    else:
        A("未发现越层依赖 ✅")
    A("")

    monoliths = sorted(py_items, key=lambda x: -x[1].loc)[:12]
    A("### 4.3 巨石文件（>1000 行，优先拆分）\n")
    big = [(r, m.loc) for r, m in monoliths if m.loc > 1000]
    if big:
        A(_tbl([["`%s`" % r, loc, "%.1f%%" % (100.0 * loc / max(total_loc, 1))]
                for r, loc in big], ["文件", "行数", "占后端代码"]))
    else:
        A("无超过 1000 行的 Python 文件 ✅")
    A("")

    orphans = [r for r, m in py_items
               if not graph.imported_by.get(r) and not r.endswith("__init__.py")
               and r not in ROOT_PY_FILES]
    A("### 4.4 孤儿模块（无人引用）\n")
    if orphans:
        A(_tbl([["`%s`" % r, graph.py[r].loc] for r in orphans[:30]],
               ["文件", "行数"]))
        A("\n> 可能是死代码，也可能是静态分析盲区——常见的无静态引用但仍然活着的情况："
          "① 通过注册表动态 import（如 `core/vm/providers/*` 的 provider 发现）；"
          "② 独立启动的脚本/守护进程入口；③ 从模板或配置里传名字再反射加载。"
          "**删除前务必全文 grep 确认**。")
    else:
        A("无孤儿模块 ✅")
    A("")

    if graph.errors:
        A("### 4.5 解析失败文件\n")
        A(_tbl([["`%s`" % e.split(":")[0], e.split(":", 1)[-1][:120]]
                for e in graph.errors], ["文件", "错误"]))
        A("")

    # ---------- 5. 运行时拓扑 ----------
    A("## 5. 运行时拓扑（跑起来是什么样）\n")
    rt_rows = []
    for name, anchor, desc in RUNTIME_TOPOLOGY:
        path, _, func = anchor.partition(":")
        st, detail = _verify_exists(graph, path, func or None)
        if st == "❌":
            _issue("ERROR", "运行时拓扑入口不存在", "「%s」的入口 `%s` 不存在" % (name, path))
        elif st == "⚠":
            _issue("WARN", "运行时拓扑锚点失效", "「%s」的 `%s` 已不在 `%s`" % (name, func, path))
        rt_rows.append(["**%s**" % name, "`%s`" % anchor, "%s %s" % (st, detail), desc])
    A(_tbl(rt_rows, ["角色", "入口", "校验", "说明"]))
    A("")
    th = sorted([(r, m.thread_usage) for r, m in graph.py.items() if m.thread_usage],
                key=lambda x: -x[1])
    if th:
        A("**显式起线程的位置**（并发/故障排查线索）：\n")
        A(_tbl([["`%s`" % r, c] for r, c in th[:15]], ["文件", "Thread/Timer 次数"]))
        A("")

    # ---------- 6. REST API ----------
    A("## 6. REST API 索引\n")
    A("按蓝图分组：METHOD + 完整 URL → handler（文件:行号）→ 该接口所在文件的能力依赖。\n")
    api_rows = defaultdict(list)
    total_routes = 0
    unknown_prefix = {}          # 蓝图变量名 -> 受影响的路由数
    for rel, m in sorted(graph.py.items()):
        for rt in m.routes:
            total_routes += 1
            key = os.path.basename(rel)
            api_rows[key].append((rt, rel, m))
            if rt.get("prefix_known") is False:
                unknown_prefix.setdefault(rt["bp"], []).append(
                    "`%s:%d` %s" % (rel, rt["line"], rt["rule"]))
    A("共有 **%d** 个注册路由（全部来自 `@<bp>.route(...)` 装饰器的 AST 解析，无推断、无补写）。\n"
      % total_routes)
    if unknown_prefix:
        _issue("ERROR", "路由 URL 前缀未知",
               "%d 个蓝图的 `url_prefix` 没能在同文件里实读到，"
               "对应路由的 URL **不完整**（缺前缀），不能拿去直接调用：%s"
               % (len(unknown_prefix),
                  ", ".join("%s(%d 条)" % (k, len(v)) for k, v in sorted(unknown_prefix.items()))))
        A("> ❌ **URL 前缀未知**：%d 个蓝图的 `url_prefix` 未在同文件里解析到，"
          "下表中这类路由的 URL **缺少前缀**，不能用它直接调接口。  \n" % len(unknown_prefix))
        for bp, items in sorted(unknown_prefix.items()):
            A("> - `%s`：%s\n" % (bp, ", ".join(items[:6]) +
                                  (" … +%d" % (len(items) - 6) if len(items) > 6 else "")))
        A("")
    for grp in sorted(api_rows):
        A("<details><summary><b>%s</b>（%d 个接口）</summary>\n" % (grp, len(api_rows[grp])))
        rows = []
        for rt, rel, m in api_rows[grp]:
            rows.append([
                " ".join(rt["methods"]), "`%s`" % rt["rule"],
                "`%s:%d`" % (rel, rt["line"]), rt["handler"],
            ])
        A(_tbl(rows, ["METHOD", "URL", "handler 位置", "函数名"]))
        A("\n</details>\n")

    # ---------- 7. 前端映射 ----------
    A("## 7. 前端：页面 → 资源 → 接口\n")
    # 统计「几乎所有页面都继承到的通用资源」，避免每行重复同一串东西
    inherit_counter = defaultdict(list)
    for rel in graph.templates:
        for parent, asset in _inherited_assets(graph, rel):
            inherit_counter[asset].append(parent)
    n_tpl = max(len(graph.templates), 1)
    common = sorted(a for a, ps in inherit_counter.items()
                    if len(ps) >= max(2, int(n_tpl * 0.5)))
    common_txt = ", ".join("`%s`" % a.split("/")[-1] for a in common) or "（无）"
    A("**通用继承**：几乎所有页面都 `extends base.html`，因此默认带上 %s，"
      "页面的 JS 逻辑主要写在 `static/js/app.js`——「内联接口数」为 0 不代表没有后端交互。\n"
      % common_txt)
    page_rows = []
    for rel in sorted(graph.templates):
        t = graph.templates[rel]
        page_route = _find_page_route(graph, rel)
        own = ["`%s`" % a.split("/")[-1] for a in t["assets"]]
        extra = ["`%s`←%s" % (a.split("/")[-1], p.split("/")[-1])
                 for p, a in _inherited_assets(graph, rel) if a not in common]
        page_rows.append([
            "`%s`" % rel.split("/")[-1].replace(".html", ""),
            "`%s`" % page_route if page_route else "—（宏/组件/邮件模板）",
            ", ".join(own) or "—",
            ", ".join(sorted(set(extra))) or "—",
            len(t["apis"]),
        ])
    A(_tbl(page_rows, ["模板", "页面路由", "专属资源", "额外继承资源", "内联接口数"]))
    A("")
    js_api_rows = []
    for rel in sorted(graph.js):
        j = graph.js[rel]
        if rel.startswith("static/vendor") or rel.endswith(".min.js"):
            continue
        js_api_rows.append(["`%s`" % rel, j["loc"], len(j["apis"]),
                            ", ".join("`%s`" % a for a in j["apis"][:6]) +
                            (" …+%d" % (len(j["apis"]) - 6) if len(j["apis"]) > 6 else "")])
    if js_api_rows:
        A("**JS 文件 → 调用的后端接口**（静态字符串匹配，动态拼接的接口不在内）：\n")
        A(_tbl(js_api_rows, ["JS 文件", "行数", "接口数", "示例接口"]))
        A("")

    # ---------- 8. 数据表 ----------
    A("## 8. 元数据库表与访问热点\n")
    table_defs = {}
    for node_line, name in _scan_table_defs(graph):
        table_defs.setdefault(name, []).append(node_line)
    access = defaultdict(lambda: {"r": 0, "w": 0, "mods": set()})
    for rel, m in graph.py.items():
        for t, c in m.sql_read.items():
            if t in table_defs or c >= 2:
                access[t]["r"] += c
                access[t]["mods"].add(rel)
        for t, c in m.sql_write.items():
            if t in table_defs or c >= 2:
                access[t]["w"] += c
                access[t]["mods"].add(rel)
    rows = []
    for t in sorted(table_defs):
        a = access.get(t, {"r": 0, "w": 0, "mods": set()})
        defined = ", ".join("`%s:%d`" % (r, l) for r, l in table_defs[t][:3])
        top_mods = ", ".join("`%s`" % _short(x) for x in sorted(a["mods"])[:3])
        if len(a["mods"]) > 3:
            top_mods += " …+%d" % (len(a["mods"]) - 3)
        rows.append([t, "平台元库表", defined, a["r"], a["w"], top_mods or "—"])
    # 有访问但未找到定义：区分「远端 DBMS 系统视图」与「疑似 CTE/别名」
    remote_rows, unknown_rows = [], []
    for t in sorted(access):
        if t in table_defs:
            continue
        a = access[t]
        if a["r"] + a["w"] < 3:
            continue
        if len(t) < 3 or t.startswith(_REMOTE_OBJ_PREFIX):
            remote_rows.append([t, "远端/内置对象", "—", a["r"], a["w"], len(a["mods"])])
        else:
            unknown_rows.append([t, "未找到定义 ⚠", "—", a["r"], a["w"], len(a["mods"])])
    rows += remote_rows + unknown_rows
    if rows:
        A(_tbl(rows, ["表名", "归属", "定义位置", "读次数", "写次数", "主要读写模块"]))
        A("\n> 读/写次数是**静态字符串出现次数**（同一段 SQL 模板被多处复用会重复计数），"
          "用途是判断热度与归属，不是运行期 QPS。  ")
        A("> 「远端/内置对象」是被备份/连接的**目标数据库**侧的系统表与视图"
          "（`pg_*`、`information_schema`、`v$*`、`syscat*`…），不属于平台元库；  ")
        A("> 「未找到定义」多为 SQL 里的 CTE/临时别名，需要人工确认。")
        A("")
    else:
        A("未能从静态分析中提取到表定义（表结构可能集中在 `core/db.py` 的字符串里）。\n")

    # ---------- 9. 关键链路 ----------
    A("## 9. 关键业务链路\n")
    A("每条链路按执行顺序给出必经文件，**每一跳都与当前代码做了 AST 比对**："
      "✅ = 文件与锚点符号都存在；🧩 = 占位模板；⚠ = 文件在但锚点符号已不存在；"
      "❌ = 文件本身已不存在。出现 ⚠/❌ 时**以代码为准**，并请同步修正脚本里的 `CHAINS`。\n")
    for name, steps in CHAINS:
        A("### %s\n" % name)
        rows = []
        for label, path, func in steps:
            status, detail = _verify_exists(graph, path, func)
            if status == "❌":
                _issue("ERROR", "链路引用了不存在的文件",
                       "链路「%s」的环节「%s」指向 `%s`，不存在" % (name, label, path))
            elif status == "⚠":
                _issue("WARN", "链路锚点已失效",
                       "链路「%s」的环节「%s」找不到 `%s`（%s）"
                       % (name, label, func, detail))
            rows.append([label, "`%s`" % path, detail, status])
        A(_tbl(rows, ["环节", "文件", "锚点/说明", "校验"]))
        A("")

    # ---------- 10. 改动指引 ----------
    A("## 10. 改动指引：想改 X，先看哪些文件\n")
    A("路径全部做过存在性校验（🧩 = 占位模板，非真实文件）。\n")
    rows = []
    for want, files in CHANGE_GUIDE.items():
        parts, bad = [], []
        for f in files:
            st, detail = _verify_exists(graph, f, None)
            parts.append("`%s`" % f)
            if st == "❌":
                bad.append(f)
                _issue("ERROR", "改动指引引用了不存在的文件",
                       "「%s」指向 `%s`，该文件已不存在" % (want, f))
        flag = ("  ⚠ 失效：%s" % ", ".join("`%s`" % b for b in bad)) if bad else ""
        rows.append([want, " → ".join(parts) + flag])
    A(_tbl(rows, ["改动目标", "建议阅读顺序"]))
    A("")

    # ---------- 11. 扩展点 ----------
    A("## 11. 扩展点 / 插件机制\n")
    ext_rows = [
        ["数据库类型适配", "`core/engines/` + `core/engines/__init__.py` 注册表",
         "追加引擎模块并在注册表登记 db_type"],
        ["可插拔适配器", "`core/db_adapters.py`（启动注册 enabled=1 的项）",
         "无需改主线代码即可扩展库型"],
        ["同步/迁移数据源", "`core/sync/plugins/` + `core/sync/plugins/base.py`",
         "实现读取/写入/类型映射插件"],
        ["CDC 捕获通道", "`core/cdc/__init__.py` 工厂 + `core/cdc/*.py`",
         "按 db_type 注册 make_capture 实现"],
        ["存储后端", "`core/storage_backends/`", "本地/对象存储/磁带等可插拔后端"],
        ["通知渠道", "`core/notifier.py` + `core/webhooks.py`", "邮件/钉钉/企业微信/Webhook"],
        ["ITSM 工单", "`core/itsm.py`", "可插拔工单系统对接"],
        ["自定义脚本任务", "`core/custom_scripts.py`（backup_mode=custom）",
         "任务级脚本经 SFTP 推送执行"],
        ["依赖插件包", "`core/plugin_catalog.py` + `core/plugin_installer.py`",
         "xtrabackup / mariabackup 等二进制包，使用时由平台推送到目标机，客户端免安装"],
    ]
    for row in ext_rows:
        bad = _bad_paths_in(graph, row[1])
        if bad:
            row[1] += "  ⚠ 失效：%s" % ", ".join("`%s`" % b for b in bad)
    A(_tbl(ext_rows, ["扩展点", "注册/实现位置", "如何扩展"]))
    A("")

    # ---------- 12. 符号速查 ----------
    A("## 12. 符号速查表\n")
    A("只收录**公共**顶层函数/类/常量（私有不带下划线前缀的过多则截断）。"
      "完整清单见 `docs/code_graph.json` 的 `modules[*].symbols`。\n")
    for rel, m in sorted(graph.py.items()):
        pubs = [s for s in m.symbols if s["public"] and s["kind"] != "method"]
        if not pubs:
            continue
        A("<details><summary><b><code>%s</code></b>（%d 个公共符号）</summary>\n" % (rel, len(pubs)))
        rows = []
        for s in sorted(pubs, key=lambda x: x["line"])[:60]:
            doc = s["doc"].replace("|", "/")
            rows.append([s["kind"], "`%s%s`" % (s["name"], s["sig"][:70]),
                         s["line"], (doc[:90] if doc else "—")])
        A(_tbl(rows, ["类型", "符号", "行号", "说明"]))
        if len(pubs) > 60:
            A("\n> 其余 %d 个省略\n" % (len(pubs) - 60))
        A("</details>\n")

    # ---------- 13. 第三方依赖面 ----------
    A("## 13. 第三方依赖面（离线交付必须打包这些）\n")
    ext_counter = defaultdict(int)
    for rel, m in graph.py.items():
        for e, c in m.imports_ext.items():
            ext_counter[e] += c
    rows = sorted(ext_counter.items(), key=lambda x: -x[1])
    A(_tbl([["`%s`" % e, c] for e, c in rows[:40]], ["第三方包", "被引用次数"]))
    A("")

    # ---------- 14. 与上一版的差异 ----------
    if old:
        A("## 14. 结构变更（相对上一次生成）\n")
        d = diff_graph(old, _snapshot(graph))
        if not any(d.values()):
            A("本次与上次的**模块/依赖/路由结构完全一致**（可能只是注释或函数体变化）。\n")
        else:
            for title, items in d.items():
                if not items:
                    continue
                A("**%s**：\n" % title)
                for line in items[:40]:
                    A("- %s" % line)
                if len(items) > 40:
                    A("- … 还有 %d 条（完整见 code_graph.json 对比）" % (len(items) - 40))
                A("")

    A("---\n")
    A("## 维护者须知\n")
    A("1. 每次大改后运行 `python scripts/gen_code_graph.py` 重生成；运行 `--diff` 查看结构漂移。  ")
    A("2. 若发现 §9 链路或 §10 指引出现 ⚠，**修改本脚本里的 `CHAINS` / `CHANGE_GUIDE` 常量**——"
      "这是图谱防腐化的唯一钩子。  ")
    A("3. 模块职责取自各文件的 **module docstring**，因此写好 docstring = 自动获得更好的图谱。  ")
    A("4. 静态分析看不到运行时动态注册（注册表/插件/字符串名字调度），"
      "此类遗漏已在 §11 显式列出扩展点。\n")
    return "\n".join(L)


def _exists_path(graph, rel):
    return rel in graph.py or os.path.exists(os.path.join(PROJECT_ROOT, rel))


_RE_BACKTICK = re.compile(r"`([^`]+)`")


def _bad_paths_in(graph, text, where=""):
    """校验一段 Markdown 文本里引用的所有代码路径是否真实存在。

    只检查看起来像路径的 token（以 .py 结尾或以 / 结尾），带 `*` / `<` 的通配符跳过。
    任何一条不存在都会写进 ISSUES——不允许文档引用不存在的文件还装作没事。
    """
    bad = []
    for tok in _RE_BACKTICK.findall(text or ""):
        tok = tok.strip().split()[0] if tok.strip() else ""
        if not tok or "*" in tok or "<" in tok:
            continue
        if not (tok.endswith(".py") or tok.endswith("/")):
            continue
        if tok.endswith("/") and not any(r.startswith(tok) for r in graph.py):
            if not os.path.isdir(os.path.join(PROJECT_ROOT, tok)):
                bad.append(tok)
        elif not _exists_path(graph, tok):
            bad.append(tok)
    for b in bad:
        _issue("ERROR", "文档引用了不存在的路径",
               "%s`%s` 不存在%s" % (where and where + "：", b, ""))
    return bad


def _template_render_lines(graph, template_rel):
    """找出渲染该模板的所有位置：[(模块, 行号)]，数据来自 AST 的 render_template 实参。"""
    hits = []
    for r, m in graph.py.items():
        for name, lineno in m.renders:
            if name == template_rel or name == template_rel.replace("templates/", ""):
                hits.append((r, lineno))
    return sorted(hits, key=lambda x: (x[0], x[1]))


def _find_page_route(graph, rel):
    """模板 -> 页面路由。

    归属方法：render_template 调用的行号落在哪个 @route 装饰的处理函数范围内
    （同文件内按行号升序，取**紧接着其上方**的那条路由），这是行号级的精确归属。
    找不到就返回空字符串——宁可显示"—"，也不猜一个看起来像的路由。
    """
    lines = {}
    for src_mod, lineno in _template_render_lines(graph, rel):
        m = graph.py.get(src_mod)
        if not m or not m.routes:
            continue
        ordered = sorted(m.routes, key=lambda r: r["line"])
        owner = None
        for rt in ordered:
            if rt["line"] <= lineno:
                owner = rt
            else:
                break
        if owner is not None:
            lines[src_mod] = owner["rule"]
    # 页面路由优先取 app.py 那一条
    for prefer in ("app.py",):
        if prefer in lines:
            return lines[prefer]
    return sorted(lines.values())[0] if lines else ""


def _verify_exists(graph, path, func):
    """校验手写注解里的文件/函数是否仍然存在于**当前代码**。

    返回 (标记, 说明)。三种结果：
      ✅ 文件/符号都在（AST 确证）
      🧩 路径本身是占位模板（如 `core/engines/<新引擎>.py`），不校验也不伪装成文件
      ❌ 文件不存在  ⚠ 文件在但符号不存在
    """
    if _is_placeholder(path):
        return "🧩", "占位模板：%s" % path
    if path.endswith("/"):
        hits = [r for r in graph.py if r.startswith(path)]
        return ("✅" if hits else "❌"), ("%d 个文件" % len(hits) if hits else "目录不存在")
    if path not in graph.py:
        if os.path.exists(os.path.join(PROJECT_ROOT, path)):
            return "✅", "存在（未在扫描范围内）"
        return "❌", "文件不存在"
    if func:
        m = graph.py[path]
        exact = {s["name"]: s["line"] for s in m.symbols}
        if func in exact:
            return "✅", "%s() @ L%d" % (func, exact[func])
        # 只写裸函数名时，兼容 Class.method 形式（并记录类名）
        hits = sorted({"%s @ L%d" % (n, l) for n, l in exact.items()
                       if n.split(".")[-1] == func})
        if hits:
            return "✅", " / ".join(hits[:3]) + (" …" if len(hits) > 3 else "")
        return ("⚠", "%s 已不存在（%d 行，%d 个符号，请以代码为准）"
                % (func, m.loc, len(m.symbols)))
    return "✅", "%d 行" % graph.py[path].loc


def _scan_table_defs(graph):
    """返回 [(rel, lineno, table_name)] 扁平化的 [( (rel,lineno), name )]。"""
    out = []
    pat = re.compile(r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?`?([a-z_][a-z0-9_]*)`?", re.I)
    for rel in ("core/db.py", "init_db.py"):
        p = os.path.join(PROJECT_ROOT, rel)
        if not os.path.isfile(p):
            continue
        text = _read_text(p)
        for i, line in enumerate(text.splitlines(), 1):
            m = pat.search(line)
            if m:
                out.append(((rel, i), m.group(1).lower()))
    return out


# ============================================================================
# 七、快照 & 差异
# ============================================================================

def _snapshot(graph):
    snap = {
        "generated_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "modules": {},
        "templates": {},
        "js": {},
    }
    for rel, m in graph.py.items():
        snap["modules"][rel] = {
            "loc": m.loc,
            "layer": graph.layer_of(rel),
            "deps": sorted(m.imports_pkg.keys()),
            "rdeps": sorted(graph.imported_by.get(rel, ())),
            "routes": sorted(["%s %s" % (" ".join(r["methods"]), r["rule"])
                              for r in m.routes]),
            "symbols": sorted(s["name"] for s in m.symbols if s["public"]),
            "doc": m.doc,
        }
    for rel, t in graph.templates.items():
        snap["templates"][rel] = {"loc": t["loc"], "apis": t["apis"]}
    for rel, j in graph.js.items():
        snap["js"][rel] = {"loc": j["loc"], "apis": j["apis"]}
    return snap


def _route_set(snap):
    rs = set()
    for v in snap.get("modules", {}).values():
        rs.update(v.get("routes", []))
    return rs


def diff_graph(old, new):
    """返回 Dict[分类, [描述行]]。"""
    om, nm = old.get("modules", {}), new.get("modules", {})
    out = {"新增模块": [], "删除模块": [], "依赖边变化": [], "接口变化": [],
           "行数剧变（>20% 且 >30 行）": [], "公共符号变化": []}
    for rel in sorted(set(nm) - set(om)):
        out["新增模块"].append("`%s`（%d 行，%s）" % (rel, nm[rel]["loc"], nm[rel]["layer"]))
    for rel in sorted(set(om) - set(nm)):
        out["删除模块"].append("`%s`（原 %d 行）" % (rel, om[rel]["loc"]))
    for rel in sorted(set(nm) & set(om)):
        d0, d1 = set(om[rel]["deps"]), set(nm[rel]["deps"])
        add, rem = d1 - d0, d0 - d1
        if add or rem:
            out["依赖边变化"].append(
                "`%s`: +%s / -%s" % (rel,
                                     ",".join(sorted(add)) or "—",
                                     ",".join(sorted(rem)) or "—"))
        l0, l1 = om[rel]["loc"], nm[rel]["loc"]
        if abs(l1 - l0) > 30 and abs(l1 - l0) / max(l0, 1) > 0.2:
            out["行数剧变（>20% 且 >30 行）"].append(
                "`%s`: %d → %d 行" % (rel, l0, l1))
        s0, s1 = set(om[rel]["symbols"]), set(nm[rel]["symbols"])
        sa, sr = s1 - s0, s0 - s1
        if sa or sr:
            out["公共符号变化"].append(
                "`%s`: 新增 %s / 移除 %s" % (
                    rel, ",".join(sorted(sa)[:6]) or "—", ",".join(sorted(sr)[:6]) or "—"))
    r0, r1 = _route_set(old), _route_set(new)
    for r in sorted(r1 - r0):
        out["接口变化"].append("新增 `%s`" % r)
    for r in sorted(r0 - r1):
        out["接口变化"].append("移除 `%s`" % r)
    return out


def render_diff_text(old, new):
    d = diff_graph(old, new)
    lines = ["== AIDBM 代码结构变更摘要 ==",
             "上次生成: %s ｜ 本次: %s" % (old.get("generated_at", "?"),
                                          new.get("generated_at", "?")), ""]
    total = 0
    for k, v in d.items():
        if v:
            lines.append("-- %s (%d)" % (k, len(v)))
            for x in v[:50]:
                lines.append("   " + x)
            if len(v) > 50:
                lines.append("   … 还有 %d 条" % (len(v) - 50))
            lines.append("")
            total += len(v)
    if not total:
        lines.append("本次与上次结构一致（无模块增删、无依赖边变化、无路由变化）。")
    return "\n".join(lines)


# ============================================================================
# 八、入口
# ============================================================================

def main(argv=None):
    ap = argparse.ArgumentParser(
        description="生成 AIDBM 代码图谱（替代通读代码的结构索引）")
    ap.add_argument("--roots", nargs="*", default=DEFAULT_ROOTS,
                    help="要分析的目录/文件（相对项目根），默认 %s" % DEFAULT_ROOTS)
    ap.add_argument("--out-dir", default="docs", help="输出目录，默认 docs")
    ap.add_argument("--include-all", action="store_true",
                    help="把 tests/、scripts/、tools/ 也纳入分析")
    ap.add_argument("--diff", action="store_true",
                    help="只输出相对上次快照的结构变更（不覆盖图谱）")
    ap.add_argument("--json-only", action="store_true", help="只产出 JSON")
    ap.add_argument("--name", default="code_graph", help="输出文件名前缀")
    ap.add_argument("--strict", action="store_true",
                    help="只要图谱里存在可信度 ERROR 就以非 0 退出（用于 CI / 提交钩子）")
    args = ap.parse_args(argv)

    graph = CodeGraph().build(args.roots, include_optional=args.include_all)
    snap_new = _snapshot(graph)

    out_dir = args.out_dir if os.path.isabs(args.out_dir) else \
        os.path.join(PROJECT_ROOT, args.out_dir)
    os.makedirs(out_dir, exist_ok=True)
    json_path = os.path.join(out_dir, args.name + ".json")
    md_path = os.path.join(out_dir, args.name + ".md")

    old = None
    if os.path.isfile(json_path):
        try:
            with io.open(json_path, "r", encoding="utf-8") as fh:
                old = json.load(fh)
        except Exception:
            old = None

    if args.diff:
        if not old:
            print("[!] 找不到历史快照 %s，无从对比；先跑一次无 --diff 的生成。" % json_path)
            return 1
        print(render_diff_text(old, snap_new))
        return 0

    # 无论是否输出 md，都要先渲染一次以收集 ISSUES（--strict 需要它）
    md = render_markdown(graph, old=old, include_all=args.include_all)

    with io.open(json_path, "w", encoding="utf-8") as fh:
        json.dump(snap_new, fh, ensure_ascii=False, indent=1)

    if not args.json_only:
        with io.open(md_path, "w", encoding="utf-8") as fh:
            fh.write(md)
        print("[ok] %s" % _rel(md_path))
    print("[ok] %s" % _rel(json_path))
    print("     Python 文件 %d 个，路由 %d 个，模板 %d 个，JS %d 个" % (
        len(graph.py),
        sum(len(m.routes) for m in graph.py.values()),
        len(graph.templates), len(graph.js)))
    if graph.errors:
        print("     ⚠ 解析失败 %d 个文件" % len(graph.errors))

    # ---- 可信度账本结算 ----
    errs = [i for i in ISSUES if i[0] == "ERROR"]
    warns = [i for i in ISSUES if i[0] == "WARN"]
    if errs or warns:
        print("\n== 数据可信度：ERROR %d / WARN %d ==" % (len(errs), len(warns)))
        for sev, kind, detail in errs + warns:
            print("  [%s] %s —— %s" % (sev, kind, detail))
        print("  （完整清单见图谱 §0.1）")
    else:
        print("     数据可信度：全部条目 AST 确证，0 个问题")
    if errs:
        if args.strict:
            print("\n[x] --strict：图谱存在 %d 个可信度 ERROR，拒绝产出可信报告。" % len(errs))
            return 1
        print("     ⚠ 图谱存在不可信数据，请以 §0.1 为准（--strict 可让命令失败）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
