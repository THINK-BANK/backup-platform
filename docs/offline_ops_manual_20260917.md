# AIDBM 离线交付与运维手册（v1.4.9）

> 面向**完全离线（air-gapped）**交付场景：客户现场不能联网、不能装依赖、**改代码要走变更流程**。
> 本手册的目标是：**让现场运维不改一行 Python 就能把平台跑起来、把常见故障处理掉。**
>
> 配套：[产品功能固化清单](feature_manifest_20260917.md)（有什么能力、哪些没验证）

---

## 1. 三条交付路径怎么选

| 形态 | 适用 | 优点 | 注意事项 |
|---|---|---|---|
| **Docker 镜像**（推荐） | 客户现场有 Docker/私仓 | 依赖全部烘焙在内，开箱即用 | 工作目录 `/app`，数据目录 `/data`，SSH/HTTP 端口需映射 |
| **离线交付包**（脚本安装） | 无容器、只有裸机 | 不依赖容器运行时 | 先在本机/net 侧用 `make_bundle.sh` 打 wheelhouse，现场 `install.sh` 建 venv |
| PyInstaller 打包 | 需要单目录绿色交付 | 客户机不必装 Python | `.spec` 已在仓库，构建需在本机完成，产物较大 |

---

## 2. 在「有网侧」准备交付物

### 2.1 Docker 镜像

```bash
docker build -t ghcr.io/zhh9126/backup-platform:v1.4.9 .
docker push ghcr.io/zhh9126/backup-platform:v1.4.9
```

镜像内目录（由 Dockerfile 决定）：

| 路径 | 内容 |
|---|---|
| `/app` | 应用代码（`app.py` `run.py` `init_db.py` `config.py` `auth.py` `start.sh` `core/` `api/` `static/` `templates/` `drivers/` `skills/` `tools/`） |
| `/data/backups` | 备份产物根（`BACKUP_ROOT` 默认） |
| `/data/instance` | 实例数据（元数据 SQLite 等，`INSTANCE_DIR`） |
| `/data/logs` | 日志（`LOG_DIR`） |

> 注意：**`scripts/` 不进镜像**（既有设计），因此现场镜像内没有 `api_audit.py`、`stress_test_full.py`。
> 需要做接口体检时，把脚本单独带进去执行即可。

### 2.2 离线交付包

```bash
cd /root/CodeBuddy/20260826095855/backup-platform
bash scripts/offline/make_bundle.sh          # 输出到 /tmp/offline_bundle
```

产物包含 `app/`（应用）、`wheelhouse/`（pip download 的全部依赖 wheel）、`tools/`、`manifests/`、
`install.sh`、`healthcheck.sh`，最终打成 `backup-platform-offline-<VER>.tar.gz`。

校验依赖是否自足（**交付前必做**）：

```bash
python3 scripts/check_offline.py
```

---

## 3. 客户现场安装

### 3.1 Docker 形态（推荐最小可用）

```bash
docker run -d --name aidbm --restart=always \
  -p 8080:8080 \
  -v /opt/aidbm/data:/data \
  -e WEB_PASSWORD='<强口令>' \
  -e SECRET_KEY='<随机串>' \
  -e COOKIE_SECURE=0 \
  ghcr.io/zhh9126/backup-platform:v1.4.9
```

首登用配置的 `WEB_USERNAME` / `WEB_PASSWORD`，**首登会被标记 `must_change_password`，务必立即改密码**。

### 3.2 离线包形态

```bash
tar xzf backup-platform-offline-v1.4.9.tar.gz
cd backup-platform-offline-v1.4.9
bash install.sh                # 创建 INSTALL_DIR、data/{backups,instance,logs}、python3 -m venv .venv
bash tools/healthcheck.sh      # 检查 curl 等必备工具
```

### 3.3 启动（**务必用 `start.sh`**）

```bash
setsid nohup bash start.sh > /tmp/aidbm.log 2>&1 < /dev/null &
```

> 开发/工作台环境的 safe-delete 钩子会杀平台进程，`start.sh` 内置了保护变量；
> 客户机虽然没这钩子，但 `start.sh` 仍是统一入口（内含依赖检查与日志重定向）。
> **不要用 `python run.py` 直接前台跑**，SSH 断开即退出。

---

## 4. 上线前必做清单（打勾后才能交付）

- [ ] `bash tools/healthcheck.sh` 全绿（或记下缺失项并确认不影响本次范围）
- [ ] 访问 `/login` 返回 200（容器内验证：`curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8080/login`）
- [ ] 日志出现 APScheduler 周期任务注册（`lifecycle` / `clone` 等），说明调度起来了
- [ ] 修改默认账号密码；如对外对接，确认 `COOKIE_SECURE`、来源 IP 限制
- [ ] 每种要保护的数据库**至少做一次真实闭环**：备份 → 拉回 → 恢复 → 恢复校验 → 看行数/样本值
- [ ] 接口体检（把 `scripts/api_audit.py` 带进镜像/tmp 跑一遍）：0 个 5xx、0 段 traceback
- [ ] 确认 `META_DB_PATH`、`BACKUP_ROOT` 都在**持久化卷**上，且有容量监控
- [ ] 确认备份目录所在磁盘余量（首页「容量与增长预测」读的就是它）
- [ ] 抽一台机器上跑一遍"平台进程被 kill 后能否自动拉起"（`--restart=always` 或 systemd）

---

## 5. 排障地图（**症状 → 处置**，优先给不用改代码的手段）

### 5.1 平台起不来 / 页面打不开

| 症状 | 定位 | 处置 |
|---|---|---|
| 端口不通 | `ss -lntp \| grep 8080` | 检查 `-p` 映射、`WEB_HOST`/`WEB_PORT` |
| 容器反复重启 | `docker logs aidbm \| tail -50` | 多为 `SECRET_KEY` 或数据卷权限 |
| 起进程后秒退 | 看 `/tmp/aidbm.log` 或 `LOG_DIR` | 是否用前台方式启动（应 `setsid nohup bash start.sh`） |
| 登录后立刻掉线 | 会话/时间问题 | 检查主机时区与 `SESSION_TIMEOUT`；对外建议 `COOKIE_SECURE=1` + HTTPS |
| 登录被锁 | 连续失败触发 | 环境变量 `LOGIN_MAX_FAILS` / `LOGIN_LOCK_MINUTES` 调整，或等待锁定期 |

### 5.2 并发 / 性能

| 症状 | 处置 |
|---|---|
| `database is locked` | 已默认 `timeout=30` + `busy_timeout=30000`；仍有高并发写时优先**降并发**而非改代码，生产建议 `gunicorn -w 4 --threads 8`（内置web concurrent) |
| 备份吞吐上不去 | 实测瓶颈是**单次备份固定开销**（13~15 个/秒），把 `max_concurrent_backups` 调大收益很小；应错峰 + 分批，并给大库开断点续传 |

### 5.3 MySQL / MariaDB

| 症状 | 原因 | 处置 |
|---|---|---|
| Access denied（用了正确密码） | 服务器上 `/root/.my.cnf` 的 password 优先级高于 `MYSQL_PWD` | 平台内部调用已统一加 `--no-defaults`；若客户自定义脚本自己调 mysql，也要加 `--no-defaults` |
| 导入报 1840 `GTID_PURGED` | 备份带 GTID 集，目标已有 GTID | 平台跨主机恢复已自动清理；自定义脚本请在导入前 `RESET MASTER`（按其规范） |
| 物理备份找不到 xtrabackup | 平台侧缺对应版本二进制 | 配 `XTRABACKUP_8_PATH`（8.0+）/ `XTRABACKUP_24_PATH`（5.5–5.7）/ `MARIABACKUP_PATH`；**绝不在数据库服务器装** |
| `--databases` 产物恢复到别的库却写回源库名 | 产物自带 `CREATE DATABASE/USE` | 平台已自动剥离；自研脚本需注意 |
| 目标机没有 `mysql` 命令 | PATH 里没有（源码装在 `/opt/mysql*/bin`） | 任务高级选项 `tool_path` 填 bin 目录（冒号/分号分隔） |

### 5.4 PostgreSQL / 金仓

| 症状 | 处置 |
|---|---|
| PITR 卡住不前进 | `restore_command` 必须能处理 `.history` 文件（不能一律按压缩格式解压）；先 `pg_switch_wal()` 强制归档 |
| 时间线无限扫描 | 必须限定 `recovery_target_timeline` |
| 恢复实例起不来 | 用**独立端口**，避免与主库冲突 |
| 金仓连不上（报口令错） | V9 的口令环境变量是 `KINGBASE_PASSWORD`（V8 才是 `PGPASSWORD`），平台双注入；自研脚本注意区分 |
| 金仓系统目录表找不到 | V9R1 是 `pg_database`（不是 `sys_database`） |

### 5.5 Oracle

> 上线必读：Oracle 的备份/恢复链路**没有端到端验证报告**（唯一专项报告 `docs/oracle_backup_test_report_2026-08-12.md` 结论 Partial，19c 连通性 Pass 但备份执行因缺少客户端未跑通）。
> 因此本节条目是**既往实战经验**，不是当前版本的验收结论——首次对接客户 Oracle 环境时，必须按 §4 完整跑一遍闭环，并把结果与本节的差异补充回来。
> 好消息是：当时的"仿真占位成功"假成功路径已硬化为失败（见功能清单 §3.7.5），现在缺客户端会**明确报错**而不是假装成功。

常见报错 `缺少必要客户端/连接，无法执行真实备份`：说明平台侧/远端找不到 `expdp` 或 `rman`。
处置顺序（均不改代码）：① 任务高级选项 `tool_path` 填 Oracle 的 `bin` 目录；② 确认远端预检用 `oracle` 用户探测（服务端工具常只在 oracle 用户 profile 可见）；③ 用 `resolve_remote_tool` 的目录枚举思路确认实际路径（`/u01/app/oracle/product/*/*/bin`）。

| 症状 | 处置 |
|---|---|
| ORA-01109（数据库未打开） | 19c 重启后 PDB 可能回落 MOUNTED，平台已加自动 `ALTER PLUGGABLE DATABASE ALL OPEN`；手工场景先开 PDB |
| 找不到 `DATA_PUMP_DIR` | 必须用与 expdp/impdp **相同的登录方式**（service 连接）查询；19c 下 CDB 与 PDB 目录对象不同 |
| ORA-39145（目录对象为空） | expdp 必须显式指定 `DIRECTORY` |
| 预检查报客户端工具缺失 | 服务端工具只在 oracle 用户 profile 可见，工具探测要用 oracle 用户（平台已按此实现） |
| impdp 报 ORA-31684 / ORA-39082 | 非致命（对象已存在等），数据已导入应按成功判定（平台已容错） |

### 5.6 SQL Server

| 症状 | 处置 |
|---|---|
| T-SQL 报错但任务显示成功 | `sqlcmd` 必须带 `-b`（平台已带）；自研脚本注意 |
| 密码相关失败/泄露风险 | 走 `SQLCMDPASSWORD` 环境变量，不进 argv |
| 路径解析出一堆脏数据 | `SERVERPROPERTY` 输出要过滤表头、分隔线、`(N rows affected)` 脚注 |
| 备份目录权限 | 远端备份目录需 `chown mssql` |

### 5.7 实时保护 / CDC

| 症状 | 处置 |
|---|---|
| RT 守护「本进程不启动」 | 旧进程仍持有 `rt_supervisor.lock`（flock）。`pkill` 后必须 `ps` 复查确认死透再拉起 |
| RPO 持续告警 | 看 `RT_RPO_ALERT_MIN_SEC` / `RT_ALERT_SUPPRESS_MIN`；先确认捕获源是否真的在写 |
| MySQL binlog 捕获连不上 | 检查是否受 `.my.cnf` 干扰（同 5.3） |
| 封段/恢复点异常 | `RT_DB_SEAL_INTERVAL_SEC`、`RT_UPLOAD_BATCH_MB`、`RT_UPLOAD_INTERVAL_MIN` 可调 |
| **页面出现「仿真」徽标** | 命中仿真降级路径，见 §5.7.1（**必须立即处理**） |

#### 5.7.1 出现「仿真」徽标的处理流程

「仿真」意味着这条 CDC 链路用的是 `core/cdc/simulated.py`，**不是真实日志流**。触发原因按优先级排查：

1. `DEMO_MODE` 是否为 `on`（生产必须 `false`）——改环境变量重启即可，**不用改代码**。
2. 任务是否被标记 `demo_only`，或 `rt_mode` 是否为 `sample`——前端/任务参数调整即可。
3. **真实引擎依赖缺失导致 import 失败自动降级**：这是离线环境最常见的隐蔽故障。
   - 查控制器轨迹：`docker exec aidbm bash -lc 'python -c "import core.cdc.mysql_binlog"'`，看抛什么 ImportError；
   - 对照 ImportError 缺失的模块，把对应 wheel 补进离线包的 wheelhouse（在有网侧重打 `make_bundle.sh`，不要现场联网 pip）。

处理完毕后：删除仿真产生的恢复点，**重跑一次真实链路**（新建恢复点 → 恢复 → 校验），确认徽标消失。
**在有仿真徽标期间产生的恢复点，不得计入 RPO/RTO 承诺与演练通过。**

### 5.8 达梦 DM8

| 症状 | 处置 |
|---|---|
| 口令含 `@` 连接失败 | `USERID` 必须双引号包裹 |
| dimp 报错 | `FILE` 必须带 `.dmp` 扩展名 |
| DBMS_LOGMNR 常量报错 | DM8 包常量块内不解析，必须用**字面量**（NEW=1 / REMOVE=2 / ADDFILE=3） |
| 归档未开 | 需先开启归档才能做增量/实时 |

### 5.9 克隆 / VDB

- 默认**免审批直通**（`CLONE_AUTO_APPROVE` 默认 true）；需要走工单审批的环境设为 `false`。
- 不支持的产物（如部分物理 tar）会**诚实失败**并在前端显示原因，不会降级成"假成功"。
- 失败可在界面重试（重试 = 再次 approve）。

### 5.10 日志在哪

| 内容 | 位置 |
|---|---|
| 平台运行日志 | `LOG_DIR`（容器 `/data/logs`） |
| 任务级日志/错误 | 页面 `/logs`、`log_repository` 表 |
| 远端命令输出 | 任务执行详情（平台已把远端真实输出带回，不再只有 `rc=1`） |

---

## 6. 升级与回滚

### 6.1 升级（镜像形态）

```bash
docker pull ghcr.io/zhh9126/backup-platform:<新版本>
docker stop aidbm && docker rm aidbm
# 用相同的 -v /opt/aidbm/data 重新 docker run（数据卷必须沿用）
```

### 6.2 回滚

1. 保留旧镜像 tag（不要用 `latest` 做生产引用，**始终钉具体版本**，如 `v1.4.9`）。
2. 只换镜像 tag 重新 run；若元数据 schema 有变更，用升级前对 `/data/instance` 做的目录级快照恢复。

### 6.3 升级前必做

- 备份 `/data/instance`（元数据）与 `/data/backups`（若同机）。
- 记录当前生效的全部环境变量（`docker inspect aidbm | jq '.[].Config.Env'`）。
- 与服务对象确认窗口期，避免与备份窗口重叠。

---

## 7. 现场「不能做什么」（交付给客户的红线）

1. **禁止在被备份的数据库服务器上安装任何备份工具/agent**（xtrabackup、mariabackup、pgbackrest 等一律不允许）。
   物理备份由平台把二进制临时推送到远端 `/tmp`，用完即清理。
2. **禁止现场联网安装依赖**（pip/apt/yum 下载）。所有依赖必须随离线包/镜像自带。
3. **禁止在生产机上热改 Python 代码**：需求变更走版本流程。绝大多数差异可用 §8 的手段解决。
4. **不要把 `latest` 作为生产引用**，钉版本号。
5. **不要把 `META_DB_PATH` 放在临时目录/容器可写层**，否则容器重建元数据全丢。

---

## 8. 不改代码能做的全部调整（速查）

| 诉求 | 手段 |
|---|---|
| 改端口/账号/会话安全 | `WEB_HOST` `WEB_PORT` `WEB_USERNAME` `WEB_PASSWORD` `SESSION_TIMEOUT` `COOKIE_SECURE` |
| 大库超时/重试/续传 | `BACKUP_CMD_TIMEOUT` `BACKUP_IDLE_TIMEOUT` `BACKUP_RETRY_INTERVAL` `BACKUP_RETRY_MAX` `BACKUP_RESUME_ENABLED` `BACKUP_RESUME_TTL` `BACKUP_STABLE_SECS` `BACKUP_REMOTE_STAGE` |
| 存储路径 | `BACKUP_ROOT` `INSTANCE_DIR` `LOG_DIR` `META_DB_PATH` |
| 实时保护行为 | `RT_*` 系列（约 20 项，见功能清单 §7） |
| 单任务特殊环境 | 任务高级选项 → `env_vars`（多行 KEY=VALUE，本机与远端均注入，`PATH` 前缀合并） |
| 单任务免纳管 SSH | 任务高级选项 → `ssh_cred` |
| 工具不在 PATH | 任务高级选项 → `tool_path`（bin 目录列表） |
| 完全定制的备份动作 | `backup_mode=custom` + `custom_script` / `custom_restore_script`（SFTP 上传到数据库服务器执行，注入 `PLATFORM_DB_*`、`PLATFORM_BACKUP_DIR`、`PLATFORM_BACKUP_FILE` 等） |
| 包含/排除系统库 | 任务 `include_system_dbs` |
| 克隆是否免审批 | `CLONE_AUTO_APPROVE` |
| 关停 AI/外部通知 | 不配 `AI_SECRET_KEY`；`NOTIFY_ENABLED=0` |
| 单向网闸/摆渡场景 | `FERRY_INBOX_DIR` + 摆渡收件箱流程（`core/ferry_inbox.py`） |

> 若以上都解决不了，才属于「必须改代码」的变更——参见功能清单 §8.2，上线前务必一次性提清。

---

## 9. 版本与联系人位

| 项 | 值 |
|---|---|
| 版本 | v1.4.9（commit `535f6f2`） |
| 镜像 | `ghcr.io/zhh9126/backup-platform:v1.4.9`（同 `v1.4.9-20260916`、`latest`） |
| digest | `sha256:728eee6ea13abee8f128870d8d64f7636188febc91e98c83bbf47035c59fe2f2` |
| 仓库 | GitHub `Zhh9126/backup-platform`；Gitee 镜像 `zhh_w/backup-platform` |
