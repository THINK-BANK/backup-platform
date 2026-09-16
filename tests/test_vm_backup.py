# -*- coding: utf-8 -*-
"""虚拟机备份（core/vm 子系统）契约测试。

重点是把「新增一种受保护对象」容易漏登记的几处固化成断言：
1. 引擎可解析：core.vm.engine 与本模块循环依赖，只能惰性注册；
   一旦 get_engine('vm') 拿不到引擎，定时调度 / 手动备份 / 还原 / 克隆 /
   恢复验证会全线报「不支持的数据库类型」，必须守住。
2. 四种虚拟化平台（PVE / KVM-libvirt / ESXi / Hyper-V）均已注册且能给出
   静态能力声明（不支持块级增量时必须如实说明，不允许伪造增量）。
3. 增量链决策（journal.plan_next）与 RPO 判定（journal.rpo_state）为纯函数逻辑，
   无真实环境也能验证「不造假」。
4. 表结构与 API 端点可用；不可达时诚实失败而非假成功。

环境：不需要真实虚拟化平台，用不可达地址 127.0.0.1:1 验证失败路径（秒级返回）。
"""
import os
import sys
import tempfile
import unittest

os.environ["DEMO_MODE"] = "on"
_TMP = tempfile.mkdtemp(prefix="vm_test_")
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

from core.engines import get_engine             # noqa: E402
from core.vm import provider_for, list_providers  # noqa: E402
from core.vm import journal                     # noqa: E402
from core.vm.base import VMProviderError        # noqa: E402

VM_TABLES = ("vm_hypervisors", "vm_protected", "vm_recovery_points", "vm_jobs")
EXPECTED_PROVIDERS = ("pve", "libvirt_ssh", "esxi_ssh", "hyperv_ssh")
_UNREACHABLE = "127.0.0.1:1"      # 立即 Connection refused，避免测试超时


class TestSchema(unittest.TestCase):
    def test_vm_tables_created(self):
        names = {r["name"] for r in
                 db.query("SELECT name FROM sqlite_master WHERE type='table'")}
        for t in VM_TABLES:
            self.assertIn(t, names)


class TestEngineResolution(unittest.TestCase):
    """回归：VM 引擎惰性注册必须被 get_engine 兜住。"""

    def setUp(self):
        self.task = {"id": 1, "name": "vm-test", "db_type": "vm",
                     "host": "192.0.2.10", "port": None, "username": "root",
                     "db_name": "100", "backup_mode": "logical",
                     "extra_options": '{"vm_id": 1}'}

    def test_get_engine_vm(self):
        eng = get_engine("vm", self.task, config.BACKUP_ROOT, None)
        self.assertEqual(eng.db_type, "vm")
        self.assertEqual(eng.__class__.__name__, "VMBackupEngine")

    def test_backup_with_unknown_vm_fails_honestly(self):
        """受保护对象不存在时必须快速失败，不允许产出假成功。"""
        task = dict(self.task, extra_options='{"vm_id": 999999}')
        eng = get_engine("vm", task, config.BACKUP_ROOT, None)
        res = eng.backup("full")
        self.assertFalse(res.success)
        self.assertTrue(res.message)


class TestProviders(unittest.TestCase):
    def test_all_registered(self):
        ids = set()
        for p in list_providers():
            ids.add(p["id"] if isinstance(p, dict) else str(p))
        for p in EXPECTED_PROVIDERS:
            self.assertIn(p, ids)

    def test_capabilities_static(self):
        """能力声明必须能离线给出；且不支持增量时要如实说明原因。

        注：SSH 类 provider（KVM/ESXi/Hyper-V）的 capabilities() 需要先与宿主机
        建立连接，此处用 PVE（HTTP 通道）验证「不联网也能拿到能力声明」。
        """
        hv = {"id": 1, "name": "t", "provider": "pve",
              "endpoint": "https://" + _UNREACHABLE, "username": "root@pam",
              "password": "x", "extra_config": "{}"}
        caps = provider_for(hv).capabilities().to_dict()
        self.assertIn("incremental", caps)
        self.assertIn("clone_to_new_vm", caps)
        if not caps.get("incremental"):
            self.assertTrue(caps.get("notes"), msg="不支持增量却没说明原因")


class TestJournal(unittest.TestCase):
    """增量链与 RPO：不做假增量、不虚报达标。"""

    def test_no_incremental_capability_always_full(self):
        caps = {"incremental": False}
        self.assertEqual(journal.plan_next([], caps)["level"], "full")
        rows = [{"id": 1, "rp_type": "full", "pit_at": "2026-01-01 00:00:00",
                 "change_token": "t1"}]
        self.assertEqual(journal.plan_next(rows, caps)["level"], "full")

    def test_first_backup_is_full(self):
        caps = {"incremental": True}
        self.assertEqual(journal.plan_next([], caps)["level"], "full")

    def test_incremental_after_base(self):
        """有基座且在强制全量周期内 → 增量。"""
        caps = {"incremental": True}
        now = journal.datetime(2026, 1, 1, 2, 0, 0)
        rows = [{"id": 2, "rp_type": "full", "pit_at": "2026-01-01 00:00:00",
                 "change_token": "t1", "created_at": "2026-01-01 00:00:00"}]
        plan = journal.plan_next(rows, caps, now=now)
        self.assertEqual(plan["level"], "incremental")

    def test_force_full_when_base_too_old(self):
        """基座超过 full_interval_days → 强制全量（防增量链过长）。"""
        caps = {"incremental": True}
        now = journal.datetime(2026, 2, 1, 0, 0, 0)      # 距基座 31 天
        rows = [{"id": 2, "rp_type": "full", "pit_at": "2026-01-01 00:00:00",
                 "change_token": "t1", "created_at": "2026-01-01 00:00:00"}]
        plan = journal.plan_next(rows, caps, now=now)
        self.assertEqual(plan["level"], "full")

    def test_rpo_no_recovery_point(self):
        st = journal.rpo_state([], 60)
        self.assertFalse(st["ok"])
        self.assertIn("尚无恢复点", st["message"])

    def test_rpo_exceeded(self):
        rows = [{"pit_at": "2020-01-01 00:00:00"}]
        st = journal.rpo_state(rows, 60)
        self.assertFalse(st["ok"])


class TestVmApi(unittest.TestCase):
    """页面与 API 端点可用；不可达时诚实失败（不返回假成功）。"""

    @classmethod
    def setUpClass(cls):
        from app import create_app
        cls.app = create_app()
        cls.c = cls.app.test_client()
        cls.c.post("/login", json={"username": config.WEB_USERNAME,
                                   "password": config.WEB_PASSWORD},
                   content_type="application/json")

    def test_page(self):
        self.assertEqual(self.c.get("/vm").status_code, 200)

    def test_list_endpoints(self):
        for p in ("/api/vm/providers", "/api/vm/hypervisors", "/api/vm/protected",
                  "/api/vm/jobs", "/api/vm/rpo", "/api/vm/recovery-points"):
            self.assertEqual(self.c.get(p).status_code, 200, msg=p)

    def test_hypervisor_crud_and_unreachable_test(self):
        import core.models as models
        r = self.c.post("/api/vm/hypervisors", json={
            "name": "ut-pve", "provider": "pve",
            "endpoint": "https://" + _UNREACHABLE, "username": "root@pam",
            "password": "x", "verify_ssl": 0})
        self.assertIn(r.status_code, (200, 201))
        hv_id = (r.get_json() or {}).get("id")
        self.assertTrue(hv_id)

        t = self.c.post("/api/vm/hypervisors/test", json={
            "provider": "pve", "endpoint": "https://" + _UNREACHABLE,
            "username": "root@pam", "password": "x"})
        self.assertEqual(t.status_code, 200)
        self.assertNotEqual((t.get_json() or {}).get("ok"), True)

        models.delete_vm_hypervisor(hv_id)
        self.assertEqual(self.c.get("/api/vm/hypervisors").status_code, 200)


if __name__ == "__main__":
    unittest.main()
