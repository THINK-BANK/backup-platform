# -*- coding: utf-8 -*-
"""目标端通道声明（文档 docs/agentless_architecture_20260919.md §2、§3 的 P0 落点）。

这里是**声明层**：把"AIDBM 到目标端一共走几条通道、每条侵入到什么程度"写成机器
可读的数据，供 API 面板、任务详情与审计脚本共用。

注意：本模块的值目前是**人工声明**，必须与实现保持一致；P1 会改为由能力模板
（core/agentless/templates/*.yml）自动生成。声明错了就等于对外发布的口径错了，
因此任何实现变动都必须同步这里（并有 tests/test_agentless_audit.py 兜底结构）。
"""
from __future__ import annotations

#: 侵入等级：A0 最轻，X 是被明令禁止的红线
INVASION_LEVELS = {
    "A0": "零安装、零文件、零改配：只用账号 + 标准协议（DB 原生协议/JDBC/REST）",
    "A1": "零安装、零平台文件：调用目标端自带客户端（mysqldump/pg_dump/expdp/rman 等）",
    "A2": "执行期临时文件落地，用完即删（xtrabackup/mariabackup 临时推送、恢复临时解包）",
    "A3": "需客户侧开启配置（非安装软件）：log-bin/wal_level/ARCHIVELOG/RLOG_APPEND 等",
    "X": "禁止：目标端常驻进程、开机自启、crontab、systemd unit、常驻监听端口",
}

#: 平台到目标端的通道清单（impl 为当前实现位置，便于核对声明是否过期）
CHANNELS = [
    {
        "id": "ssh",
        "name": "SSH / SFTP（paramiko）",
        "invasiveness": "A1",
        "impl": "core/remote_dump.py:_connect / remote_exec_capture",
        "requires_install": False,
        "note": "备份/恢复/预检的主通道；只执行目标端自带的命令",
    },
    {
        "id": "native-protocol",
        "name": "数据库原生协议直连",
        "invasiveness": "A0",
        "impl": "core/native_conn.py（pymysql / psycopg2 / oracledb / cx_Oracle / dmPython）",
        "requires_install": False,
        "note": "连接测试、拉库列表、数据同步、数据对比读端口",
    },
    {
        "id": "jdbc",
        "name": "JDBC 桥接（平台侧 JVM）",
        "invasiveness": "A0",
        "impl": "core/jdbc.py + drivers/*.jar",
        "requires_install": False,
        "note": "JVM 与驱动运行在平台侧，目标端零感知；缺原生驱动时的兜底",
    },
    {
        "id": "rest",
        "name": "HTTP/REST 控制面",
        "invasiveness": "A0",
        "impl": "core/vm/providers/pve.py（PVE /api2/json）"
                "；core/objectstore/client.py（S3 兼容 REST）",
        "requires_install": False,
        "note": "虚拟化控制面 / 对象存储桶；VMware/Hyper-V 等无控制面的走 SSH",
    },
    {
        "id": "temp-binary",
        "name": "执行期临时二进制（BYO-Binary）",
        "invasiveness": "A2",
        "impl": "core/engines/mysql.py:619-789（推送 → 备份 → 清理）",
        "requires_install": False,
        "note": "仅在无对应客户端时才用；必须清理并可由 audit 取证",
    },
]

#: A3 类：需要客户侧开启的目标端配置（非安装软件）。
#: 值来自当前 CDC 实现的检测逻辑所在文件，便于核对。
TARGET_CONFIG_REQUIREMENTS = {
    "mysql": [{"item": "log_bin=ON 且 binlog_format=ROW",
               "checked_in": "core/cdc/mysql_binlog.py"}],
    "mariadb": [{"item": "log_bin=ON 且 binlog_format=ROW",
                 "checked_in": "core/cdc/mysql_binlog.py"}],
    "postgresql": [{"item": "wal_level=replica/archive + archive_command",
                    "checked_in": "core/cdc/pg_wal.py"}],
    "kingbase": [{"item": "WAL 归档开启 + sys_hba.conf 复制授权（scram-sha-256）",
                  "checked_in": "core/cdc/kingbase_wal.py"}],
    "oracle": [{"item": "ARCHIVELOG 模式",
                "checked_in": "core/cdc/oracle_logminer.py"}],
    "dameng": [{"item": "归档开启 + RLOG_APPEND_LOGIC 开启",
                "checked_in": "core/cdc/dameng_logmnr.py"}],
}

#: 红线：任何流程都不允许出现
FORBIDDEN = [
    "目标端常驻进程",
    "目标端开机自启 / systemd unit",
    "目标端 crontab 计划任务",
    "目标端常驻监听端口",
    "写入目标端系统目录",
]


def channels() -> list[dict]:
    """返回通道声明（浅拷贝，避免调用方改动全局）。"""
    return [dict(c) for c in CHANNELS]


def invasion_levels() -> dict[str, str]:
    return dict(INVASION_LEVELS)


def invasiveness_of(channel_id: str) -> str | None:
    for c in CHANNELS:
        if c["id"] == channel_id:
            return c["invasiveness"]
    return None


def target_requirements(db_type: str) -> list[dict]:
    """某类数据库的客户侧前置配置要求（A3）；未知类型返回空列表。"""
    return [dict(x) for x in TARGET_CONFIG_REQUIREMENTS.get((db_type or "").lower(), [])]


def capabilities() -> dict:
    """对外总览：通道 + 等级 + 红线 + 各库前置要求。"""
    per_channel = []
    for c in channels():
        item = dict(c)
        item["level_desc"] = INVASION_LEVELS.get(c["invasiveness"], "")
        per_channel.append(item)
    return {
        "invasion_levels": invasion_levels(),
        "channels": per_channel,
        "forbidden": list(FORBIDDEN),
        "target_requirements": {k: target_requirements(k)
                               for k in TARGET_CONFIG_REQUIREMENTS},
        "note": "AIDBM 不在被保护对象上安装软件、不部署常驻 Agent；A2 场景为执行期"
                "临时推送且用完即删，可用 GET /api/v1/targets/<id>/no-agent-audit 取证。",
    }
