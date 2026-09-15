# -*- coding: utf-8 -*-
"""自定义备份/恢复脚本模板库（按数据库类型 + 备份范围）。

背景
----
平台的「自定义脚本」备份通道（core/engines/base.py 的 run_backup/run_restore）
把用户脚本经 SSH 送到数据库服务器执行，平台注入 ``PLATFORM_*`` 环境变量并
把脚本产物拉回落盘（真实 size/sha256）。此前脚本需要用户从零编写，
缺少"全库 / 单表"的粒度约定，本模块补齐：

- **范围（scope）**：``full_instance``（全实例）/ ``full_database``（全库）
  / ``single_table``（单表或多表）；
- 平台注入 ``PLATFORM_BACKUP_SCOPE`` / ``PLATFORM_TABLES``，脚本据此拼命令；
- 模板全部使用**数据库自带客户端**（mysqldump / pg_dump / expdp / dexp /
  sqlcmd / mongodump 等），符合"被管服务器零安装"约束；
- 离线可用：不依赖任何在线下载。

所有模板均为**可执行样例**，用户可在界面上一键填充后按需修改。
"""

from __future__ import annotations

import re

# 备份范围
SCOPE_INSTANCE = "full_instance"   # 全实例（所有库）
SCOPE_DATABASE = "full_database"   # 全库（单库内所有对象）
SCOPE_TABLE = "single_table"       # 单表 / 多表

SCOPES = (SCOPE_INSTANCE, SCOPE_DATABASE, SCOPE_TABLE)

SCOPE_LABELS = {
    SCOPE_INSTANCE: "全实例（所有库）",
    SCOPE_DATABASE: "全库（单库全部表）",
    SCOPE_TABLE: "单表 / 多表",
}

# scope 别名（兼容用户手工填写 extra_options 或旧数据）
_SCOPE_ALIAS = {
    "full": SCOPE_DATABASE, "database": SCOPE_DATABASE, "db": SCOPE_DATABASE,
    "full_db": SCOPE_DATABASE, "full_database": SCOPE_DATABASE,
    "schema": SCOPE_DATABASE, "schemas": SCOPE_DATABASE,
    "instance": SCOPE_INSTANCE, "all": SCOPE_INSTANCE, "all_db": SCOPE_INSTANCE,
    "full_instance": SCOPE_INSTANCE, "cluster": SCOPE_INSTANCE,
    "table": SCOPE_TABLE, "tables": SCOPE_TABLE, "single_table": SCOPE_TABLE,
    "multi_table": SCOPE_TABLE,
}


def normalize_scope(scope: str, default: str = SCOPE_DATABASE) -> str:
    """把任意写法归一化为标准 scope；未知值回退默认。"""
    key = str(scope or "").strip().lower()
    return _SCOPE_ALIAS.get(key, default if default in SCOPES else SCOPE_DATABASE)


def parse_tables(raw: str) -> list:
    """解析界面填写的表名（逗号/空格/换行分隔，支持 db.table 写法）。"""
    return [t.strip() for t in re.split(r"[,\s]+", str(raw or "")) if t.strip()]


# ---------------------------------------------------------------- 模板正文
_HDR = """#!/bin/bash
# 平台自定义备份脚本（{scope_label}）
# 平台注入：PLATFORM_DB_HOST/PORT/USER/PASSWORD/NAME、PLATFORM_DB_TYPE、
#           PLATFORM_BACKUP_SCOPE、PLATFORM_TABLES、PLATFORM_BACKUP_LEVEL、
#           PLATFORM_BACKUP_DIR（产物必须写入该目录）、PLATFORM_TASK_ID/NAME
# 约定：退出码 0 视为成功；产物写入 $PLATFORM_BACKUP_DIR。
set -e
"""

_MYSQL_BACKUP = {
    SCOPE_INSTANCE: r'''
export MYSQL_PWD="$PLATFORM_DB_PASSWORD"
OUT="$PLATFORM_BACKUP_DIR/all_databases_$(date +%Y%m%d_%H%M%S).sql.gz"
# 全实例：导出全部库（排除系统库 information_schema/performance_schema/sys）
DBS=$(mysql --no-defaults -h "$PLATFORM_DB_HOST" -P "$PLATFORM_DB_PORT" \
      -u "$PLATFORM_DB_USER" -N -e "SELECT schema_name FROM information_schema.schemata \
      WHERE schema_name NOT IN ('information_schema','performance_schema','sys','mysql')")
mysqldump --no-defaults -h "$PLATFORM_DB_HOST" -P "$PLATFORM_DB_PORT" -u "$PLATFORM_DB_USER" \
  --single-transaction --routines --triggers --events --hex-blob \
  --default-character-set=utf8mb4 --databases $DBS | gzip -9 > "$OUT"
echo "backup done: $OUT"
''',
    SCOPE_DATABASE: r'''
export MYSQL_PWD="$PLATFORM_DB_PASSWORD"
OUT="$PLATFORM_BACKUP_DIR/${PLATFORM_DB_NAME}_$(date +%Y%m%d_%H%M%S).sql.gz"
# 注意：全库备份不加 --databases —— 否则产物自带 CREATE DATABASE/USE 源库，
# 恢复到其它目标库时数据会被写回源库。不带该参数时导出的是"库内全部表"，
# 可恢复到任意目标库（恢复脚本负责 CREATE DATABASE）。
mysqldump --no-defaults -h "$PLATFORM_DB_HOST" -P "$PLATFORM_DB_PORT" -u "$PLATFORM_DB_USER" \
  --single-transaction --routines --triggers --events --hex-blob \
  --default-character-set=utf8mb4 --set-gtid-purged=OFF "$PLATFORM_DB_NAME" | gzip -9 > "$OUT"
echo "backup done: $OUT"
''',
    SCOPE_TABLE: r'''
export MYSQL_PWD="$PLATFORM_DB_PASSWORD"
OUT="$PLATFORM_BACKUP_DIR/${PLATFORM_DB_NAME}_tables_$(date +%Y%m%d_%H%M%S).sql.gz"
# 单表/多表：PLATFORM_TABLES 为逗号分隔表名，支持 db.table 写法
TABLES="${PLATFORM_TABLES//,/ }"
if [ -z "$TABLES" ]; then echo "PLATFORM_TABLES 为空，无法确定要备份的表" >&2; exit 2; fi
mysqldump --no-defaults -h "$PLATFORM_DB_HOST" -P "$PLATFORM_DB_PORT" -u "$PLATFORM_DB_USER" \
  --single-transaction --hex-blob --default-character-set=utf8mb4 \
  --set-gtid-purged=OFF "$PLATFORM_DB_NAME" $TABLES | gzip -9 > "$OUT"
echo "backup done: $OUT (tables: $TABLES)"
''',
}

_MYSQL_RESTORE = r'''#!/bin/bash
# 平台自定义恢复脚本（MySQL）
# 平台注入：PLATFORM_BACKUP_FILE（备份文件路径）、PLATFORM_RESTORE_DB（目标库名）、
#           PLATFORM_DB_HOST/PORT/USER/PASSWORD、PLATFORM_RESTORE_SCOPE、PLATFORM_TABLES
set -e
export MYSQL_PWD="$PLATFORM_DB_PASSWORD"
MYSQL="mysql --no-defaults -h $PLATFORM_DB_HOST -P $PLATFORM_DB_PORT -u $PLATFORM_DB_USER"
if [ "${PLATFORM_RESTORE_SCOPE:-full_database}" = "full_instance" ]; then
  # 全实例产物自带 CREATE DATABASE / USE，直接整体回放（不指定目标库）
  if gzip -t "$PLATFORM_BACKUP_FILE" 2>/dev/null; then
    gunzip -c "$PLATFORM_BACKUP_FILE" | $MYSQL
  else
    $MYSQL < "$PLATFORM_BACKUP_FILE"
  fi
  echo "restore done(full instance): $PLATFORM_BACKUP_FILE"
  exit 0
fi
[ -n "$PLATFORM_RESTORE_DB" ] || { echo "PLATFORM_RESTORE_DB 为空，无法确定目标库" >&2; exit 2; }
# 目标库不存在时自动创建（全库/单表恢复都必须先有目标库）
$MYSQL -e "CREATE DATABASE IF NOT EXISTS \`$PLATFORM_RESTORE_DB\` DEFAULT CHARSET utf8mb4"
if gzip -t "$PLATFORM_BACKUP_FILE" 2>/dev/null; then
  gunzip -c "$PLATFORM_BACKUP_FILE" | $MYSQL "$PLATFORM_RESTORE_DB"
else
  $MYSQL "$PLATFORM_RESTORE_DB" < "$PLATFORM_BACKUP_FILE"
fi
echo "restore done: $PLATFORM_BACKUP_FILE -> $PLATFORM_RESTORE_DB"
'''

_PG_BACKUP = {
    SCOPE_INSTANCE: r'''
export PGPASSWORD="$PLATFORM_DB_PASSWORD"
OUT="$PLATFORM_BACKUP_DIR/all_databases_$(date +%Y%m%d_%H%M%S).sql.gz"
pg_dumpall -h "$PLATFORM_DB_HOST" -p "$PLATFORM_DB_PORT" -U "$PLATFORM_DB_USER" | gzip -9 > "$OUT"
echo "backup done: $OUT"
''',
    SCOPE_DATABASE: r'''
export PGPASSWORD="$PLATFORM_DB_PASSWORD"
OUT="$PLATFORM_BACKUP_DIR/${PLATFORM_DB_NAME}_$(date +%Y%m%d_%H%M%S).dump"
pg_dump -h "$PLATFORM_DB_HOST" -p "$PLATFORM_DB_PORT" -U "$PLATFORM_DB_USER" \
  -Fc -f "$OUT" -d "$PLATFORM_DB_NAME"
echo "backup done: $OUT"
''',
    SCOPE_TABLE: r'''
export PGPASSWORD="$PLATFORM_DB_PASSWORD"
OUT="$PLATFORM_BACKUP_DIR/${PLATFORM_DB_NAME}_tables_$(date +%Y%m%d_%H%M%S).dump"
TARGS=""
for T in ${PLATFORM_TABLES//,/ }; do TARGS="$TARGS -t $T"; done
if [ -z "$TARGS" ]; then echo "PLATFORM_TABLES 为空，无法确定要备份的表" >&2; exit 2; fi
pg_dump -h "$PLATFORM_DB_HOST" -p "$PLATFORM_DB_PORT" -U "$PLATFORM_DB_USER" \
  -Fc -f "$OUT" -d "$PLATFORM_DB_NAME" $TARGS
echo "backup done: $OUT (tables: $PLATFORM_TABLES)"
''',
}

_PG_RESTORE = r'''#!/bin/bash
# 平台自定义恢复脚本（PostgreSQL / Kingbase）
set -e
export PGPASSWORD="$PLATFORM_DB_PASSWORD"
PSQL="psql -h $PLATFORM_DB_HOST -p $PLATFORM_DB_PORT -U $PLATFORM_DB_USER"
if [ "${PLATFORM_RESTORE_SCOPE:-full_database}" = "full_instance" ]; then
  # pg_dumpall 产物：整体回放到集群（不指定目标库）
  if gzip -t "$PLATFORM_BACKUP_FILE" 2>/dev/null; then
    gunzip -c "$PLATFORM_BACKUP_FILE" | $PSQL -d postgres
  else
    $PSQL -d postgres -f "$PLATFORM_BACKUP_FILE"
  fi
  echo "restore done(full instance): $PLATFORM_BACKUP_FILE"
  exit 0
fi
[ -n "$PLATFORM_RESTORE_DB" ] || { echo "PLATFORM_RESTORE_DB 为空，无法确定目标库" >&2; exit 2; }
$PSQL -d postgres -tc "SELECT 1 FROM pg_database WHERE datname='$PLATFORM_RESTORE_DB'" \
  | grep -q 1 || $PSQL -d postgres -c "CREATE DATABASE \"$PLATFORM_RESTORE_DB\""
if pg_restore --version >/dev/null 2>&1 && head -c5 "$PLATFORM_BACKUP_FILE" | grep -q PGDMP; then
  pg_restore -h "$PLATFORM_DB_HOST" -p "$PLATFORM_DB_PORT" -U "$PLATFORM_DB_USER" \
    -d "$PLATFORM_RESTORE_DB" --no-owner "$PLATFORM_BACKUP_FILE"
else
  if gzip -t "$PLATFORM_BACKUP_FILE" 2>/dev/null; then
    gunzip -c "$PLATFORM_BACKUP_FILE" | $PSQL -d "$PLATFORM_RESTORE_DB"
  else
    $PSQL -d "$PLATFORM_RESTORE_DB" -f "$PLATFORM_BACKUP_FILE"
  fi
fi
echo "restore done: $PLATFORM_BACKUP_FILE -> $PLATFORM_RESTORE_DB"
'''

_ORACLE_BACKUP = {
    SCOPE_INSTANCE: r'''
OUT="$PLATFORM_BACKUP_DIR/full_$(date +%Y%m%d_%H%M%S).dmp"
expdp "$PLATFORM_DB_USER/$PLATFORM_DB_PASSWORD@$PLATFORM_DB_HOST:$PLATFORM_DB_PORT/$PLATFORM_DB_NAME" \
  FULL=Y DIRECTORY=DATA_PUMP_DIR DUMPFILE=$(basename $OUT) LOGFILE=expdp_full.log
cp "$(dirname $OUT)/$(basename $OUT)" "$OUT" 2>/dev/null || true
echo "backup done: $OUT"
''',
    SCOPE_DATABASE: r'''
OUT="$PLATFORM_BACKUP_DIR/${PLATFORM_DB_NAME}_$(date +%Y%m%d_%H%M%S).dmp"
expdp "$PLATFORM_DB_USER/$PLATFORM_DB_PASSWORD@$PLATFORM_DB_HOST:$PLATFORM_DB_PORT/$PLATFORM_DB_NAME" \
  SCHEMAS=$PLATFORM_DB_NAME DIRECTORY=DATA_PUMP_DIR DUMPFILE=$(basename $OUT) LOGFILE=expdp_schema.log
echo "backup done: $OUT"
''',
    SCOPE_TABLE: r'''
OUT="$PLATFORM_BACKUP_DIR/${PLATFORM_DB_NAME}_tables_$(date +%Y%m%d_%H%M%S).dmp"
[ -z "$PLATFORM_TABLES" ] && { echo "PLATFORM_TABLES 为空" >&2; exit 2; }
expdp "$PLATFORM_DB_USER/$PLATFORM_DB_PASSWORD@$PLATFORM_DB_HOST:$PLATFORM_DB_PORT/$PLATFORM_DB_NAME" \
  TABLES=$PLATFORM_TABLES DIRECTORY=DATA_PUMP_DIR DUMPFILE=$(basename $OUT) LOGFILE=expdp_tables.log
echo "backup done: $OUT"
''',
}

_ORACLE_RESTORE = r'''#!/bin/bash
# 平台自定义恢复脚本（Oracle Data Pump）
set -e
impdp "$PLATFORM_DB_USER/$PLATFORM_DB_PASSWORD@$PLATFORM_DB_HOST:$PLATFORM_DB_PORT/$PLATFORM_RESTORE_DB" \
  DIRECTORY=DATA_PUMP_DIR DUMPFILE=$(basename $PLATFORM_BACKUP_FILE) \
  REMAP_SCHEMA=$PLATFORM_DB_NAME:$PLATFORM_RESTORE_DB TABLE_EXISTS_ACTION=REPLACE
echo "restore done: $PLATFORM_BACKUP_FILE -> $PLATFORM_RESTORE_DB"
'''

_DM_BACKUP = {
    SCOPE_INSTANCE: r'''
OUT="$PLATFORM_BACKUP_DIR/full_$(date +%Y%m%d_%H%M%S).dmp"
dexp "SYSDBA/$PLATFORM_DB_PASSWORD@$PLATFORM_DB_HOST:$PLATFORM_DB_PORT" \
  FULL=Y FILE=$(basename $OUT) LOG=dexp_full.log DIRECTORY=$(dirname $OUT)
echo "backup done: $OUT"
''',
    SCOPE_DATABASE: r'''
OUT="$PLATFORM_BACKUP_DIR/${PLATFORM_DB_NAME}_$(date +%Y%m%d_%H%M%S).dmp"
dexp "$PLATFORM_DB_USER/$PLATFORM_DB_PASSWORD@$PLATFORM_DB_HOST:$PLATFORM_DB_PORT" \
  SCHEMAS=$PLATFORM_DB_NAME FILE=$(basename $OUT) LOG=dexp_schema.log DIRECTORY=$(dirname $OUT)
echo "backup done: $OUT"
''',
    SCOPE_TABLE: r'''
OUT="$PLATFORM_BACKUP_DIR/${PLATFORM_DB_NAME}_tables_$(date +%Y%m%d_%H%M%S).dmp"
[ -z "$PLATFORM_TABLES" ] && { echo "PLATFORM_TABLES 为空" >&2; exit 2; }
dexp "$PLATFORM_DB_USER/$PLATFORM_DB_PASSWORD@$PLATFORM_DB_HOST:$PLATFORM_DB_PORT" \
  TABLES=$PLATFORM_TABLES FILE=$(basename $OUT) LOG=dexp_tables.log DIRECTORY=$(dirname $OUT)
echo "backup done: $OUT"
''',
}

_DM_RESTORE = r'''#!/bin/bash
# 平台自定义恢复脚本（达梦 dexp/dimp）
set -e
dimp "$PLATFORM_DB_USER/$PLATFORM_DB_PASSWORD@$PLATFORM_DB_HOST:$PLATFORM_DB_PORT" \
  FILE=$PLATFORM_BACKUP_FILE LOG=dimp_restore.log TABLE_EXISTS_ACTION=REPLACE
echo "restore done: $PLATFORM_BACKUP_FILE"
'''

_SQLSERVER_BACKUP = {
    SCOPE_INSTANCE: r'''
OUT="$PLATFORM_BACKUP_DIR/all_databases_$(date +%Y%m%d_%H%M%S).bak"
export SQLCMDPASSWORD="$PLATFORM_DB_PASSWORD"
sqlcmd -S "$PLATFORM_DB_HOST,$PLATFORM_DB_PORT" -U "$PLATFORM_DB_USER" -b -Q \
  "EXEC sp_MSforeachdb 'IF DB_ID(''?'')>4 BACKUP DATABASE [?] TO DISK=''$OUT'' WITH INIT,COMPRESSION,CHECKSUM'"
echo "backup done: $OUT"
''',
    SCOPE_DATABASE: r'''
OUT="$PLATFORM_BACKUP_DIR/${PLATFORM_DB_NAME}_$(date +%Y%m%d_%H%M%S).bak"
export SQLCMDPASSWORD="$PLATFORM_DB_PASSWORD"
sqlcmd -S "$PLATFORM_DB_HOST,$PLATFORM_DB_PORT" -U "$PLATFORM_DB_USER" -b -Q \
  "BACKUP DATABASE [$PLATFORM_DB_NAME] TO DISK=N'$OUT' WITH INIT,COMPRESSION,CHECKSUM,STATS=10"
echo "backup done: $OUT"
''',
    SCOPE_TABLE: r'''
[ -z "$PLATFORM_TABLES" ] && { echo "PLATFORM_TABLES 为空" >&2; exit 2; }
OUT="$PLATFORM_BACKUP_DIR/${PLATFORM_DB_NAME}_tables_$(date +%Y%m%d_%H%M%S)"
mkdir -p "$OUT"
export SQLCMDPASSWORD="$PLATFORM_DB_PASSWORD"
for T in ${PLATFORM_TABLES//,/ }; do
  bcp "$PLATFORM_DB_NAME.dbo.$T" out "$OUT/$T.bcp" -S "$PLATFORM_DB_HOST,$PLATFORM_DB_PORT" \
      -U "$PLATFORM_DB_USER" -n -q
done
tar -czf "$OUT.tar.gz" -C "$(dirname $OUT)" "$(basename $OUT)"
rm -rf "$OUT"
echo "backup done: $OUT.tar.gz"
''',
}

_SQLSERVER_RESTORE = r'''#!/bin/bash
# 平台自定义恢复脚本（SQL Server）
set -e
export SQLCMDPASSWORD="$PLATFORM_DB_PASSWORD"
sqlcmd -S "$PLATFORM_DB_HOST,$PLATFORM_DB_PORT" -U "$PLATFORM_DB_USER" -b -Q \
  "RESTORE DATABASE [$PLATFORM_RESTORE_DB] FROM DISK=N'$PLATFORM_BACKUP_FILE' WITH REPLACE,RECOVERY"
echo "restore done: $PLATFORM_RESTORE_DB"
'''

_MONGO_BACKUP = {
    SCOPE_INSTANCE: r'''
OUT="$PLATFORM_BACKUP_DIR/mongodump_all_$(date +%Y%m%d_%H%M%S).gz"
mongodump --host "$PLATFORM_DB_HOST:$PLATFORM_DB_PORT" -u "$PLATFORM_DB_USER" \
  -p "$PLATFORM_DB_PASSWORD" --authenticationDatabase admin --archive --gzip > "$OUT"
echo "backup done: $OUT"
''',
    SCOPE_DATABASE: r'''
OUT="$PLATFORM_BACKUP_DIR/${PLATFORM_DB_NAME}_$(date +%Y%m%d_%H%M%S).gz"
mongodump --host "$PLATFORM_DB_HOST:$PLATFORM_DB_PORT" -u "$PLATFORM_DB_USER" \
  -p "$PLATFORM_DB_PASSWORD" --authenticationDatabase admin --db "$PLATFORM_DB_NAME" \
  --archive --gzip > "$OUT"
echo "backup done: $OUT"
''',
    SCOPE_TABLE: r'''
[ -z "$PLATFORM_TABLES" ] && { echo "PLATFORM_TABLES 为空" >&2; exit 2; }
OUT="$PLATFORM_BACKUP_DIR/${PLATFORM_DB_NAME}_collections_$(date +%Y%m%d_%H%M%S).gz"
CARG=""
for C in ${PLATFORM_TABLES//,/ }; do CARG="$CARG --collection $C"; done
mongodump --host "$PLATFORM_DB_HOST:$PLATFORM_DB_PORT" -u "$PLATFORM_DB_USER" \
  -p "$PLATFORM_DB_PASSWORD" --authenticationDatabase admin --db "$PLATFORM_DB_NAME" \
  $CARG --archive --gzip > "$OUT"
echo "backup done: $OUT"
''',
}

_MONGO_RESTORE = r'''#!/bin/bash
# 平台自定义恢复脚本（MongoDB）
set -e
mongorestore --host "$PLATFORM_DB_HOST:$PLATFORM_DB_PORT" -u "$PLATFORM_DB_USER" \
  -p "$PLATFORM_DB_PASSWORD" --authenticationDatabase admin \
  --nsFrom "$PLATFORM_DB_NAME.*" --nsTo "$PLATFORM_RESTORE_DB.*" \
  --archive --gzip < "$PLATFORM_BACKUP_FILE"
echo "restore done: $PLATFORM_RESTORE_DB"
'''

# 通用兜底：数据库自带客户端未知时，给出带范围分支的骨架脚本
_GENERIC_BACKUP = {
    SCOPE_INSTANCE: r'''
OUT="$PLATFORM_BACKUP_DIR/full_$(date +%Y%m%d_%H%M%S).bak"
echo "请在此调用数据库自带客户端导出全实例，产物写入: $OUT" >&2
# 示例（按实际数据库替换）：your_dump_tool --host $PLATFORM_DB_HOST --all > "$OUT"
touch "$OUT"
''',
    SCOPE_DATABASE: r'''
OUT="$PLATFORM_BACKUP_DIR/${PLATFORM_DB_NAME}_$(date +%Y%m%d_%H%M%S).bak"
echo "请在此调用数据库自带客户端导出库 $PLATFORM_DB_NAME，产物写入: $OUT" >&2
touch "$OUT"
''',
    SCOPE_TABLE: r'''
OUT="$PLATFORM_BACKUP_DIR/${PLATFORM_DB_NAME}_tables_$(date +%Y%m%d_%H%M%S).bak"
[ -z "$PLATFORM_TABLES" ] && { echo "PLATFORM_TABLES 为空" >&2; exit 2; }
echo "请在此导出表 $PLATFORM_TABLES，产物写入: $OUT" >&2
touch "$OUT"
''',
}

_GENERIC_RESTORE = r'''#!/bin/bash
# 平台自定义恢复脚本（通用骨架）
set -e
echo "待恢复文件: $PLATFORM_BACKUP_FILE"
echo "目标库: $PLATFORM_RESTORE_DB  范围: $PLATFORM_RESTORE_SCOPE  表: ${PLATFORM_TABLES:-（全部）}"
# 示例（按实际数据库替换）：your_restore_tool --file $PLATFORM_BACKUP_FILE --db $PLATFORM_RESTORE_DB
echo "请在此调用数据库自带客户端完成恢复" >&2
'''

# db_type → (备份模板 dict, 恢复脚本)
_TEMPLATES = {
    "mysql": (_MYSQL_BACKUP, _MYSQL_RESTORE),
    "mariadb": (_MYSQL_BACKUP, _MYSQL_RESTORE),
    "postgresql": (_PG_BACKUP, _PG_RESTORE),
    "kingbase": (_PG_BACKUP, _PG_RESTORE),
    "oracle": (_ORACLE_BACKUP, _ORACLE_RESTORE),
    "dameng": (_DM_BACKUP, _DM_RESTORE),
    "dm": (_DM_BACKUP, _DM_RESTORE),
    "sqlserver": (_SQLSERVER_BACKUP, _SQLSERVER_RESTORE),
    "mongodb": (_MONGO_BACKUP, _MONGO_RESTORE),
}

# 支持「单表」粒度的类型（用于前端提示与预检查）
SUPPORTS_TABLE_SCOPE = tuple(sorted(_TEMPLATES.keys()))


def backup_script(db_type: str, scope: str = SCOPE_DATABASE) -> str:
    """生成备份脚本模板（bash 全文）。"""
    scope = normalize_scope(scope)
    pair = _TEMPLATES.get(str(db_type or "").lower())
    body_map = pair[0] if pair else _GENERIC_BACKUP
    body = body_map.get(scope) or body_map[SCOPE_DATABASE]
    return _HDR.format(scope_label=SCOPE_LABELS.get(scope, scope)) + body


def restore_script(db_type: str, scope: str = SCOPE_DATABASE) -> str:
    """生成恢复脚本模板（bash 全文）。"""
    scope = normalize_scope(scope)
    pair = _TEMPLATES.get(str(db_type or "").lower())
    return pair[1] if pair else _GENERIC_RESTORE


def template(db_type: str, scope: str = SCOPE_DATABASE) -> dict:
    """返回 {"backup": ..., "restore": ..., "scope": ...}。"""
    scope = normalize_scope(scope)
    return {
        "db_type": db_type,
        "scope": scope,
        "scope_label": SCOPE_LABELS.get(scope, scope),
        "backup": backup_script(db_type, scope),
        "restore": restore_script(db_type, scope),
    }
