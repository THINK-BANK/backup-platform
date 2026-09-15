# 自定义脚本备份使用说明（全库 / 单表 / 全实例）

## 1. 能力概述

自定义备份（`backup_mode = custom`）允许用户用 **自己的 bash 脚本** 完成备份与恢复，
平台负责：脚本投递与执行、产物拉回、大小与 sha256 计算、备份记录、保留策略、
三级存储复制、定时调度、恢复编排。适用于国产库、专有导出工具、特殊归档流程等
平台内置引擎未覆盖的场景（也常用于"只导某几张表"的细粒度备份）。

三种**备份范围（scope）**：

| scope | 含义 | 典型用途 |
|---|---|---|
| `full_database` | 全库（单库内全部表，默认） | 整库备份，可恢复到任意目标库 |
| `single_table` | 单表 / 多表 | 大表单独备份、按业务拆分、快速恢复 |
| `full_instance` | 全实例（所有库） | 实例级搬迁、集群整体保护 |

## 2. 配置入口

任务编辑 → **自定义脚本** 区域：

1. **备份范围**：下拉选择 全库 / 单表·多表 / 全实例；
2. **表名**：单表/多表模式必填，逗号分隔；点「选择表」可从库中直接勾选
   （接口 `/api/tasks/{id}/list-tables`，原生驱动直连优先、JDBC 兜底）；
3. **备份脚本**：点「按范围填充脚本模板」一键生成（按数据库类型 + 范围），可再按需修改；
4. 恢复脚本同样支持模板填充；
5. 高级选项可配置 `tool_path`（客户端不在 PATH 时，如 `/opt/mysql840b/bin`）。

保存后 **预检查** 会拦截"单表模式未填表名"这类必错配置。

## 3. 执行通道

- **SSH 通道（默认）**：脚本经 SFTP 上传到数据库服务器执行（需纳管主机或任务级 SSH 凭据）；
- **本机通道（新增）**：任务的数据库地址就是平台本机（`127.0.0.1` / 本机 IP / 主机名）
  且未匹配到 SSH 主机时，脚本在平台本机 bash 执行，无需纳管自己。

两种方式注入的环境变量完全一致，脚本无需区分。

## 4. 平台注入的环境变量

备份脚本：

| 变量 | 说明 |
|---|---|
| `PLATFORM_DB_HOST/PORT/USER/PASSWORD/NAME` | 源库连接信息（密码仅走环境变量） |
| `PLATFORM_DB_TYPE` | 数据库类型（mysql/postgresql/oracle/...） |
| `PLATFORM_BACKUP_TYPE` / `PLATFORM_BACKUP_LEVEL` | 备份级别（full/incremental/differential） |
| `PLATFORM_BACKUP_SCOPE` | `full_database` / `single_table` / `full_instance` |
| `PLATFORM_TABLES` | 单表模式下的表名（逗号分隔，如 `t_order,t_user`） |
| `PLATFORM_BACKUP_DIR` | **产物必须写入该目录**，平台自动拉回 |
| `PLATFORM_TASK_ID` / `PLATFORM_TASK_NAME` | 任务标识 |

恢复脚本：

| 变量 | 说明 |
|---|---|
| `PLATFORM_BACKUP_FILE` | 备份文件路径（平台已推送到执行机） |
| `PLATFORM_RESTORE_DB` | 目标库名 |
| `PLATFORM_RESTORE_SCOPE` / `PLATFORM_TABLES` | 恢复范围与表名 |
| `PLATFORM_DB_HOST/PORT/USER/PASSWORD` | 目标连接信息 |

约定：脚本退出码 0 视为成功；非零时平台记录 stdout/stderr 片段到备份记录，便于排障。

## 5. 示例：MySQL 单表备份（模板节选）

```bash
export MYSQL_PWD="$PLATFORM_DB_PASSWORD"
OUT="$PLATFORM_BACKUP_DIR/${PLATFORM_DB_NAME}_tables_$(date +%Y%m%d_%H%M%S).sql.gz"
TABLES="${PLATFORM_TABLES//,/ }"
[ -z "$TABLES" ] && { echo "PLATFORM_TABLES 为空" >&2; exit 2; }
mysqldump --no-defaults -h "$PLATFORM_DB_HOST" -P "$PLATFORM_DB_PORT" -u "$PLATFORM_DB_USER" \
  --single-transaction --hex-blob --set-gtid-purged=OFF "$PLATFORM_DB_NAME" $TABLES | gzip -9 > "$OUT"
```

> 注意：全库备份**不要加 `--databases`**，否则产物自带 `CREATE DATABASE/USE 源库`，
> 恢复到其它目标库时数据会被写回源库。平台模板已按此约定输出。

## 6. 已验证场景（真实环境）

| 场景 | 通道 | 结果 |
|---|---|---|
| MySQL 全库备份 + 恢复到新库 | 本机 / SSH | 表结构与 3+2 行中文数据一致 |
| MySQL 单表（t_a）备份 + 恢复 | 本机 / SSH | 目标库仅 t_a，3 行一致 |
| MySQL 多表（t_a,t_b）备份 + 恢复 | 本机 | 2 张表 5 行一致 |
| MySQL 全实例备份 | 本机 | 产物含建库/建表/数据 |
| PostgreSQL 全库 / 单表备份 + 恢复 | 本机（tool_path=/pgdb/pgsql/bin） | 数据一致 |
| API 全链路（建任务→执行→恢复） | HTTP | 备份 success，恢复后目标库数据一致 |

## 7. 相关代码

- 模板库：`core/custom_scripts.py`
- 执行与产物回收：`core/engines/base.py`（`_backup_custom_remote` / `_backup_custom_local`
  / `_restore_custom_remote` / `_restore_custom_local`）
- 接口：`api/tasks.py`（`/api/custom-script/template`、`/api/tasks/{id}/list-tables`）
- 前端：`templates/tasks.html` + `static/js/app.js`
- 单元测试：`tests/test_custom_backup.py`
