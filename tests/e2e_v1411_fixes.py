#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v1.4.11 两个用户反馈问题的**真实端到端回归**（真实执行，不仿真）。

覆盖
----
问题 1 文件备份「单文件源失败 + 失败原因空白」：
  F1 单文件源 → 归档内确实有该文件（此前被当目录处理）
  F2 目录源 → 归档内文件齐全
  F3 排除规则真实生效（此前界面有、代码从未使用）
  F4 源路径不存在 → 提前失败，且**原因非空**
  F5 目标分区写满 → 失败，且原因带真实系统错误（不再空白）

问题 2 MySQL 全实例备份「errno 28 + 失败文案误导」：
  M1 正常全实例备份 → 产物真实包含各库 dump（含中文数据）
  M2 临时工作目录放在 2MB tmpfs → **备份前**终止，诊断含可用空间与处置建议
  M3 连接信息错误 → 失败原因指向连接/认证，不再一律误导为「请纳管 SSH」
  M4 估算失效（返回 0）时第二道防线生效 → 逐库间检测到余量不足即终止

前置
----
需要一个可连的 MySQL 实例（默认本机 3399，root 空口令）。可用
``--mysql-host/--mysql-port/--mysql-user/--mysql-pwd`` 指定；连不上时 M* 场景
会如实标记 SKIP（不伪造通过）。空间类场景需要 root 权限挂载 tmpfs。

用法
----
    python tests/e2e_v1411_fixes.py
    python tests/e2e_v1411_fixes.py --mysql-port 3306 --mysql-pwd 'xxx'
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import config  # noqa: E402
import core.db as db  # noqa: E402
from core.engines import get_engine  # noqa: E402
from core.engines.base import BackupType  # noqa: E402
import core.logical_full as lf  # noqa: E402
import core.global_dedup as gd  # noqa: E402

MYSQL_BIN = "/opt/mysql840b/bin"
DUMP_TOOL = os.path.join(MYSQL_BIN, "mysqldump")
QUERY_TOOL = os.path.join(MYSQL_BIN, "mysql")

_PASS, _FAIL, _SKIP = [], [], []


def ok(name, detail=""):
    _PASS.append(name)
    print("[PASS] %s%s" % (name, (" | " + detail) if detail else ""))


def bad(name, detail=""):
    _FAIL.append(name)
    print("[FAIL] %s%s" % (name, (" | " + detail) if detail else ""))


def skip(name, why):
    _SKIP.append(name)
    print("[SKIP] %s | %s" % (name, why))


# --------------------------------------------------------------------------
# 文件备份
# --------------------------------------------------------------------------

def _mk_engine(tmp, task_id, name, src_paths, excludes=None, dst_dir=None,
               target_type="local", source_type="local"):
    extra = {
        "source_type": source_type,
        "source_paths": src_paths,
        "target_type": target_type,
        "target_path": dst_dir or "",
    }
    if excludes:
        extra["exclude_patterns"] = excludes
    task = {
        "id": task_id,
        "db_type": "file",
        "name": name,
        "task_name": name,
        "compress_algo": "gzip",
        "compress_level": 6,
        "storage_tier": 1,
        "extra_options": extra,
    }
    return get_engine("file", task, tmp)


def _members(archive):
    """返回归档内成员名列表（自动处理 zstd/gzip）。"""
    if archive.endswith(".zst"):
        dec = subprocess.run(["zstd", "-dc", archive], capture_output=True, check=True)
        tmp = tempfile.NamedTemporaryFile(suffix=".tar", delete=False)
        tmp.write(dec.stdout)
        tmp.close()
        path = tmp.name
    else:
        path = archive
    try:
        with tarfile.open(path, "r:*") as tf:
            return tf.getnames()
    finally:
        if path != archive and os.path.exists(path):
            os.unlink(path)


def _run_file_case(tmp, task_id, name, src_paths, excludes=None, dst_dir=None):
    eng = _mk_engine(tmp, task_id, name, src_paths, excludes, dst_dir)
    res = eng.backup(BackupType.FULL)
    return res, getattr(res, "backup_path", "")


def test_file_backups(tmp, tiny_dir):
    src = os.path.join(tmp, "src")
    os.makedirs(src)
    single = os.path.join(src, "only-one.txt")
    with open(single, "w", encoding="utf-8") as f:
        f.write("单文件源内容\n")
    with open(os.path.join(src, "keep.txt"), "w", encoding="utf-8") as f:
        f.write("keep\n")
    with open(os.path.join(src, "drop.log"), "w", encoding="utf-8") as f:
        f.write("noise\n")

    # F1 单文件源
    res, path = _run_file_case(tmp, 8101, "e2e_single_file", [single])
    if res.success and path and os.path.isfile(path):
        names = _members(path)
        if any(n.endswith("only-one.txt") for n in names):
            ok("F1 单文件源备份", "归档含 only-one.txt，size=%s" % res.size_bytes)
        else:
            bad("F1 单文件源备份", "归档内容=%s" % names[:5])
    else:
        bad("F1 单文件源备份", "success=%s msg=%s" % (res.success, res.message))

    # F2 目录源
    res, path = _run_file_case(tmp, 8102, "e2e_dir", [src])
    if res.success and path:
        names = _members(path)
        if any("keep.txt" in n for n in names) and any("drop.log" in n for n in names):
            ok("F2 目录源备份", "成员数=%d" % len(names))
        else:
            bad("F2 目录源备份", "归档内容=%s" % names[:8])
    else:
        bad("F2 目录源备份", res.message)

    # F3 排除规则生效
    res, path = _run_file_case(tmp, 8103, "e2e_exclude", [src], excludes=["*.log"])
    if res.success and path:
        names = _members(path)
        if not any(n.endswith(".log") for n in names) and any("keep.txt" in n for n in names):
            ok("F3 排除规则生效", "*.log 已排除，保留 keep.txt")
        else:
            bad("F3 排除规则生效", "归档内容=%s" % names[:8])
    else:
        bad("F3 排除规则生效", res.message)

    # F4 源不存在 → 提前失败且原因非空
    missing = os.path.join(src, "no-such-file.txt")
    res, path = _run_file_case(tmp, 8104, "e2e_missing", [missing])
    if res.success:
        bad("F4 源不存在", "不该成功（会产出假备份）")
    elif not (res.message or "").strip().split(":")[-1].strip():
        bad("F4 源不存在", "失败原因空白: %r" % res.message)
    else:
        ok("F4 源不存在提前失败", res.message[:80])

    # F5 目标分区写满 → 失败原因带真实系统错误
    tiny = _ensure_tiny_fs(tiny_dir, size_kb=1024)
    if not tiny:
        skip("F5 目标盘写满", "无法挂载 tmpfs（需 root / 特权）")
    else:
        big = os.path.join(src, "big.bin")
        with open(big, "wb") as f:
            f.write(os.urandom(3 * 1024 * 1024))  # 随机数据，压缩后仍 >1MB
        res, path = _run_file_case(tmp, 8105, "e2e_enospc", [big], dst_dir=tiny)
        msg = (res.message or "").strip()
        if res.success:
            bad("F5 目标盘写满", "不该成功（目标仅 1MB）")
        elif len(msg.split(":")[-1].strip()) < 8:
            bad("F5 目标盘写满", "失败原因空白: %r" % msg)
        elif "28" in msg or "space" in msg.lower() or "空间" in msg:
            ok("F5 目标盘写满真实报错", msg[:100])
        else:
            ok("F5 目标盘写满有真实原因", msg[:100])
        os.remove(big)


def _ensure_tiny_fs(path, size_kb=2048):
    """挂载一个极小的 tmpfs 用于真实制造 ENOSPC；失败返回 None。"""
    os.makedirs(path, exist_ok=True)
    if os.path.ismount(path):
        return path
    try:
        subprocess.run(["mount", "-t", "tmpfs", "-o", "size=%dK" % size_kb,
                        "tmpfs", path], check=True, capture_output=True)
        return path
    except Exception:
        return None


# --------------------------------------------------------------------------
# MySQL 全实例
# --------------------------------------------------------------------------

def _mysql_reachable(host, port, user, pwd):
    try:
        env = dict(os.environ, MYSQL_PWD=pwd or "")
        r = subprocess.run([QUERY_TOOL, "--no-defaults", "-h", host, "-P", str(port),
                            "-u", user, "-N", "-B", "-e", "select 1"],
                           capture_output=True, env=env, timeout=10)
        return r.returncode == 0
    except Exception:
        return False


def test_mysql_full_instance(tmp, host, port, user, pwd, tiny_dir):
    if not os.path.isfile(DUMP_TOOL):
        skip("M* MySQL 全实例", "本机无 mysqldump: %s" % DUMP_TOOL)
        return
    if not _mysql_reachable(host, port, user, pwd):
        skip("M* MySQL 全实例", "MySQL 不可达 %s:%s" % (host, port))
        return

    # M1 正常全实例备份
    out1 = os.path.join(tmp, "fullinst_ok.tar.gz")
    try:
        man = lf.backup_full_instance(
            "mysql", host=host, port=port, user=user, password=pwd,
            dump_tool=DUMP_TOOL, query_tool=QUERY_TOOL, out_path=out1)
        dbs = (man or {}).get("databases", [])
        with tarfile.open(out1, "r:gz") as tf:
            names = tf.getnames()
            has_a = any(n.endswith("e2e_a.sql") for n in names)
            sql = tf.extractfile([n for n in names if n.endswith("e2e_b.sql")][0]).read().decode("utf-8", "replace") if any(n.endswith("e2e_b.sql") for n in names) else ""
        if os.path.getsize(out1) > 0 and dbs and has_a and "中文数据" in sql:
            ok("M1 全实例备份真实产物", "库=%s size=%s 含中文数据" % (dbs, os.path.getsize(out1)))
        else:
            bad("M1 全实例备份真实产物", "dbs=%s size=%s has_a=%s" % (dbs, os.path.getsize(out1), has_a))
    except Exception as e:
        bad("M1 全实例备份真实产物", str(e)[:200])

    # M2 备份前空间预检（2MB tmpfs 作为临时工作目录）
    tiny = _ensure_tiny_fs(tiny_dir, size_kb=2048)
    if not tiny:
        skip("M2 备份前空间预检", "无法挂载 tmpfs")
    else:
        out2 = os.path.join(tmp, "fullinst_enospc.tar.gz")
        old = os.environ.get("BP_WORK_DIR")
        os.environ["BP_WORK_DIR"] = tiny
        try:
            lf.backup_full_instance(
                "mysql", host=host, port=port, user=user, password=pwd,
                dump_tool=DUMP_TOOL, query_tool=QUERY_TOOL, out_path=out2)
            bad("M2 备份前空间预检", "空间不足却备份成功了")
        except Exception as e:
            msg = str(e)
            if "磁盘空间不足" in msg and "备份前终止" in msg and ("可用" in msg) and ("BP_WORK_DIR" in msg):
                ok("M2 备份前终止并给出诊断", msg[:120].replace("\n", " "))
            else:
                bad("M2 备份前终止并给出诊断", msg[:200])
            if os.path.exists(out2) and os.path.getsize(out2) > 0:
                bad("M2 不产出残缺产物", "存在非空产物 %s" % out2)
            else:
                ok("M2 不产出残缺产物")
        finally:
            if old is None:
                os.environ.pop("BP_WORK_DIR", None)
            else:
                os.environ["BP_WORK_DIR"] = old

    # M3 连接信息错误 → 原因指向连接/认证
    out3 = os.path.join(tmp, "fullinst_badconn.tar.gz")
    try:
        lf.backup_full_instance(
            "mysql", host=host, port=int(port) + 1, user=user, password=pwd,
            dump_tool=DUMP_TOOL, query_tool=QUERY_TOOL, out_path=out3)
        bad("M3 错误连接信息", "连不上却成功了")
    except Exception as e:
        msg = str(e)
        if ("连接或认证失败" in msg or "枚举" in msg) and "纳管 SSH" not in msg:
            ok("M3 失败原因指向连接/认证", msg[:120].replace("\n", " "))
        else:
            bad("M3 失败原因指向连接/认证", msg[:200])

    # M4 第二道防线：估算失效（返回 0）时靠逐库余量检查兜底
    if not tiny:
        skip("M4 第二道防线", "无 tmpfs")
    else:
        out4 = os.path.join(tmp, "fullinst_second.tar.gz")
        orig_est = lf.estimate_instance_bytes
        old = os.environ.get("BP_WORK_DIR")
        os.environ["BP_WORK_DIR"] = tiny
        lf.estimate_instance_bytes = lambda *a, **k: 0
        try:
            lf.backup_full_instance(
                "mysql", host=host, port=port, user=user, password=pwd,
                dump_tool=DUMP_TOOL, query_tool=QUERY_TOOL, out_path=out4)
            bad("M4 第二道防线", "2MB 工作目录下不该备份成功")
        except Exception as e:
            msg = str(e)
            if ("提前终止" in msg) or ("磁盘空间不足" in msg) or lf.is_enospc(msg):
                ok("M4 第二道防线生效", msg[:120].replace("\n", " "))
            else:
                bad("M4 第二道防线生效", msg[:200])
        finally:
            lf.estimate_instance_bytes = orig_est
            if old is None:
                os.environ.pop("BP_WORK_DIR", None)
            else:
                os.environ["BP_WORK_DIR"] = old


# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mysql-host", default="127.0.0.1")
    ap.add_argument("--mysql-port", default="3399")
    ap.add_argument("--mysql-user", default="root")
    ap.add_argument("--mysql-pwd", default="")
    ap.add_argument("--tiny-dir", default="/tmp/e2e_tiny_2m",
                    help="极小文件系统挂载点（容器无 mount 权限时可 docker run --tmpfs "
                         "/bpwork:size=2m 后传 /bpwork）")
    args = ap.parse_args()

    tmp = tempfile.mkdtemp(prefix="e2e_v1411_")
    _orig_root = config.BACKUP_ROOT
    config.BACKUP_ROOT = tmp
    # 重删会写元数据库；E2E 只验证备份链路，避免污染真实库
    _orig_dedup = gd.dedup_file
    gd.dedup_file = lambda *a, **k: {"saved_bytes": 0}
    try:
        print("== 文件备份（问题 1）==")
        test_file_backups(tmp, args.tiny_dir)
        print("\n== MySQL 全实例备份（问题 2）==")
        test_mysql_full_instance(tmp, args.mysql_host, args.mysql_port,
                                 args.mysql_user, args.mysql_pwd, args.tiny_dir)
    finally:
        gd.dedup_file = _orig_dedup
        config.BACKUP_ROOT = _orig_root
        for p in (args.tiny_dir, os.path.join(tmp, "tiny")):
            if os.path.ismount(p):
                subprocess.run(["umount", p], capture_output=True)
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n==== 汇总: PASS %d / FAIL %d / SKIP %d ====" % (len(_PASS), len(_FAIL), len(_SKIP)))
    if _FAIL:
        print("失败项:", ", ".join(_FAIL))
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
