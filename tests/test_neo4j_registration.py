# -*- coding: utf-8 -*-
"""Neo4j 全栈注册契约测试（任务2：引擎注册表 / config / dump_format / 前端下拉 / 连接探测）。

目的：把「新增一种数据库需要登记哪些地方」固化成断言，避免以后只改一半
（例如引擎注册了但前端下拉没有，或 dump_format 有格式但引擎不认）。

覆盖：
1. config：支持类型、默认端口、中文显示名；
2. 引擎注册表：ENGINE_REGISTRY / supported_types / engine_meta_map 能力声明；
3. dump_format：五种通道、默认通道、每个通道都有真实恢复方式；
4. 连接探测：core.probe 注册 Neo4j 且入参校验诚实；
5. 自定义适配器：BUILTIN_TYPES 含 neo4j（不允许被自定义类型占用）；
6. 物理备份模式被诚实拒绝（Neo4j 无数据文件级通道）；
7. 前端下拉与后端格式表一致（tasks.html 类型项 + app.js DUMP_FORMATS.neo4j）。
"""
import os
import re
import sys
import tempfile
import unittest

# ---------------- 0. 运行环境（导入 config 前设置） ----------------
os.environ["DEMO_MODE"] = "on"
_TMP = tempfile.mkdtemp(prefix="neo4j_reg_test_")
_MODULE_DB = os.path.join(_TMP, "instance", "meta.db")
os.makedirs(os.path.dirname(_MODULE_DB), exist_ok=True)
os.environ["META_DB_PATH"] = _MODULE_DB

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import config                                   # noqa: E402
import core.db as db                            # noqa: E402
config.META_DB_PATH = _MODULE_DB
db.init_schema()                                # noqa: E402

from core.engines import (                      # noqa: E402
    ENGINE_REGISTRY, ENGINE_DISPLAY, supported_types, engine_meta_map,
    get_engine, get_adapter_tier,
)
from core.engines.neo4j import Neo4jEngine      # noqa: E402
from core import dump_format, probe, db_adapters  # noqa: E402

_EXPECTED_FORMATS = ("dump", "backup", "apoc_cypher", "apoc_json", "apoc_csv")


class TestConfigRegistration(unittest.TestCase):
    """config.py 三张表：SUPPORTED_DB_TYPES / DEFAULT_PORTS / DB_DISPLAY_NAMES。"""

    def test_supported_db_types(self):
        self.assertIn("neo4j", config.SUPPORTED_DB_TYPES)

    def test_default_port(self):
        self.assertEqual(config.DEFAULT_PORTS.get("neo4j"), 7687)

    def test_display_name(self):
        self.assertEqual(config.DB_DISPLAY_NAMES.get("neo4j"), "Neo4j")


class TestEngineRegistry(unittest.TestCase):
    def test_registered(self):
        self.assertIs(ENGINE_REGISTRY.get("neo4j"), Neo4jEngine)
        self.assertEqual(ENGINE_DISPLAY.get("neo4j"), "Neo4j")
        self.assertIn("neo4j", supported_types())

    def test_get_engine(self):
        task = {"id": 0, "name": "t", "db_type": "neo4j", "host": "127.0.0.1",
                "port": 7687, "username": "neo4j", "password": "", "db_name": "neo4j",
                "backup_mode": "logical", "extra_options": "{}"}
        eng = get_engine("neo4j", task, config.BACKUP_ROOT, None)
        self.assertIsInstance(eng, Neo4jEngine)
        self.assertEqual(eng.db_type, "neo4j")
        self.assertEqual(eng._db_name(), "neo4j")
        self.assertEqual(eng._bolt_uri(), "bolt://127.0.0.1:7687")

    def test_adapter_tier(self):
        self.assertEqual(get_adapter_tier("neo4j"), "peripheral_api")

    def test_meta_map_capabilities(self):
        m = engine_meta_map()["neo4j"]
        self.assertEqual(m["default_port"], 7687)
        self.assertEqual(m["backup_modes"], ["logical"])      # 无物理通道
        self.assertFalse(m["supports_full_instance"])          # 无全实例语义
        self.assertFalse(m["supports_sync"])                   # 未接入数据同步
        self.assertTrue(m["builtin"])


class TestDumpFormat(unittest.TestCase):
    def test_channels(self):
        vals = [f["value"] for f in dump_format.supported_formats("neo4j")]
        self.assertEqual(tuple(vals), _EXPECTED_FORMATS)

    def test_default_channel(self):
        self.assertEqual(dump_format.resolve("neo4j", {})["value"], "dump")

    def test_resolve_each_channel(self):
        ext_map = {"dump": ".dump", "backup": ".backup.tar.gz",
                   "apoc_cypher": ".cypher", "apoc_json": ".json",
                   "apoc_csv": ".csv.zip"}
        for v, ext in ext_map.items():
            item = dump_format.resolve("neo4j", {"dump_format": v})
            self.assertEqual(item["value"], v)
            self.assertEqual(item["ext"], ext)
            # 每个通道都必须有真实恢复方式，不允许出现不可恢复的假选项
            self.assertTrue(item.get("restore"))
            self.assertNotEqual(item.get("restore"), "unsupported")

    def test_engine_method_matches(self):
        """引擎 _method() 必须认得 dump_format 的每个取值。"""
        task = {"id": 0, "name": "t", "db_type": "neo4j", "host": "127.0.0.1",
                "port": 7687, "username": "neo4j", "password": "",
                "db_name": "neo4j", "backup_mode": "logical",
                "extra_options": "{}"}
        for v in _EXPECTED_FORMATS:
            task = dict(task, extra_options='{"dump_format":"%s"}' % v)
            eng = Neo4jEngine(task, config.BACKUP_ROOT, None)
            self.assertEqual(eng._method(), v, msg="引擎未识别通道 %s" % v)


class TestProbe(unittest.TestCase):
    def test_registered(self):
        self.assertIn("neo4j", probe._PROBES)
        self.assertIs(probe._PROBES["neo4j"][1], probe._probe_neo4j)

    def test_missing_host_is_failure_not_unknown(self):
        ok, msg = probe.probe_db_connection("neo4j", "", 7687, "neo4j", "x", "neo4j", 3)
        self.assertFalse(ok)
        self.assertIn("主机", msg)

    def test_unreachable_is_honest(self):
        """不可达时允许未知(None)或失败(False)，但绝不能返回成功。"""
        ok, _msg = probe.probe_db_connection(
            "neo4j", "192.0.2.10", 7687, "neo4j", "x", "neo4j", 2)
        self.assertNotEqual(ok, True)


class TestBuiltinTypeGuard(unittest.TestCase):
    def test_not_occupiable_by_adapter(self):
        self.assertIn("neo4j", db_adapters.BUILTIN_TYPES)


class TestPhysicalModeRejected(unittest.TestCase):
    def test_preflight_physical(self):
        task = {"id": 0, "name": "t", "db_type": "neo4j", "host": "127.0.0.1",
                "port": 7687, "username": "neo4j", "password": "",
                "db_name": "neo4j", "backup_mode": "physical",
                "extra_options": "{}"}
        ok, msg = Neo4jEngine(task, config.BACKUP_ROOT, None).preflight()
        self.assertFalse(ok)
        self.assertIn("物理", msg)


class TestFrontendDropdown(unittest.TestCase):
    """前端下拉与后端格式表必须一致（防只改后端不改前端）。"""

    @staticmethod
    def _read(path):
        with open(os.path.join(PROJECT_ROOT, path), "r", encoding="utf-8") as f:
            return f.read()

    def test_tasks_html_has_option(self):
        html = self._read(os.path.join("templates", "tasks.html"))
        self.assertIn('<option value="neo4j">', html)

    def test_app_js_formats_match_backend(self):
        js = self._read(os.path.join("static", "js", "app.js"))
        m = re.search(r"neo4j:\s*\[(.*?)\],\s*\n\s*\};", js, re.S)
        self.assertTrue(m, msg="static/js/app.js 缺少 DUMP_FORMATS.neo4j")
        values = re.findall(r'\{\s*v:\s*"([^"]+)"', m.group(1))
        self.assertEqual(tuple(values), _EXPECTED_FORMATS)


if __name__ == "__main__":
    unittest.main()
