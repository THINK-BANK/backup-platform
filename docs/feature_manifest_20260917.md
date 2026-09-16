# AIDBM 产品功能固化清单（v1.4.9）

> **本清单是产品的功能全集快照，面向「完全离线部署、上线后不再改代码」的场景。**
> 所有条目均通过回查代码统计得出（盘点日期 2026-09-17，基线 commit `535f6f2`，tag `v1.4.9`），
> 不写「应该有」的功能，只写代码里**确实存在**的能力。
>
> 配套文档：
> - [离线交付与运维手册](offline_ops_manual_20260917.md)（上线、排障、应急调整）
> - [产品设计说明书](product_design_spec_20260916.md)（领域模型、机制、产品化差距清单 G1–G13）
> - [设计思想](design_philosophy_20260916.md)（11 条原则与优先级裁决）

---

## 0. 怎么用这份清单

| 你想知道 | 看这里 |
|---|---|
| 某个功能在哪个页面、哪个接口、哪个文件 | §3 能力域详解 |
| 有多少个页面 / 接口 / 表 | §4 / §5 / §6 |
| 上线后不改代码能调什么 | §7、**§8（最重要）** |
| 哪些能力还没做完、不能对客户承诺 | §9 |
| 上线后坏了怎么查 | [离线交付与运维手册](offline_ops_manual_20260917.md) |

**盘点基线数据（回查统计，非估算）**：Web 路由 **332** 条、页面模板 **27** 个（另有 3 个内联片段）、
备份引擎 **11** 类 + 文件备份、同步插件 **5** 类、元数据表 **46** 张、
`config.py` 配置项 **69** 个、环境变量读取点 **70** 个。

---

## 1. 产品形态

| 项 | 结论 |
|---|---|
| 产品名称 | AIDBM（AI 原生智能数据库灾备管理平台 / AI-Native Database Backup & DR Management） |
| 技术栈 | Python 3 + Flask（Jinja2 模板 + Bootstrap 5 + 原生 JS，复杂页用 Preact+htm 免构建局部增强） |
| 元数据库 | SQLite（默认，路径可改）；**不支持 HA/横向扩展**（已知最大架构短板，见 §9） |
| 调度 | 进程内 APScheduler + RT 守护进程（`core/rt_backup/supervisor.py`；锁文件 `INSTANCE_DIR/rt_supervisor.lock`，flock 保证单实例） |
| 交付形态 | ① Docker 镜像 `ghcr.io/zhh9126/backup-platform`；② 离线交付包（脚本安装）；③ PyInstaller 打包（`.spec` 已存在） |
| 连接通道 | **统一 SSH(paramiko) + SFTP**；少量只读元数据走原生 Python 驱动（MySQL/MariaDB=pymysql、PG/金仓=psycopg2、Oracle=oracledb 瘦客户端、达梦=dmPython），JDBC 为可选兜底 |
| 客户端要求 | **零安装、零 agent**：被管数据库服务器不装任何备份工具；物理备份由平台临时推送二进制到远端 `/tmp`，用完清理 |
| 网络要求 | 完全离线可用：不依赖公网 PyPI / DockerHub / NTP / AI API |

---

## 2. 能力域总览

| # | 能力域 | 入口 | 实现主模块 | 状态 |
|---|---|---|---|---|
| 1 | 资产纳管（主机/实例） | `/operations`、`/hosts` | `core/ssh_hosts.py`、`api/hosts.py` | 已验证 |
| 2 | 备份（11 类引擎 + 文件） | `/tasks`、`/protection` | `core/engines/*`、`core/logical_full.py` | 见 §3.2 矩阵 |
| 3 | 恢复 + 跨主机恢复 | `/restore`、`/restore_records` | `core/restore.py`、`core/cross_host.py` | 已验证 |
| 4 | 恢复校验（深度校验） | `/restore_verify` | `core/restore_verify.py` | 已验证 |
| 5 | 克隆服务（VDB 直通） | `/clone`、`/vdb` | `core/clone_service.py`、`api/clone.py` | 已验证 |
| 6 | 实时保护（PITR 准 CDP） | `/realtime` | `core/rt_backup/`（`db_rt.py` 数据库实时、`file_rt.py` 文件实时、`pitr.py` 时间点恢复、`supervisor.py` 守护）+ `core/rt/`（`journal.py`、`log_repo.py`） | 已验证 |
| 7 | CDC 变更捕获回放 | `/realtime`（标签页） | `core/cdc/`：`mysql_binlog.py`、`pg_wal.py`、`oracle_logminer.py`、`kingbase_wal.py`、`dameng_logmnr.py`、`rowlevel.py`、`polling_base.py` | 已验证；**存在仿真降级通道，见 §3.7** |
| 8 | 数据同步 | `/sync` | `core/sync/` | **v1.4.9 九链路已验证** |
| 9 | 数据迁移（DTS 式） | `/migration` | `core/db_migrate.py`、`api/migration.py` | **v1.4.9 四链路已验证** |
| 10 | 数据对比 / 挖掘 / 脱敏导出 | `/data_compare`、`/datamining` | `core/data_compare.py`、`core/data_mining.py`、`core/sensitive_scan.py` | 已验证 |
| 11 | 存储分层 / 磁带 / 去重 / 生命周期 | `/storage`、`/lifecycle` | `core/storage.py`、`core/tier_replication.py`、`core/global_dedup.py`、`core/lifecycle.py`、`core/retention_gfs.py`、`core/tape.py` | 部分见 §9 |
| 12 | 恢复演练（SureBackup 式） | `/drills` | `core/drill.py` | 已验证 |
| 13 | 告警 / AI 预测 / 灾难联动 | `/alert`、`/drlink` | `core/ai_alert.py`、`core/disaster_link.py` | 部分见 §9 |
| 14 | 虚拟机保护（PVE/KVM/ESXi/Hyper-V） | `/vm` | `core/vm/` | **未见真实环境验证** |
| 15 | 运维管理（RBAC/令牌/日志/巡检/报告/通知/插件） | `/users`、`/logs`、`/settings`、`/inspection`、`/plugins` 等 | `core/rbac.py`、`api/logs.py`、`core/inspection.py`、`core/reports.py`、`core/notifier.py`、`core/plugin_*` | 已验证 |
| 16 | AI 助手（可选） | `/agent` | `core/ai_agent/`、`skills/` | 需外部 LLM，离线默认关闭 |
| 17 | 自定义备份脚本 | 任务高级选项 | `core/custom_scripts.py` | 已验证 |

---

## 3. 能力域详解

### 3.2 备份引擎能力矩阵（**核对别承诺错的关键表**）

| 引擎 | 代码位置 | 通道 | 真实验证状态 |
|---|---|---|---|
| MySQL / MariaDB | `core/engines/mysql.py` `mariadb.py` | 逻辑备份（单库/多库/全实例逐库打包）、物理备份（xtrabackup 8.0.35 / 2.4.29、mariabackup，平台侧推送免安装）、binlog PITR | ✅ 5.7.44 / 8.0.40 / MariaDB 10.11.19 实跑 |
| PostgreSQL | `core/engines/postgresql.py` | `pg_dump` / `pg_dumpall` / `pg_basebackup` 物理、`pg_restore` | ✅ 14.12 实跑 |
| 金仓 KingbaseES | `core/engines/kingbase.py` | `sys_dump` / `sys_dumpall` / `sys_basebackup`，V8/V9R1/V9R3 目录表差异已兼容 | ✅ 137 与 133 实跑 |
| 达梦 DM8 | `core/engines/dameng.py` | `dexp` / `dimp`、联机 `BACKUP DATABASE FULL`、`dmrman`、DBMS_LOGMNR 日志解析 | ✅ 137 实跑 |
| Oracle | `core/engines/oracle.py` | `expdp` / `impdp` / RMAN（含 PITR、归档备份），代码内有 11g/19c 兼容分支 | ⚠️ **无端到端验证报告**：唯一专项报告 `oracle_backup_test_report_2026-08-12.md` 结论为 Partial（19c 连通性 Pass，但备份执行因缺 Oracle Client 降级为仿真 `"仿真备份(占位)成功；expdp 客户端不可用"`），此后无备份/恢复 E2E 落盘凭证。仅类型映射层面有覆盖（1281 组冒烟含 Oracle 源） |
| SQL Server | `core/engines/sqlserver.py` | 官方 T-SQL `BACKUP DATABASE` FULL/DIFF/LOG、`RESTORE ... WITH MOVE,REPLACE`、`RESTORE VERIFYONLY WITH CHECKSUM` | ✅ 2019 CU27 实跑 |
| MongoDB | `core/engines/mongodb.py` | `mongodump` / `mongorestore`（含 archive/压缩） | ⚠️ 代码已实现，**本次盘点未见端到端验证记录** |
| Redis | `core/engines/redis.py` | RDB 备份 | ⚠️ 代码已实现，**未见验证记录** |
| Neo4j | `core/engines/neo4j.py` | 三通道：离线 `neo4j-admin dump`（docker 场景 stop→临时容器 dump→start）、企业版在线 `backup`、APOC 导出（Cypher/JSON/CSV） | ⚠️ v1.4.8 新增，**未见验证记录** |
| 文件备份 | `core/engines/file.py` | 目录/文件 tar+zstd，增量链 + 合成全量，`file_snapshots` 记录 | ⚠️ **未见验证记录** |
| 虚拟机 | `core/vm/` | PVE / KVM(libvirt) / ESXi / Hyper-V 纳管 → 保护策略 → 恢复点(PITR) → 原地还原 / 克隆新 VM / 自动恢复验证 | ⚠️ README 已明示**尚未真实环境验证，不建议生产使用** |

> ⚠️ 项的含义：功能代码在，但在本次全仓库文档回查中**找不到真实环境的端到端验证记录**。
> 离线上线前若要承诺，必须先在客户环境做一次真实演练（备份→拉回→恢复→校验）。

### 3.3 恢复能力边界

- 逻辑备份恢复：还原到源库或跨主机目标库（`cross_host.py` 自动建库）。
- 跨主机恢复零安装：目标机无解压工具时，平台侧解压后上传明文再回放。
- MySQL `--databases` 产物会自动剥离 `CREATE DATABASE/USE`（否则写回源库名）；导入前清 GTID 避免 1840。
- 物理备份恢复：平台侧 prepare（按大版本选 xtrabackup 二进制），校验用远端自带 mysqld 起临时实例。
- **不支持**：异机裸机恢复、即时恢复（Instant Recovery）、不可变/WORM 防篡改（见 §9）。

### 3.8 数据同步 / 3.9 数据迁移（v1.4.9 重点）

- 同步插件：`mysql` `postgresql` `oracle` `sqlserver` `dameng`（`core/sync/plugins/`）。
- 执行模式：`full`（全量）、`full_db_migrate`（整库，结构+数据一体）、`incremental`（增量）、`realtime`（轮询 CDC 默认；MySQL/MariaDB 可 flink-engined binlog）。
- 迁移计划：`core/db_migrate.py` 编排，阶段 precheck → migrate → verify → report，状态机 created/checking/migrating/verifying/completed/failed。
- **v1.4.9 已修复并验证的 6 个缺陷**（详见 README v1.4.9）：自增丢失、`nextval` 建表 1064、`DATETIME(6)` 默认值 1067、`CHARACTER VARYING` 落 TEXT、整表失败被判「假成功」、`ColumnMeta` 契约。
- 同步**不覆盖**：双向同步、DDL 同步、大对象特化通道（部分类型按字节透传）。

### 3.7 仿真通道（**离线上线必须确认已关闭**）

CDC/实时链路保留了一条「仿真降级」实现：`core/cdc/simulated.py`（`engine_key="simulated"`）。
命中以下**任一**条件时会被选中（`core/cdc/__init__.py` 的选择逻辑）：

| 触发条件 | 说明 |
|---|---|
| `DEMO_MODE=on` | 全局开关，**生产必须为 false** |
| 任务标记 `demo_only` | 任务级标记 |
| `rt_mode=sample` | 模式配置显式要求 |
| **真实引擎依赖 import 失败** | **自动静默降级为仿真**（最危险的一条路径） |

兜底保护：仿真实现产生的**每个恢复点都打 `is_simulated=1`**，UI 显示「仿真」徽标，相关接口响应也会带 `simulated` 标记，
因此**可识别、可追溯**，不是偷偷造假。

**交付红线**：
1. 离线部署必须确认 `DEMO_MODE=false`；
2. 验收时打开 `/realtime`，**不得出现「仿真」徽标**；
3. 一旦出现徽标，说明某条真实链路的依赖没随镜像/离线包带上（缺模块 → import 失败 → 降级），查 import 错误见运维手册 §5.7；
4. 仿真产生的恢复点**不得**作为 RPO/RTO 承诺或恢复演练通过的证据。
5. **备份/恢复引擎侧已无假成功风险**（重要澄清，避免误判）：各引擎仍保留名为 `_simulate_backup` / `_simulate_restore` 的历史方法，但自 2026-08-14 起实现已硬化——`core/engines/base.py` 中它们一律返回 `success=False, status=FAILED, message="缺少必要客户端/连接，无法执行真实备份..."`，**不再产出假产物**。因此引擎侧出现这些名字**不是**仿真，缺客户端时会如实失败。真正会产生假数据的只有上面这条 CDC 通道。
   > 历史背景：2026-08-12 的 Oracle 测试报告记录了 `message = "仿真备份(占位)成功；expdp 客户端不可用"`——那就是这条旧路径造成的**假成功**，现已不可能复现；但该报告同时说明**真实 Oracle 备份当时并未跑通**，此结论至今没有新的报告推翻（见 §3.2 与 §9）。

### 3.11 存储与数据生命周期

- 存储目标：`storage_targets`（本地 / SSH 远端 / S3 兼容可配）。
- 分层复制：L1→L2→L3，`core/tier_replication.py`（目录型产物自动打包后再复制）。
- 全局去重：`core/global_dedup.py` + `dedup_index`。
- 保留策略 GFS：`core/retention_gfs.py`；自动生命周期：`core/lifecycle.py`。
- 磁带/出库：`api/tape.py`（4 条路由）提供出库/回库登记。

---

## 4. 页面清单（27 个）

`dashboard`(首页运营态势) `tasks` `protection` `records` `restore` `restore_records` `restore_verify`
`realtime`(含 PITR 与 CDC 两个标签页) `drills` `drlink` `vm` `vdb` `clone` `file_backup`
`sync` `migration` `data_compare` `datamining` `deploy` `storage` `logs` `inspection`
`plugins` `operations` `users` `settings` `agent` `login` `alert`

> 旧入口 `/cdc`、`/rt-timeline`、`/db-adapters` 保留 **302 重定向**并带深链参数（已在测试里固化该行为）。
> 内联片段：`_rt_panel.html`、`_cdc_panel.html`、`_db_adapters_panel.html`。

---

## 5. 接口清单（332 条路由，按模块）

| 模块 | 路由数 | 模块 | 路由数 |
|---|---|---|---|
| `app.py`（页面路由） | 33 | `api/vm.py` | 24 |
| `api/sync.py` | 22 | `api/storage.py` | 17 |
| `api/datamining.py` | 17 | `api/system.py` | 15 |
| `api/rt.py` | 13 | `api/tasks.py` | 11 |
| `api/data_compare.py` | 11 | `api/restore_verify.py` | 10 |
| `api/plugins.py` | 10 | `api/drills.py` | 10 |
| `api/cdc.py` | 10 | `api/migration.py` | 9 |
| `api/link.py` | 9 | `api/db_adapters.py` | 9 |
| `api/rbac.py` | 8 | `api/logs.py` | 8 |
| `api/deploy.py` | 8 | `api/clone.py` | 8 |
| `api/records.py` | 7 | `api/policy.py` | 7 |
| `api/jdbc.py` | 7 | `api/ai_alert.py` | 7 |
| `api/hosts.py` | 6 | `api/ai_agent.py` | 6 |
| `api/restore.py` | 5 | `api/restore_extras_api.py` | 5 |
| `api/inspection.py` | 5 | `api/tape.py` | 4 |
| `api/itsm.py` | 4 | `api/lifecycle.py` | 3 |
| `api/synthesize.py` | 2 | `api/dedup.py` | 2 |

> 上线做接口连通性体检可用 `scripts/api_audit.py`（自动发现路由 + 真实请求，抓 5xx / 非 JSON / 假成功 / 鉴权洞）。

---

## 6. 数据模型（46 张元数据表）

`ai_messages` `ai_sessions` `alert_predictions` `anonymized_exports` `api_tokens` `backup_objects`
`backup_records` `backup_sets` `backup_tasks` `cdc_events` `cdc_streams` `clone_requests`
`data_compare_reports` `data_compare_tasks` `data_scan_results` `db_adapters` `db_migration_plans`
`dedup_index` `deployments` `disaster_links` `drills` `hetero_jobs` `inspection_records`
`itsm_tickets` `log_repository` `migration_plans` `plugin_host_state` `protection_policies`
`recovery_journal` `restore_records` `restore_test_reports` `restore_verify_policies`
`rt_capture_state` `rt_tasks` `ssh_hosts` `storage_targets` `sync_records` `sync_tasks`
`system_config` `system_logs` `users` `vdb_instances` `vm_hypervisors` `vm_jobs` `vm_protected`
`vm_recovery_points`

> 迁移相关有 `migration_plans`（迁移保护，老语义）与 `db_migration_plans`（DTS 式迁移计划），两套并存，不要混。

---

## 7. 配置与开关

`config.py` 暴露 **69** 个配置项，其中绝大多数可用**同名环境变量覆盖**（离线环境改配置的首选方式，不需要动代码）。

### 7.1 高频项

| 环境变量 | 含义 | 备注 |
|---|---|---|
| `WEB_USERNAME` / `WEB_PASSWORD` | 平台登录账号 | 首登会标记 `must_change_password` |
| `WEB_HOST` / `WEB_PORT` / `WEB_THREADED` | 监听与线程 | 压测后已默认开多线程 |
| `SECRET_KEY` / `SESSION_TIMEOUT` / `COOKIE_SECURE` | 会话安全 | 生产建议设 `COOKIE_SECURE=1` |
| `META_DB_PATH` | 元数据库位置 | **必须落在持久化卷** |
| `BACKUP_ROOT` | 备份产物根目录 | 容量增长预测也读它 |
| `INSTANCE_DIR` / `LOG_DIR` | 实例目录 / 日志目录 | |
| `BACKUP_CMD_TIMEOUT` | 执行超时，默认 0=不限 | 大库不要设小 |
| `BACKUP_IDLE_TIMEOUT` | 空闲超时，默认 1800s | 区分「读得慢」与「真卡死」 |
| `BACKUP_RETRY_INTERVAL` / `BACKUP_RETRY_MAX` / `BACKUP_RETRY_DELAY` | 失败重试 | |
| `BACKUP_RESUME_ENABLED` / `BACKUP_RESUME_TTL` / `BACKUP_REMOTE_STAGE` / `BACKUP_STABLE_SECS` | 断点续传与固定产物结束判定 | 大库必备 |
| `XTRABACKUP_8_PATH` / `XTRABACKUP_24_PATH` / `MARIABACKUP_PATH` | 物理备份二进制路径 | 缺哪个就缺对应产品的物理备份能力 |
| `CLONE_AUTO_APPROVE` | 克隆免审批直通，默认 true | |
| `SCHEDULER_ENABLED` | 总调度开关 | |
| `DEMO_MODE` | 演示模式 | 生产必须为 false |
| `RT_*`（约 20 项） | 实时守护：重连、封段、配额、重试、守护 tick 等 | 见 §8 |
| `FERRY_INBOX_DIR` | 摆渡盘收件箱目录 | 单向网闸/摆渡场景 |
| `AI_SECRET_KEY` / `NOTIFY_ENABLED` / `NOTIFY_ON_*` | AI 与外部通知 | 离线环境保持关闭 |
| `LOGIN_MAX_FAILS` / `LOGIN_LOCK_MINUTES` | 登录锁定策略 | |
| `RESTORE_PARALLEL` / `COMPRESS_BY_DEFAULT` / `DEFAULT_RETENTION_*` | 恢复并发、默认压缩、默认保留 | |
| `DM_JDBC_JAR` / `BACKUP_PLATFORM_STRICT_SSL` / `OPLOG_*` / `SYNC_DEBUG` | 驱动路径、SSL、运维日志、同步调试 | |

### 7.2 完整变量清单获取方式（离线环境也能查）

```bash
grep -rhoE "os\.(getenv|environ\.get)\(['\"][A-Z_0-9]+" core api config.py app.py | grep -oE "[A-Z_0-9]+$" | sort -u
```

---

## 8. 上线后「能调」与「不能调」（**最重要**）

### 8.1 不改代码即可调整

- 所有 §7 的环境变量（容器 `-e` / `docker-compose environment` / systemd `Environment=`）。
- 任务级能力（前端表单或任务 API）：
  - 执行超时、空闲超时、重试策略、断点续传开关。
  - 任务级 SSH 凭据（`extra_options.ssh_cred`，免纳管）。
  - 工具路径兜底（`extra_options.tool_path`，冒号/分号分隔的 bin 目录）。
  - 自定义环境变量（`extra_options.env_vars`，多行 `KEY=VALUE`，本机与远程命令均注入；`PATH` 为前缀合并不覆盖）。
  - 自定义备份/恢复脚本（`custom_script` / `custom_restore_script` / `custom_artifact_dir` / `custom_timeout`，`backup_mode=custom`）。
  - 全实例是否包含系统库（`include_system_dbs`）。
- 页面可配：主机纳管、存储目标、保留策略、告警规则、通知、RBAC 用户与令牌、巡检策略、演练计划。

### 8.2 需要改代码才能调整（**必须在上线前定稿**）

| 项 | 原因 | 上线前要确认 |
|---|---|---|
| 新增数据库类型/引擎 | 要新增 `core/engines/*.py` 并注册 | 客户现网有哪些类型，是否已在矩阵内 |
| 新增同步/迁移方向 | 要新增 `core/sync/plugins/*.py` | 已覆盖 mysql/pg/oracle/sqlserver/dameng |
| 元数据库换 MySQL/HA | 当前 SQLite + 进程内调度，抽象未做 | 是否承诺 HA（当前**不能**） |
| 前端页面/字段增删改 | Jinja2 模板与原生 JS | 定制字段本次是否已全部提出 |
| 不可变/防篡改 | 结构性空白（无相关代码） | 招标条款是否强制要求 |
| 报表模板 / 导出列 | `core/reports.py` 与模板耦合 | 客户报表格式是否已定 |
| 词表/字典（业务系统、告警文案） | 散落在各模块常量 | 是否需要客户化词表 |

> **结论给实施同学**：除了上表右侧几类，绝大多数现场差异（超时、重试、路径、凭据、脚本、环境变量）都能在线调，
> 不需要改 Python。遇到必须改代码的诉求，说明需求在上线前没提干净，应按变更流程走，不能在离线客户机上热改。

---

## 9. 已知空白与边界（**不能对客户承诺的项**）

| 缺口 | 现状 | 影响 |
|---|---|---|
| 元数据库仅 SQLite + 进程内调度 | 无 HA、无法横向扩展 | 单机部署，平台自身需主机级高可用保障 |
| 不可变备份 / WORM / 保留锁定 / 防篡改 | 全仓库零匹配 | 防勒索场景是硬缺口 |
| 即时恢复 / 异机裸机恢复 | 未实现 | RTO 承诺要留余量 |
| 恶意代码扫描 | 未实现（离线无病毒库） | 只能做启发式异常检测（P1 路线） |
| 同步：双向 / DDL 同步 | 未实现 | 单向数据流 |
| MongoDB / Redis / Neo4j / 文件备份 / VM | 无端到端验证记录 | 上线前必须真实演练 |
| **Oracle 备份 / 恢复 / RMAN / 归档实时** | 唯一专项报告（2026-08-12）结论为 **Partial**：19c 连通性 Pass，但备份执行因本机缺 Oracle Client **降级为仿真**；此后无端到端报告 | **上线前必须在客户真实 Oracle 环境跑通闭环**（备份→拉回→恢复→校验），19c 与 11g 各一轮，并补恢复校验（impdp SQLFILE / RMAN VALIDATE） |
| 委托 Global 去重为全局单实例 | 依赖 `dedup_index` | 元库损坏需重建索引 |
| AI 助手依赖外部 LLM | 离线环境不可用 | 默认关闭，非卖点 |
| 全量 pytest 存在用例间共享临时库污染 | 170+ failed（既有，非功能缺陷） | 见下方「回归基线」 |

### 回归基线（重要，避免误判）

全量 `pytest tests` **存在既有的用例间共享临时库相互污染**（约 170 failed，但同一批文件隔离跑全绿）。
判断某次改动有无回归，**不要直接看绝对失败数**，应该用：

```bash
git archive HEAD | tar -x -C /tmp/base && cd /tmp/base && pytest tests -q --ignore=tests/playwright_save_test.py
```

与改动后的同命令结果对比。当前基线：**HEAD 171 failed / 291 passed / 30 errors → v1.4.9 为 170 failed / 292 passed / 30 errors**。
`playwright_save_test.py` 依赖 playwright（离线环境通常没有），跑全量时必须 `--ignore`。

---

## 10. 版本固化信息

| 项 | 值 |
|---|---|
| 版本 | v1.4.9 |
| Commit | `535f6f2` |
| 镜像 | `ghcr.io/zhh9126/backup-platform:v1.4.9`（`v1.4.9` / `v1.4.9-20260916` / `latest`） |
| 镜像 digest | `sha256:728eee6ea13abee8f128870d8d64f7636188febc91e98c83bbf47035c59fe2f2` |
| 仓库 | GitHub `Zhh9126/backup-platform`；Gitee 镜像 `zhh_w/backup-platform` |
