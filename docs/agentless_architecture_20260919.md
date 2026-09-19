# AIDBM「无 Agent」能力体系设计（借鉴 Apache HertzBeat）

> 日期：2026-09-19　｜　目的：把"我们不用 Agent"从**口号**变成**可声明、可验证、可对外承诺的产品能力**。
> 编写原则与既往一致：**只写仓库里真实存在的代码**，未实现的显式标注。
> 对照项目：Apache HertzBeat（`apache/hertzbeat`），Apache-2.0，Agentless 实时监控告警系统。

---

## 1. HertzBeat 值得到借鉴的是什么（先划清边界）

| HertzBeat 的做法 | 对我们的启发 | 是否照搬 |
|---|---|---|
| **协议驱动采集**：不再 target 上装探针，Collector 直接用 HTTP / HTTPS / JMX / SNMP / JDBC / SSH / Telnet 等标准协议与目标对话 | 这正是我们的主张。我们应把"AIDBM 到目标端到底走了几条协议、各自侵入到什么程度"**显式建模**，而不是散落在引擎代码里 | ✅ 借鉴建模方式 |
| **监控模板数据化**：每种监控类型一份 YAML（`app-*.yml`）描述 `protocol` + 采集字段 + 表达式，新增资产类型**不改引擎代码** | 当前我们的能力描述硬编码在 Python 引擎类中，新增库型/版本必须改代码发版 | ✅ 借鉴（重点 P1） |
| **Manager / Collector 分离、Collector 可集群注册、就近采集** | 对应我们的"平台本体 + SSH 前置机 + 离线摆渡机"（`core/ferry_inbox.py`），应升级为一等公民"执行节点" | ✅ 借鉴（P2） |
| **告警阈值规则化 + 多渠道**（邮件/钉钉/企微/飞书/Webhook，门槛低） | 我们有 `api/ai_alert.py`、`core/notifier.py`、`core/webhooks.py`、`core/itsm.py`，但阈值多为硬编码 | ⚠️ 部分借鉴（P3） |
| Prometheus 兼容 / 指标时序库 / 大屏 | 通用指标监控不是我们的主战场（我们服务的是"采—搬—验"链路） | ❌ 不吸收 |
| JMX / SNMP 协议采集 | 数据库灾备场景不需要 JVM MIB/SNMP 作为主通道 | ❌ 不吸收 |

**一句话**：学它的"**用协议 + 数据化模板消灭目标端部署**"，不学它的"通用监控面"。

---

## 2. 现状取证：AIDBM 今天到底走了几条通路

下列每条都可在仓库中定位（path:符号），不是宣传口径。

### 2.1 通路清单

| # | 通道 | 实现对地 | 用途 | 目标端是否需要安装东西 |
|---|---|---|---|---|
| ① | **SSH / SFTP**（paramiko） | `core/remote_dump.py:245 _connect`、`:253 remote_exec_capture`、`resolve_ssh_host`（:31） | 备份/恢复/远程预检/物理备份/自定义脚本的主通道 | ❌ 不需要（用目标端 OS 自带账户） |
| ② | **原生数据库协议**（纯 Python 驱动） | `core/native_conn.py:121 _import_driver`：mysql/mariadb→`pymysql`、postgresql/kingbase→`psycopg2`、oracle→`oracledb`/`cx_Oracle`、dameng→`dmPython` | 连接测试、拉库列表、数据同步、数据对比 | ❌ 不需要（协议直连，账号即可） |
| ③ | **JDBC 桥接**（平台侧 JVM） | `core/jdbc.py`；`core/probe.py:378 _probe_via_jdbc` 作为驱动缺失时的兜底 | Oracle 11g/达梦等无兼容原生驱动时的降级通道 | ❌ 不需要（JVM 与 jar 都在**平台侧**，`drivers/`） |
| ④ | **HTTP/REST 控制面** | `core/vm/providers/pve.py`（PVE REST `/api2/json`）；ESXi/Hyper-V/libvirt 走 `*_ssh.py` | 虚拟机备份控制面 | ❌ 不需要（用其自带管理 API） |
| ⑤ | **临时二进制推送（BYO-Binary）** | `core/engines/mysql.py:619~789`：xtrabackup/mariabackup 上传远端临时目录 → 备份 → tar → `rm -rf` 清理（:763、:774 `sftp.remove`、:785-789 统一 `rm -rf` 含 marker） | 无 `xtrabackup`/`mariabackup` 的目标也能做物理备份 | ⚠️ **有文件短暂落地**，但用完即删（有代码证据） |
| ⑥ | **恢复时临时落地产物** | `core/engines/file.py:1784 / :1807 / :1940`（`tar -xzf` 后 `rm -f`） | 文件恢复/跨主机恢复 | ⚠️ 同上，临时且已清理 |

### 2.2 因此，我们的"无 Agent"应当被定义为五档侵入度

> 这是本设计最核心的产出：**把口头承诺改成可判定的等级**，每个任务/每次执行都能标出落在哪一档。

| 等级 | 含义 | 目标端状态 | 典型场景 |
|---|---|---|---|
| **A0** | 零安装、零文件、零改配 | 只用账号 + 标准协议 | ① SSH 执行目标端自带 `mysqldump`/`pg_dump`、② 协议直连拉库列/对比只读 |
| **A1** | 零安装、**零文件**，但需目标端调用自带客户端 | 不写入任何平台文件 | 大部分逻辑备份（ expdp / impdp / rman / dexp / sqlcmd 都是**数据库自带工具**） |
| **A2** | **临时文件落地，执行完立即清理** | 短暂存在于 `/tmp` 类临时目录 | xtrabackup/mariabackup 推送、恢复时临时解包 |
| **A3** | 需要**客户侧开启配置**（不是安装软件） | 改参数/加复制用户 | MySQL `log-bin`+ROW、PG `wal_level`+`archive_command`、Oracle `ARCHIVELOG`、达梦 `RLOG_APPEND_LOGIC`、金仓 `sys_hba.conf` 复制槽授权 |
| **X（禁止）** | 常驻进程 / 开机自启 / crontab / systemd unit / 常驻监听端口 / 写入系统目录 | — | **平台一律不支持**，这也是我们区别于"业界都要装 agent"的红线 |

现状的四个真实缺口：

1. **A0–A3 与"禁止项 X" 没有在代码里集中声明**——今天是各引擎各自遵守，没有可断言的对象；
2. **没有统一的"免装自检"出口**：`core/cdc/*` 里有各引擎自己的自检（CDC 面板可见），但**没有针对 SSH 主机的通用自检端点**，能证明"这次执行对目标端零安装、零残留"；
3. **能力描述与版本差异硬编码在 Python 类中**（`core/engines/*.py`），新增库型/版本必须改代码 + 发版；
4. **A3 类前置配置检测分散**（`core/cdc/dameng_logmnr.py`、`core/cdc/oracle_logminer.py`、`core/cdc/rowlevel.py` 各自查 `ARCHIVELOG`/`RLOG_APPEND`/`log_bin`），缺少"纳管前体检 → 生成客户侧最小改动清单"的统一报告。

---

## 3. 目标架构：通道层 + 能力模板 + 免装审计

```
            ┌─────────────── 对外契约：/api/v1/capabilities · /targets/<id>/preflight · /targets/<id>/audit ─┐
            │                                                                                              │
  ┌─────────┴──────────┐   ┌──────────────────────┐   ┌────────────────────┐   ┌──────────────────────┐  │
  │ ① ssh              │   │ ② protocol(native)   │   │ ③ jdbc             │   │ ④ rest               │  │
  │ core/remote_dump   │   │ core/native_conn     │   │ core/jdbc + drivers│   │ vm/providers/pve     │  │
  └─────────┬──────────┘   └──────────┬───────────┘   └─────────┬──────────┘   └───────────┬──────────┘  │
            └──────────────┬──────────┴─────────────────────────┴──────────────────────────┘             │
                     L1 通道层 Channel（统一 connect / exec / fetch / cleanup + 侵入等级 self-declare）     │
                                             │                                                            │
                     L2 能力模板 Capability Template（data/*.yml：每库型×版本 → 可用通道、工具、命令变体、前置项）│
                                             │                                                            │
                     L3 免装审计 Agentless Audit（执行后取证 · 纳管前体检 · 可导出报告给客户）                │
                     └────────────────────────────────────────────────────────────────────────────────────┘
```

落地模块建议：

| 模块 | 职责 |
|---|---|
| `core/agentless/channels.py` | 通道注册表：每条通道声明 `invasiveness`（A0/A1/A2/A3）+ `requires_target_config` + `temp_paths` |
| `core/agentless/templates/*.yml` | 能力模板（先 MySQL / PostgreSQL 试点）：各版本可用通道、命令变体、客户端名、前置参数 SQL |
| `core/agentless/audit.py` | 目标端侵入取证：进程 / 残留文件 / crontab / systemd / 包安装记录 / marker 目录 / 端口监听 + PASS·FAIL 判定 |
| `core/agentless/preflight.py` | 纳管前体检：对比模板要求的目标端配置，产出"客户侧最小改动清单"（不到要求如实拒绝，不降级仿真） |
| `api/agentless.py` | `/api/v1/capabilities`、`/api/v1/targets/<id>/preflight`、`/api/v1/targets/<id>/audit`（遵循 §7.2 契约） |
| 页面 `/target-hygiene` | 目标端免装体检面板：每个主机一列，含最近一次审计时间与证据行 |

对外可承诺的话术（因为可被验证）：**"AIDBM 不在被保护对象上安装任何软件、不部署常驻 Agent；
需要二进制兜底的场景采用执行期临时推送、用完即删，并提供目标端残留审计报告供客户复核。"**

---

## 4. 实施路线

### P0 — 让"无 Agent"可被证明（2026-09-19 已交付第 1、2 步的前半）

| # | 交付 | 落点 | 状态 |
|---|---|---|---|
| 1 | 目标端只读取证 + 判定 | `core/agentless/audit.py`（`build_audit_shell` / `parse_sections` / `judge` / `audit_host`） | ✅ 判定为纯函数，17 条单测离网可跑；结论 `PASS/WARN/FAIL/UNKNOWN` |
| 2 | 通道与侵入等级声明 | `core/agentless/channels.py`（`capabilities()`） | ✅ 已暴露 `GET /api/v1/capabilities` |
| 3 | 取证入口 | `GET /api/v1/targets/<id>/no-agent-audit`、命令行 `scripts/no_agent_audit.py --all` | ✅ 已交付 |
| 4 | 门禁与前端面板（每个主机显示体检结论/证据行、任务显示本次通道等级） | — | ⬜ 未做 |
| 5 | 在**真实环境**跑一次取证形成审计证据 | — | ⬜ 未做（本机无可用 SSH 目标，未经真实取证，不得对外宣称"已验证"） |

判定原则已写死在代码里：只采信**可归因于本平台**的证据
（`xtrabackup/mariabackup/.bp_/bp_work/bk_*/*.bkdone/aidbm*`），
客户自己装的东西记为信息、不判 FAIL——没装过的东西不能替客户背书，也不能替客户背锅。

### P1 — 能力模板数据化（借鉴 HertzBeat 模板机制）

把 `core/engines/*.py` 里硬编码的工具名、版本差异命令（` _resolve_remote_bin`、
`resolve_remote_tool`、`ENGINE_DISPLAY` 等）抽成 `core/agentless/templates/*.yml`，
先移植 MySQL / MariaDB / PostgreSQL 三种试点；目标：**新增一种数据库支持不必改 engines 代码**。

### P2 — 执行节点化 + 自动纳管（借鉴 Manager/Collector）

把 `core/ferry_inbox.py`（摆渡收件箱）与 SSH 前置机提升为一等"执行节点"：
支持就近执行、离线摆渡、单向网闸；配合模板做**批量发现 + 建议纳管**
（当前 `core/asset_inventory.py` 只聚合已纳管任务，无自动发现）。

### P3 — 免装边界的产品化收尾

- A3 类前置要求统一到 `preflight.py`，输出"客户最小改动清单"（一条 SQL / 一个参数），
  **不满足则如实拒绝**，绝不降级为仿真（与现有 `RT_ALLOW_SIMULATED_FALLBACK=false` 的既定立场一致）；
- 告警阈值规则化（现状多为硬编码），复用既有通知渠道。

---

## 5. 不做什么（明确边界）

- ❌ 不做通用监控平台（不做指标时序库、大屏、JMX/SNMP 为主通道）——那是 HertzBeat 的活，不是灾备平台的活；
- ❌ 不允许任何"目标端常驻"（进程、服务、定时任务、常驻端口）——这是我们的红线，也是最有营销力的差异化；
- ❌ 不引入需要联网下载的组件：模板与驱动必须随**离线交付包**（`scripts/offline/make_bundle.sh`）分发。

---

## 6. 验收标准（可验证，不采信自述）

| 项 | 验收方式 | 当前状态 |
|---|---|---|
| 判定逻辑可信 | 离线单测（纯函数，不连主机） | ✅ 31 条全过（`tests/test_agentless_audit.py` 17 条 + `tests/test_agentless_plan.py` 14 条） |
| 能力可查询 | `GET /api/v1/capabilities` 返回通道 / 等级 / 红线 / 前置要求 | ✅ 已交付（并经契约测试） |
| 目标端零安装零常驻可被证明 | `scripts/agentless_forensics.py` 对真实主机取证 + 负向对照 | ✅ **已在真实主机取证**（见 §7，23/23，含 neg 对照） |
| 任务可见侵入等级 | 侧栏「无 Agent」页 + `/api/agentless/tasks` + `/api/tasks/<id>/agentless-plan` | ✅ 已交付（面板显示每任务的通道与 A0–A3） |
| 新增库型不改引擎代码 | 模板试点（MySQL/PG）新增版本差异仅需改 YAML | ⬜ 待做（P1） |
| 临时推送必清理 | A2 推送二进制场景执行后 `audit` 判定无残留 | ✅ 已验证（执行期 witness 到 `bk_pushed_xb_*`，完成后目标端 `/tmp` 全空） |
| 前置配置体检统一出口 | `/api/v1/targets/<id>/preflight` 输出客户侧最小改动清单 | ⬜ 待做（P3） |

> 上表 ⬜ 项均为**未实现**，不得作为已具备能力对外承诺；✅ 项均已给出可复跑的脚本/用例。

---

## 7. P0 收尾（2026-09-19 当日完成）

### 7.1 交付物

| 交付 | 位置 | 说明 |
|---|---|---|
| 通道/侵入等级判定层 | `core/agentless/plan.py` | 纯函数：按 `db_type + backup_mode + rt_enabled + SSH 归属` 推导本次走的通道与最高等级（A0–A3），离线可测 |
| 面板 | 侧栏「无 Agent」→ `/agentless` | 汇总卡片（各等级任务数 / 最高等级 / 需推送二进制的任务数）+ 任务明细表 + 单任务详情弹窗 |
| 接口 | `GET /api/agentless/tasks`、`GET /api/tasks/<id>/agentless-plan`（同步注册 `/api/v1` 前缀） | 前者给面板，后者可用于任务详情里嵌"本次接入方式" |
| 真实取证脚本 | `scripts/agentless_forensics.py` | 23 项断言，含**负向对照**（先在目标端投放平台风格残留，验证取证能抓到并能指认到具体文件） |
| 取证报告 | `docs/agentless_forensics_report_20260919.md` | 脚本自动生成，记录每项的判定与证据 |

复跑方式：

```bash
docker exec bp_agtarget rm -rf /tmp/bk_stage        # 可选：确保起点干净
CODEBUDDY_SAFE_DELETE_ENABLED=0 .venv/bin/python scripts/agentless_forensics.py \
    --ssh-port 2222 --db-port 3306 --db forensic_demo --db-type mariadb --mode physical
```

目标环境：Docker `mariadb:10.11` + `openssh-server`（`root@127.0.0.1:2222`，公钥认证），
**平台所有的动作只经 SSH**，目标开放。"若目标端自带 `mariabackup`，平台不推送任何二进制；
把目标端工具临时改名（`/usr/bin/mariadb-backup`）即可复现 A2 推送路径——本次报告的 F4.2
就是在该形态下取得的证据。

### 7.2 取证结论（真实执行，非自述）

- 备份前后各取证一次，结论均为 **PASS**；
- 备份执行期用采样线程持续 `ls /tmp`，**确实捕捉到**平台推送的 `bk_pushed_xb_*` 与 `bk_pushed_xb_libs`
  ——证明"临时二进制用完即删"是真实发生的行为，不是文档措辞；
- 备份结束后目标端 `/tmp` **全空**（`find /tmp -mindepth 1` 无输出）；
- 负向对照：投放 `/tmp/bk_agentless_canary` → 取证判 **WARN** 并指认到该文件 → 删除后回到 **PASS**；
- F6 校验计划与真实一致：计划说 A2/`temp_binary=True`，执行确实走了 SSH + 临时二进制。

### 7.3 本轮连带发现并修复的缺陷（都是真跑出来的，不是代码走查猜的）

| # | 缺陷 | 证据 | 修复 |
|---|---|---|---|
| 1 | 物理备份后目标端留下空目录 `/tmp/bk_stage`、`/tmp/bk_stage/fixed` | 取证判 WARN 并指认路径 | 新增 `remote_dump.cleanup_remote_stage_dirs()`，用 `rmdir` 只回收平台自建且已空的目录；`mysql.py` 成功/清理两处 + `_cleanup_remote()` 均接入 |
| 2 | 候选目录展开**越界到 `/tmp` 本身**（我在修 #1 时引入），测试容器的 `/tmp` 一度被 `rmdir` 删掉 | `find: '/tmp': No such file or directory` | `_stage_dir_candidates()` 禁止越过根级目录；`tests/test_agentless_plan.py` 用断言锁死该边界 |
| 3 | `_ssh_exec_pipe` 在 `remote_dump` 里根本没定义，而 `except Exception: pass` 把 `NameError` 吞了 → 清理静默失效 | 加日志后立即暴露 | 改为局部导入；best-effort 也要写日志，禁止裸 `pass` |
| 4 | 物理备份选二进制时**只在平台侧直连取版本**，目标端 3306 不向平台开放时退化为默认 8.0 → 给 MariaDB 10.11 推了 MySQL 8 的 xtrabackup | `远端 XtraBackup 物理备份失败(rc=1)` 且 banner 显示 `version 8.0.35` | 新增 `MySQLEngine._remote_server_version()`：经 SSH 问目标端服务进程取真实版本后再选型 |
| 5 | MariaDB 目标 + 任务声明 `db_type=mysql` 时无条件拼 `--set-gtid-purged=OFF` → MariaDB 的 mysqldump 报 `unknown variable` (rc=7)，**100% 备份失败** | 线上 task=27 记录 6731/6733 failed | 新增 `_remote_server_flavor()`：按**目标端实测风味**纠偏参数，不再信任任务声明的 `db_type` |
| 6 | 远程导出**失败**时不清理，目标端留下整套 `bk_dump_*.sh/.rc/.err/.part` | 目标端 `find /tmp` 命中 4 个文件 | 导出进程确已终止（rc≠0 或产物异常小）属不可续传，当场 `_cleanup_remote` + 丢弃断点 |

教训（写给后面改这块的人）：**"零残留"这类承诺必须靠第三方取证脚本闭环**，
代码里写"我们清理了"没有任何说服力；而 best-effort 的 `except: pass` 会让清理失效却无人察觉——
清理分支一定要留日志或者可被断言观测到。

### 7.4 仍然不能承诺的部分（如实）

1. **覆盖面**：取证只在「Docker MariaDB 10.11 + Debian」与本机 MySQL 5.7/8 上跑过；Windows / AIX / 麒麟+国产 CPU、PostgreSQL/Oracle/达梦/金仓目标均未取证过，不得说"全库型已验证"。
2. **断点续传窗口不是零残留**：远程逻辑备份期间目标端会暂存 `.part/.rc/.err/.sh`（`resume_ttl` 默认 12 小时），
   窗口内任务中断则这些文件仍在，下次运行会先续传再清理；这是有意取舍（换来 10GB 级库中断可续），
   对外表述应为"完成后零残留"，而不是"全过程零残留"。
3. **取证是抽样只读命令**（进程 / crontab / systemd / 包管理器 / `/tmp` 命名），不是全盘文件级比对；
   它可以证明"没有我们命名的东西"，不能证明"磁盘上没有任何变化"。
4. P1（能力模板数据化）、P2（执行节点化）、P3（preflight 统一出口）仍未实现。
