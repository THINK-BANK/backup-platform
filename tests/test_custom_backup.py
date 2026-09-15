# -*- coding: utf-8 -*-
"""自定义脚本备份（全库 / 单表 / 全实例）单元测试。

覆盖：
- 备份范围归一化与表名解析；
- 各类数据库模板生成（全库/单表/全实例）；
- 预检查：单表范围未填表名必须拦截；
- 本机执行通道：脚本执行、产物落盘、size/sha256、环境变量注入（含范围与表名）；
- 密码只走环境变量、不出现在命令行参数里。
"""
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
import core.db as db  # noqa: E402
from core import custom_scripts  # noqa: E402
from core.engines import get_engine  # noqa: E402
from core.engines.base import BackupType  # noqa: E402

db.init_schema()


def _task(extra: dict, host: str = "127.0.0.1") -> dict:
    return {
        "id": 9001, "name": "ut-custom", "db_type": "mysql", "host": host,
        "port": 3306, "username": "root", "password": db.encrypt_secret("p@ss word"),
        "db_name": "ut_db", "backup_type": "full", "backup_mode": "custom",
        "extra_options": json.dumps(extra, ensure_ascii=False),
    }


class TestScopeAndTemplate(unittest.TestCase):
    def test_normalize_scope_alias(self):
        self.assertEqual(custom_scripts.normalize_scope("full"), custom_scripts.SCOPE_DATABASE)
        self.assertEqual(custom_scripts.normalize_scope("table"), custom_scripts.SCOPE_TABLE)
        self.assertEqual(custom_scripts.normalize_scope("all"), custom_scripts.SCOPE_INSTANCE)
        self.assertEqual(custom_scripts.normalize_scope(""), custom_scripts.SCOPE_DATABASE)
        self.assertEqual(custom_scripts.normalize_scope("乱写"), custom_scripts.SCOPE_DATABASE)

    def test_parse_tables(self):
        self.assertEqual(custom_scripts.parse_tables("t_a, t_b  t_c"), ["t_a", "t_b", "t_c"])
        self.assertEqual(custom_scripts.parse_tables(""), [])
        self.assertEqual(custom_scripts.parse_tables(None), [])

    def test_templates_all_types(self):
        for db_type in ("mysql", "postgresql", "oracle", "dameng", "sqlserver",
                        "mongodb", "kingbase", "unknown-db"):
            for scope in custom_scripts.SCOPES:
                tpl = custom_scripts.template(db_type, scope)
                self.assertEqual(tpl["scope"], scope)
                self.assertIn("PLATFORM_BACKUP_DIR", tpl["backup"])
                self.assertTrue(tpl["backup"].startswith("#!/bin/bash"))
                self.assertIn("PLATFORM_BACKUP_FILE", tpl["restore"])
                if scope == custom_scripts.SCOPE_TABLE:
                    # 单表模板必须对空表名显式报错，避免"静默导出全库"
                    self.assertIn("PLATFORM_TABLES", tpl["backup"])

    def test_preflight_reject_table_scope_without_tables(self):
        task = _task({"custom_script": "echo hi", "custom_scope": "single_table"})
        engine = get_engine("mysql", task, config.BACKUP_ROOT, db.get_logger("ut"))
        with mock.patch("core.remote_dump.resolve_ssh_host", return_value=None):
            ok, msg = engine.preflight()
        self.assertFalse(ok)
        self.assertIn("表名", msg)

    def test_preflight_ok_local_channel(self):
        task = _task({"custom_script": "echo hi", "custom_scope": "full_database"})
        engine = get_engine("mysql", task, config.BACKUP_ROOT, db.get_logger("ut"))
        with mock.patch("core.remote_dump.resolve_ssh_host", return_value=None):
            ok, msg = engine.preflight()
        self.assertTrue(ok)
        self.assertIn("本机", msg)


class TestLocalChannel(unittest.TestCase):
    """本机执行通道：脚本真实执行 + 产物回收 + 变量注入。"""

    BACKUP_SCRIPT = r"""#!/bin/bash
set -e
mkdir -p "$PLATFORM_BACKUP_DIR"
F="$PLATFORM_BACKUP_DIR/ut_$(date +%Y%m%d%H%M%S).txt"
{
  echo "scope=$PLATFORM_BACKUP_SCOPE"
  echo "tables=$PLATFORM_TABLES"
  echo "db=$PLATFORM_DB_NAME"
  echo "type=$PLATFORM_DB_TYPE"
  echo "level=$PLATFORM_BACKUP_LEVEL"
  echo "dir=$PLATFORM_BACKUP_DIR"
  echo "pwd_len=${#PLATFORM_DB_PASSWORD}"
} > "$F"
"""

    RESTORE_SCRIPT = r"""#!/bin/bash
set -e
F="${PLATFORM_BACKUP_FILE}.restored"
{
  echo "file=$PLATFORM_BACKUP_FILE"
  echo "target=$PLATFORM_RESTORE_DB"
  echo "scope=$PLATFORM_RESTORE_SCOPE"
  echo "tables=$PLATFORM_TABLES"
} > "$F"
"""

    def _run_backup(self, scope, tables=None):
        extra = {"custom_script": self.BACKUP_SCRIPT, "custom_scope": scope,
                 "custom_timeout": 60}
        if tables:
            extra["custom_tables"] = ",".join(tables)
        task = _task(extra)
        engine = get_engine("mysql", task, config.BACKUP_ROOT, db.get_logger("ut"))
        with mock.patch("core.remote_dump.resolve_ssh_host", return_value=None):
            res = engine.run_backup(BackupType.FULL)
        return res

    def test_backup_full_database(self):
        res = self._run_backup(custom_scripts.SCOPE_DATABASE)
        self.assertTrue(res.success, res.message)
        self.assertTrue(res.backup_path and os.path.exists(res.backup_path))
        self.assertGreater(res.size_bytes, 0)
        self.assertEqual(len(res.checksum or ""), 64)
        content = open(res.backup_path, encoding="utf-8").read()
        self.assertIn("scope=full_database", content)
        self.assertIn("db=ut_db", content)
        self.assertIn("type=mysql", content)
        self.assertIn("level=full", content)
        self.assertIn("pwd_len=9", content)  # 密码完整注入（含空格与 @）

    def test_backup_single_table_env(self):
        res = self._run_backup(custom_scripts.SCOPE_TABLE, ["t_order", "t_user"])
        self.assertTrue(res.success, res.message)
        content = open(res.backup_path, encoding="utf-8").read()
        self.assertIn("scope=single_table", content)
        self.assertIn("tables=t_order,t_user", content)

    def test_backup_no_artifact_failed(self):
        extra = {"custom_script": "echo 'nothing produced'",
                 "custom_scope": custom_scripts.SCOPE_DATABASE}
        task = _task(extra)
        engine = get_engine("mysql", task, config.BACKUP_ROOT, db.get_logger("ut"))
        with mock.patch("core.remote_dump.resolve_ssh_host", return_value=None):
            res = engine.run_backup(BackupType.FULL)
        self.assertFalse(res.success)
        self.assertIn("未产出备份文件", res.message)

    def test_backup_script_failed_rc(self):
        extra = {"custom_script": "echo boom >&2; exit 3",
                 "custom_scope": custom_scripts.SCOPE_DATABASE}
        task = _task(extra)
        engine = get_engine("mysql", task, config.BACKUP_ROOT, db.get_logger("ut"))
        with mock.patch("core.remote_dump.resolve_ssh_host", return_value=None):
            res = engine.run_backup(BackupType.FULL)
        self.assertFalse(res.success)
        self.assertIn("rc=3", res.message)
        self.assertIn("boom", res.stderr)

    def test_restore_local_channel_env(self):
        extra = {"custom_script": self.BACKUP_SCRIPT,
                 "custom_restore_script": self.RESTORE_SCRIPT,
                 "custom_scope": custom_scripts.SCOPE_TABLE,
                 "custom_tables": "t_order", "custom_timeout": 60}
        task = _task(extra)
        engine = get_engine("mysql", task, config.BACKUP_ROOT, db.get_logger("ut"))
        with mock.patch("core.remote_dump.resolve_ssh_host", return_value=None):
            bres = engine.run_backup(BackupType.FULL)
            self.assertTrue(bres.success, bres.message)
            rres = engine.run_restore(bres.backup_path, target_db="ut_db_target")
        self.assertTrue(rres.success, rres.message)
        out = open(bres.backup_path + ".restored", encoding="utf-8").read()
        self.assertIn("target=ut_db_target", out)
        self.assertIn("scope=single_table", out)
        self.assertIn("tables=t_order", out)

    def test_password_not_in_commandline(self):
        """密码只经环境变量传递，不出现在 bash 命令行参数里。"""
        seen = {}

        def fake_run(cmd, env=None, **kw):
            seen["cmd"] = cmd
            seen["env"] = env
            import subprocess
            return subprocess.CompletedProcess(cmd, 0, b"", b"")

        extra = {"custom_script": self.BACKUP_SCRIPT,
                 "custom_scope": custom_scripts.SCOPE_DATABASE}
        task = _task(extra)
        engine = get_engine("mysql", task, config.BACKUP_ROOT, db.get_logger("ut"))
        with mock.patch("core.remote_dump.resolve_ssh_host", return_value=None), \
                mock.patch("subprocess.run", side_effect=fake_run):
            engine.run_backup(BackupType.FULL)
        self.assertEqual(seen["cmd"][:2], ["bash", seen["cmd"][1]])
        self.assertNotIn("p@ss word", " ".join(seen["cmd"]))
        self.assertEqual(seen["env"].get("PLATFORM_DB_PASSWORD"), "p@ss word")


if __name__ == "__main__":
    unittest.main(verbosity=2)
