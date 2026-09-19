# -*- coding: utf-8 -*-
"""core.agentless.plan（通道/侵入等级推导）与远端空目录回收的单测。

全部为纯函数或本地逻辑，**不连 SSH、不连数据库**，可在离线 CI 直接跑。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.agentless import plan as plan_mod          # noqa: E402
from core.agentless.channels import INVASION_LEVELS  # noqa: E402
from core.remote_dump import _stage_dir_candidates   # noqa: E402


HOST = {"id": 7, "host_key": "root@10.0.0.9:22", "hostname": "10.0.0.9"}


def _task(**kw) -> dict:
    base = {"id": 1, "name": "t1", "db_type": "mysql", "backup_mode": "logical",
            "host": "10.0.0.9", "port": 3306, "rt_enabled": 0,
            "extra_options": {}}
    base.update(kw)
    return base


# ------------------------------------------------------------------ plan
def test_mysql_logical_only_needs_ssh_a1():
    p = plan_mod.plan_for_task(_task(), HOST)
    ids = [c["id"] for c in p["channels"]]
    assert "ssh" in ids and "native-protocol" in ids
    assert p["max_level"] == "A1"
    assert p["temp_binary"] is False
    assert p["forbidden_ok"] is True


def test_physical_mysql_is_a2_temp_binary():
    p = plan_mod.plan_for_task(_task(backup_mode="physical"), HOST)
    assert p["temp_binary"] is True
    assert p["max_level"] == "A2"
    assert any(c["id"] == "temp-binary" for c in p["channels"])


def test_physical_postgres_uses_builtin_tool_not_push():
    p = plan_mod.plan_for_task(_task(db_type="postgresql", backup_mode="physical"),
                               HOST)
    assert p["temp_binary"] is False


def test_realtime_adds_a3_and_requirements():
    p = plan_mod.plan_for_task(_task(rt_enabled=1), HOST)
    assert p["max_level"] == "A3"
    assert p["requires_target_config"], "MySQL 实时应给出 binlog 前置要求"
    joined = " ".join(x["item"] for x in p["requires_target_config"]).lower()
    assert "binlog" in joined


def test_object_storage_and_vm_are_rest_a0():
    for t in ("object_storage", "vm"):
        p = plan_mod.plan_for_task(_task(db_type=t))
        assert p["channels"][0]["id"] == "rest"
        assert p["max_level"] == "A0"


def test_missing_ssh_host_falls_back_to_local_channel():
    p = plan_mod.plan_for_task(_task())
    assert p["ssh_host_id"] in (None, "")
    assert "本机" in p["target"]
    assert any(c["id"] == "ssh" for c in p["channels"])


def test_extra_options_json_string_supported():
    t = _task(extra_options='{"ssh_host_id": 7}')
    p = plan_mod.plan_for_task(t, HOST)
    assert p["ssh_host_id"] in (7, "7")


def test_plan_tasks_batch_and_summarize():
    plans = plan_mod.plan_tasks(
        [_task(), _task(id=2, backup_mode="physical"), _task(id=3, rt_enabled=1)],
        {7: HOST})
    s = plan_mod.summarize(plans)
    assert s["total"] == 3 and s["worst_level"] == "A3"
    assert s["temp_binary_tasks"] == 1


def test_plan_tasks_never_crashes_on_bad_task():
    plans = plan_mod.plan_tasks([{"id": 9, "extra_options": "{bad json"}])
    assert plans and plans[0]["task_id"] == 9


def test_level_desc_present():
    p = plan_mod.plan_for_task(_task(backup_mode="physical"), HOST)
    assert p["max_level_desc"] == INVASION_LEVELS[p["max_level"]]


# ------------------------------------------------- 远端空目录回收（零残留）
def test_stage_dir_candidates_stop_at_root_level():
    """产物在这条路径上时，只能回收 bk_stage/fixed、bk_stage，**绝不能到 /tmp**。"""
    got = _stage_dir_candidates(["/tmp/bk_stage/fixed/t-1_mysql_phys_full.tar.gz"])
    assert got == ["/tmp/bk_stage/fixed", "/tmp/bk_stage"]
    assert "/tmp" not in got


def test_stage_dir_candidates_var_tmp():
    got = _stage_dir_candidates(["/var/tmp/vmbk_stage/t1/x.tar"])
    assert got == ["/var/tmp/vmbk_stage/t1", "/var/tmp/vmbk_stage", "/var/tmp"]
    assert "/var" not in got


def test_stage_dir_candidates_edge_cases():
    assert _stage_dir_candidates([]) == []
    assert _stage_dir_candidates(["/tmp"]) == []
    assert _stage_dir_candidates([""]) == []
    assert _stage_dir_candidates(None) == []


def test_stage_dir_candidates_dedup_and_order():
    got = _stage_dir_candidates(["/tmp/bk_stage/fixed/a.tar.gz",
                                 "/tmp/bk_stage/fixed/b.tar.gz"])
    assert got == ["/tmp/bk_stage/fixed", "/tmp/bk_stage"]
