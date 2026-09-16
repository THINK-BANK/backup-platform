# AIDBM 设计思想

> AI 原生智能数据库灾备管理平台（AI-Native Database Backup & Disaster Recovery Management）
> 版本 v1.4.8 · 2026-09-16
> 配套文档：`docs/product_design_spec_20260916.md`（产品设计说明书）

本文不描述功能，只回答一个问题：**这个平台为什么长成现在这样，而不是另一种样子**。
每条思想给出「主张 / 为什么 / 代码中的体现 / 我们因此放弃了什么」，便于后来者判断是否继续遵守。

---

## 0. 一句话总纲

> **在完全离线的约束下，用服务端集中、客户端零安装的方式，做成一件"结果必须真实"的事。**

三个定语分别对应三条不可退让的边界：
- **完全离线** —— 运行环境连不上任何外网，装不了任何东西；
- **服务端集中 / 客户端零安装** —— 被保护的对象（数据库服务器）不允许为备份这件事做任何改变；
- **结果必须真实** —— 备份这件事的整个价值建立在"真能恢复"上，任何"看起来成功"都是负价值。

后面所有取舍，都是这三条边界推出来的。

---

## 1. 真实优先于好看（No Simulation）

**主张**：连接失败、工具缺失、版本不支持，一律**如实失败**，并给出可读原因与修复指引；绝不降级为"模拟成功"。

**为什么**：备份系统最危险的失效模式不是"备份失败"，而是"备份显示成功但恢复不了"。一个会撒谎的备份系统比没有备份系统更糟——它会让运维停止警惕。演示演示 Demo 数据、成功率 99% 的漂亮仪表盘，在真正需要恢复的那天一分钱都不值。

**代码中的体现**：
- `core/engines/base.py` 的 `_should_simulate()` 自 2026-08-14 起**恒返回 `False`**；
- `config.py` 中 `DEMO_MODE = "off"`（硬编码，非配置项）；
- 各类"找不到工具"场景改为 `FAILED` + 已生成脚本路径提示（如 Oracle RMAN 缺失时不再返回 `SIMULATED`）；
- `verify_record()` 显式拒绝 `.sim` 仿真产物；
- 克隆服务对物理 tar 等不支持的产物**诚实 `failed`**，不做"降级仿真克隆"，失败原因写入 `note` 并在前端悬停可见。

**我们因此放弃了什么**：放弃了一切演示态的"好看"。首页健康评分因修正时区 bug 与虚指标后从 89 降到真实的 64——这个"难看的 64"正是本条思想的产物（见说明书 §10.2）。

---

## 2. 客户端零安装（Server-side Everything）

**主张**：一切依赖（驱动、JRE、客户端工具、备份二进制）只装在平台服务端；被管理的数据库服务器**不安装任何 agent、不安装任何备份工具、不做任何配置变更**。

**为什么**：
1. 生产数据库服务器是客户最敏感的资产，变更需要审批、窗口与回滚方案，"为了备份先装个 agent"在很多客户处根本推不动；
2. agent 自身也成为运维负担（升级、保活、兼容性）；
3. 版本碎片化现实：一个客户环境里可能同时有 Oracle 11g 和 19c、MySQL 5.7 和 8.0，agent 无法一套打天下。

**代码中的体现**：
- 所有远程能力走 **SSH + paramiko 单一通道**（SFTP 传脚本 → 远端 bash 执行 → 产物拉回 → 远端清理）；
- 物理备份需要 xtrabackup / mariabackup 时，平台把**版本匹配的二进制临时推送到远端 `/tmp`**，执行完即删，实现"免安装、免 agent、零残留"；
- 远端工具路径**绝不写死**：`resolve_remote_tool()` 按「指定用户 profile `command -v`（如 Oracle 用 oracle 用户）→ root 登录 shell → 常见目录 glob + find」四级动态发现，并支持任务级 `tool_path` 手动兜底；
- 逻辑备份直接用数据库**自带**的 mysqldump / pg_dump / expdp / dexp / sqlcmd，只探测不安装。

**我们因此放弃了什么**：放弃了 agent 带来的本地高速通道与细粒度文件级监听；代价是备份吞吐受网络与 SSH 通道约束（实测单任务固定开销主导，约 13~15 个/秒）。

---

## 3. 离线自足（Air-gapped First）

**主张**：平台必须能在**完全无外网**的环境安装并长期运行——不依赖 PyPI、DockerHub、NTP、任何外部 AI API。

**为什么**：目标交付场景是政企内网/隔离网。这不是"以后再适配"的可选项，而是决定技术选型的硬约束——它直接否决了任何需要构建链、需要在线拉依赖、需要云端推理的方案。

**代码中的体现**：
- 交付形态为自包含离线包：`scripts/offline/make_bundle.sh` 构建（镜像 + wheelhouse + 应用 + 安装器），`scripts/offline/install.sh` 安装，`scripts/offline/healthcheck.sh` 自检；
- `backup_platform.spec`（PyInstaller）负责完整打包；
- 前端**零 npm、零构建**：主体 Jinja2 模板 + Bootstrap 5 + 原生 JS；复杂交互页用本地化的 Preact + htm（ESM 已落到 `static/vendor/preact/`，约 16KB），通过 import map 加载；
- 驱动与运行时随包自带：JDBC jar + JRE（Docker 镜像内预装）、原生驱动（pymysql / psycopg2-binary / oracledb 瘦客户端）；
- AI 相关能力均可降级：无外部 API 时不阻断主流程。

**我们因此放弃了什么**：放弃了现代前端工程化（Vite/TS/组件库）、放弃了云端大模型推理、放弃了任何"联网就能解决"的取巧方案。

---

## 4. 可插拔优于硬编码（Extensibility by Contract）

**主张**：新增一种数据库支持，**优先不改代码**；必须改代码时，只在一处登记。

**为什么**：国产数据库与新兴数据库层出不穷（金仓、达梦、OceanBase、PolarDB、TiDB……）。如果每支持一种都要改调度、改前端、改存储、改校验，平台会迅速腐化成一堆 if-else。

**代码中的体现**：两级注册机制——
1. **静态注册表**（`core/engines/__init__.py`）：`ENGINE_REGISTRY` / `ENGINE_DISPLAY` + `get_engine()` 三级查找（内置 → VM 惰性注册 → `db_adapters.ensure_registered()` 动态兜底）；
2. **动态适配器**（`core/db_adapters.py` + `db_adapters` 表）：用户在「数据库类型」页填**脚本模板**（备份/增量/全实例/恢复/校验/列库/连通性）+ 能力声明，`build_engine_class()` 运行时动态造出 `CustomDBEngine` 子类注入注册表，**即刻获得与内置引擎同等的调度、产物拉回、存储分层、校验能力**；
3. 模板 `{{KEY}}` 渲染为 `${PLATFORM_KEY}`，**脚本不落明文密码**；
4. 前端下拉由 `engine_meta_map()` 自动生成，新增类型无需改前端。

**我们因此放弃了什么**：脚本模板的表达力弱于原生代码（复杂增量链、位点捕获难以纯脚本表达），因此**高性能与深度能力仍保留在内置引擎**，形成"内置引擎做深、自定义适配器做广"的分层。

---

## 5. 契约先于实现（Contract-first）

**主张**：跨层之间只通过**显式契约**通信，契约稳定优先于实现方便。

**为什么**：引擎种类会持续增长，只有契约稳定，调度层、存储层、前端才不需要跟着改。

**代码中的体现**：
- `BackupResult` dataclass 是引擎与调度层之间**唯一**的返回契约（success / status / path / size / original_size / checksum / compress_* / duration / **binlog_file·binlog_pos·wal_lsn** CDC 基线 / verified / detail_log 等）；
- 引擎必须声明元数据契约：`db_type`、`display_name`、`adapter_tier`、`required_clients`、`physical_bundled_tools`、`physical_external_plugins`、`tool_check_user`；
- 必须实现三个方法：`backup()` / `restore()` / `synthesize_full()`，其余（`verify_record`、`list_databases`、`check_client`、`preflight`、`list_sets`）基类有默认实现，按需覆盖；
- `AdapterContract(Protocol)` 声明五类契约（backup / restore / clone_to_test / verify / list_sets）——**注意：`clone_to_test` 与 `verify` 目前尚未在基类与各引擎落地，属于契约已声明、实现未对齐的缺口**（见说明书 §11）。

**我们因此放弃了什么**：放弃了各引擎"各显神通"的自由度，新增引擎必须理解并遵守契约。

---

## 6. 能力声明优于乐观假设（Declare, Don't Assume）

**主张**：能力必须**显式声明**，未知时宁可保守；不支持就**显式拒绝**，绝不静默降级。

**为什么**：静默降级是"撒谎"的另一种形式。用户以为做了一次物理增量备份，实际被降级成全量逻辑导出——容量、RPO、恢复方式全都变了，而用户毫不知情。

**代码中的体现**：
- 能力由引擎显式声明（如 SQL Server `physical_bundled_tools=[]` 即不支持物理备份；Redis 的 incremental/differential **统一降级为 snapshot** 且声明在案；Neo4j 的 `preflight()` **显式拒绝 physical** 而不静默转逻辑）；
- `_ENGINE_META_OVERRIDE` 用于修正 `engine_meta_map()` 的乐观默认值（默认假设支持 logical+physical），让前端如实展示真实能力；
- 实时 CDC 在环境不支持时降级为 `SimulatedCDCDaemon`，同时**写入 `degrade_reason`** 并暴露在实时保护状态中——降级本身也必须可见。

**我们因此放弃了什么**：放弃了"功能面板看起来什么都能做"的观感。

---

## 7. 远端优先、本机回退（Remote-first, Local-fallback）

**主张**：能用数据库服务器自带工具在远端完成，就在远端做；远端不具备条件时，平台侧兜底。

**为什么**：数据不落地平台、减少网络传输、避免版本不匹配的客户端解析失败，是这一取向的直接收益；同时必须有退路，否则远端环境一变就全线失败。

**代码中的体现**：
- `_try_remote_then_local(远端, 本机)` 是所有引擎的统一模式；
- 恢复校验同样远端优先（如 kingbase restore 已改为远程优先，与备份对称）；
- 平台本机侧保留版本匹配的 prepare 能力（MySQL 5.5–5.7 → xtrabackup 2.4.29，8.0+ → 8.0.35，MariaDB → mariabackup）；
- 大版本不一致时用远端自带 mysqld 起临时实例校验（`_verify_prepared_remote`，`setsid` 脱离会话、`--no-defaults` 白名单启动，避免 Percona/MariaDB 专有变量导致社区版 5.7 Aborting）。

**我们因此放弃了什么**：双路径意味着双倍的测试矩阵与排障路径。

---

## 8. 失败必须可读（Actionable Failure）

**主张**：任何失败都要回答三件事——**哪里错了、为什么、下一步怎么办**。

**为什么**：灾备系统的排障发生在深夜和业务中断时，此时"ERROR: backup failed"毫无价值。

**代码中的体现**：
- `core/ai_alert.py` 的 `suggest_action(error_text)` 基于错误文本给出修复建议；
- `core/oplog.py` 的 `OperationLog(kind="backup")` 记录**上下文 / 阶段 / 实际命令 / 退出码**，形成可回溯的执行档案；
- 客户端工具专用坑已固化进代码并注释说明（如 MySQL 的 `MYSQL_PWD` 必须配合 `--no-defaults`，否则 `/root/.my.cnf` 的 password 优先级更高导致 Access denied；`sqlcmd` 必须加 `-b`，否则 T-SQL 错误被 rc=0 掩盖）；
- 接口层统一错误语义：参数问题 400、唯一性冲突 409、`/api` 异常统一转 JSON（见 `app.py` 全局 errorhandler）。

**我们因此放弃了什么**：需要在每个失败分支多写几行，且要持续维护错误知识库。

---

## 9. 安全内建（Secure by Default）

**主张**：凭据不进命令行、不进日志、不落脚本；加密与权限是默认项而非可选项。

**为什么**：备份系统天然持有所有数据库的超级用户凭据，是最高价值的攻击目标。

**代码中的体现**：
- 密码一律走**环境变量**（`DB_BACKUP_PASSWORD` / `SQLCMDPASSWORD` / `KINGBASE_PASSWORD`）或**文件方式执行**（远端 SQL/rman 用 SFTP 写文件，避免引号嵌套兼防 argv 泄露）；
- `core/crypto_pool.py`：AES-256-GCM **信封式加密**，密钥来源三选一（环境变量 / 系统设置托管 / 外部 KMS）；
- 口令存储 PBKDF2-HMAC-SHA256，20 万轮迭代；登录失败锁定（`LOGIN_MAX_FAILS=5` / `LOGIN_LOCK_MINUTES=15`）；
- RBAC：**24 个权限点 × 3 档内置角色**（admin / operator / viewer），菜单按权限收敛 + **API 层二次校验**（不依赖前端隐藏）；
- `core/sensitive_scan.py` 敏感数据扫描；SSH 凭据 `ssh_cred` 加密存储（`_enc=1` 密文保留）。

**我们因此放弃了什么**：调试时不能简单 `ps aux | grep` 看命令；凭据流转链路变长。

---

## 10. 可观测优先于"能跑"（Observability）

**主张**：每一步执行都要留痕、可查询、可回放。

**为什么**：备份是低频高后果的操作，出问题时唯一能依赖的就是留痕质量。

**代码中的体现**：
- `oplog` 全链路操作日志 + `system_logs` 系统日志；
- `webhooks.emit_async("backup.success|failure")` 对外事件；
- `inspection` 巡检、`drills` 恢复演练、`restore_verify` 恢复校验——把"备份成功"推进到"**恢复成功**"和"**可验证**"；
- 首页态势指标（保护覆盖率 / RPO 合规率 / 待处理风险 / 恢复点 / 容量预测）把"有多少数据真的被保护住"变成可量化问题。

---

## 11. 约束驱动的克制（Restraint）

**主张**：能用简单方案解决就不引入复杂设施；**引入新依赖的门槛极高**。

**为什么**：离线交付 + PyInstaller 打包 + 长期维护，意味着每引入一个依赖都要打包、验证、背负三年。而团队对客户的第一承诺是"装上就能跑，五年后还能跑"。

**代码中的体现**：
- 调度用进程内 APScheduler 而非 Celery/RabbitMQ（省掉一整套中间件，代价见说明书 §9.2）；
- 元数据库用 SQLite 而非独立数据库（省掉一套外挂依赖，代价是单机约束）；
- 前端主体原生 JS，Preact 仅在 `data_compare.html` 等复杂页试点；
- 图表用**内联 SVG**而非 Chart.js/ECharts（首页趋势图为手写 SVG，零 CDN）。

**我们因此放弃了什么**：放弃分布式能力、放弃现代前端体验、放弃部分性能上限。这些取舍在"标准化产品化"阶段需要被重新审视——**这正是说明书 §11 要讨论的主题。**

---

## 12. 思想的优先级（冲突时如何裁决）

当上述原则互相冲突时，按此顺序裁决：

```
1. 真实优先（No Simulation）          ← 最高，不可交易
2. 客户端零安装                        ← 产品形态底线
3. 离线自足                            ← 交付形态底线
4. 安全内建
5. 失败可读 / 可观测
6. 可插拔 / 契约
7. 性能与体验
8. 开发便利                            ← 最低
```

举例：为支持某数据库新增能力需要 agent → 违反第 2 条 → 即使能提升性能（第 7 条）也必须否决，改为"临时推送二进制、用完即删"的折中方案（这在 xtrabackup 方案上已经发生过一次）。

---

## 附：设计思想的达成度自评

诚实标注哪些是**已落地**、哪些仍是**方向性主张**：

| # | 设计思想 | 达成度 | 说明 |
|---|---|---|---|
| 1 | 真实优先 | ✅ 已落地 | 仿真兜底已从代码层面移除 |
| 2 | 客户端零安装 | ✅ 已落地 | 多环境实测零残留 |
| 3 | 离线自足 | ✅ 已落地 | 离线包 + 自检脚本齐备 |
| 4 | 可插拔 | ✅ 已落地 | 界面新增类型零代码 |
| 5 | 契约优先 | ⚠️ 部分 | `clone_to_test` / `verify` 契约已声明未在引擎落地 |
| 6 | 能力声明 | ⚠️ 部分 | 仅 neo4j 登记了 `_ENGINE_META_OVERRIDE`，其余引擎仍走乐观默认 |
| 7 | 远端优先 | ✅ 已落地 | 双路径均实测 |
| 8 | 失败可读 | ⚠️ 部分 | 错误建议覆盖有限，错误码体系尚未统一 |
| 9 | 安全内建 | ✅ 已落地 | 加密/RBAC/凭据隔离齐备 |
| 10 | 可观测 | ⚠️ 部分 | 有日志与事件，但缺标准化指标导出（Prometheus 等） |
| 11 | 克制 | ✅ 已落地 | 依赖清单精简 |

---

**文档版本**：v1.0 · 2026-09-16
**维护约定**：本文件记录"为什么"，代码变更若**违反**其中任何一条，需在 PR 中显式说明理由；若**印证**了新的取向，应回来补充条目。
