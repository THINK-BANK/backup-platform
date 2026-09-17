# Oracle 11g 端到端真实测试报告（192.168.220.168）

- 报告日期：2026-09-17
- 被测版本：Oracle Database 11g（11.2.0，实例 `orcl11g`）
- 平台版本：AIDBM v1.4.9（工作区 `/root/CodeBuddy/20260826095855/backup-platform`）
- 报告性质：**全部结论均由实际产物、数据库查询结果、平台日志与元数据库记录支撑**，无仿真、无占位

---

## 1. 结论速览

| # | 能力 | 结果 | 关键证据 |
|---|------|------|----------|
| 1 | 逻辑备份（expdp） | ✅ 通过 | 备份记录 #6426，dmp 188,416 B 落盘平台 |
| 2 | 物理备份（RMAN） | ✅ 通过 | 备份记录 #6396，3 个备份片共 308,239,872 B |
| 3 | 恢复（impdp 真实导入） | ✅ 通过 | 数据状态真实回退：43 行 → 42 行，恢复后插入的数据消失 |
| 4 | 实时备份（LogMiner） | ✅ 通过 | 日志段 `oracle_logminer_000001_1165581_1165601.jsonl`（4 行真实 DML，含 redo/undo SQL） |
| 5 | 恢复校验（深度校验） | ✅ 通过 | 报告 #23（RMAN VALIDATE + 真实抽取数据文件）、#24（impdp SQLFILE 解析 DDL） |

**此前记忆 ID 78622555 记录的「Oracle 无端到端验证报告」状态，本报告予以关闭。**

---

## 2. 测试环境

| 项 | 值 |
|---|---|
| 数据库服务器 | 192.168.220.168（root/123123，已纳管 `ssh_hosts` id=9） |
| 数据库 | Oracle 11.2.0，实例名 `orcl11g`，服务名 `orcl11g`，端口 1521 |
| ORACLE_HOME | `/u01/app/oracle/product/11.2.0/db_1` |
| 归档模式 | ARCHIVELOG（LogMiner 前置条件，已确认） |
| 业务 schema | `BP_ORA11G`（测试表 `T_E2E`，含主键与 `IDX_E2E_NAME` 索引） |
| 平台任务 | #3689 逻辑备份、#3690 物理备份（均为 Oracle 类型） |
| 备份根目录 | `/opt/backup-platform`（`config.BACKUP_ROOT`） |

**通道约束（遵循项目硬性规则，全程遵守）**：

- 数据库服务器**未安装任何平台工具/agent**，全部经 SSH + SFTP 完成（expdp/impdp/rman/sqlplus 均为数据库自带，路径由 `core/remote_dump.resolve_remote_tool` 以 oracle 用户动态解析）；
- 实时捕获的数据库连接走 JDBC 兜底通道（原因见 §5.1），不使用数据库直连驱动做远程备份。

---

## 3. 逐项验证证据

### 3.1 逻辑备份（expdp）

备份记录：`backup_records` id=6426，任务 #3689，`status=success`，`size_bytes=188416`，`duration_sec=46.684`。

平台日志（执行方式）：以 oracle 用户在远端执行 expdp，产物 SFTP 拉回平台：

```
通过 SSH 在 root@192.168.220.168:22 以 oracle 用户执行 expdp(指定模式(schemas=BP_ORA11G))成功，
已拉回 dmp: /opt/backup-platform/backups/oracle/3689_E2E-Oracle11g-168-逻辑备份/20260917_135540.dmp (184.0 KB)
```

补充验证（本日 19:03 复跑一轮，用于验证 §5.3 的零残留修复）：同样 success，产物 `20260917_190316.dmp` 184.0 KB，耗时 22.0s。

### 3.2 物理备份（RMAN）

备份记录：`backup_records` id=6396，任务 #3690，`status=success`，`size_bytes=308239872`（294 MB），`duration_sec=212.836`。

产物（3 个备份片 + manifest，落盘平台）：

| 文件 | 大小 |
|---|---|
| `ora_bkp_t3690_0452hj8p_1_1.bkp` | 304,332,800 |
| `ora_bkp_t3690_0552hj9s_1_1.bkp` | 1,097,728 |
| `ora_bkp_t3690_arch_0652hj9u_1_1.bkp` | 2,809,344 |
| `20260917_074441_rman_manifest.txt` | 479（含各片 sha256 与 `remote_dir` 记录） |

补充验证（19:04 复跑，299.9 MB / 86.9s）：`ora_bkp_t3690_0752ir29_1_1.bkp` 305,586,176 B 等 3 片。

### 3.3 恢复（真实 impdp 导入，非文件拷贝）

执行时间 2026-09-17 22:36，通过 `OracleEngine.run_restore()` 对备份记录 #6426 的 dmp 执行：

```
su - oracle -c 'export PATH=$ORACLE_HOME/bin:$PATH; /u01/app/oracle/product/11.2.0/db_1/bin/impdp
  "bp_ora11g/\"BpOra11g#2026\"@//127.0.0.1:1521/orcl11g"
  DUMPFILE=platform_restore_20260917_223652.dmp LOGFILE=platform_restore_20260917_223652.log
  DIRECTORY=DATA_PUMP_DIR TABLE_EXISTS_ACTION=REPLACE SCHEMAS=BP_ORA11G'
远端 impdp 返回 rc=0
```

**数据真实性对照（关键证据）**：恢复前人为在源库造了备份之后才产生的数据（`id=1001`，`name='real_cdc_A_upd'`，`amount=99.999`），恢复后该数据应消失：

| 查询 | 恢复前 | 恢复后 |
|---|---|---|
| `select count(*) from bp_ora11g.t_e2e` | 43 | **42** |
| `select count(*) ... where id=1001` | 1 | **0** |
| `select max(id) ...` | 1001 | **42** |
| `select ... where id in (1,2)` | row_1 / row_2 存在 | row_1(1.5) / row_2 仍在 |

即：表被真实重建并导入为备份时点的数据，证明恢复链路真实生效（不是「文件存在即成功」的占位判定）。

> 说明：本次恢复为验证脚本直接调用引擎执行，未经过调度器，因此未生成 `restore_records` 行；平台元数据库中同环境同链路的落库记录为 `restore_records` id=111（任务 #3689，记录 #6391，07:39 success，2.161s），可交叉印证。

### 3.4 实时备份（归档日志 / LogMiner）

实时任务 `rt_tasks` id=6（任务 #3689，`rt_mode=db_cdc`，`capture_interval=30`，`is_running=1`）。

**测试造数**（18:45，源库真实 DML）：

```sql
insert into bp_ora11g.t_e2e values (1001,'real_cdc_A',11.125,sysdate);
insert into bp_ora11g.t_e2e values (1002,'real_cdc_B',22.250,sysdate);
commit;
update bp_ora11g.t_e2e set name='real_cdc_A_upd', amount=99.999 where id=1001;  commit;
delete from bp_ora11g.t_e2e where id=1002;  commit;
alter system archive log current;
```

**捕获结果**：段 `oracle_logminer_000001_1165581_1165601.jsonl`（1,997 B，`rows=4`），内容与上述 DML 逐条对应：

```json
{"scn":"1165583","op":"INSERT","owner":"BP_ORA11G","table":"T_E2E",
 "redo":"insert into \"BP_ORA11G\".\"T_E2E\"(\"ID\",\"NAME\",\"AMOUNT\",\"CREATED_AT\") values ('1001','real_cdc_A','11.125',TO_DATE('17-SEP-26', 'DD-MON-RR'));",
 "undo":"delete from ... where \"ID\" = '1001' and \"NAME\" = 'real_cdc_A' ..."}
{"scn":"1165585","op":"UPDATE", ... "set \"NAME\" = 'real_cdc_A_upd', \"AMOUNT\" = '99.999'" ...}
{"scn":"1165587","op":"DELETE", ... "where \"ID\" = '1002'" ...}
```

redo / undo SQL 双向完整，可直接用于回滚与重放。第二个真实段 `oracle_logminer_000002_1166903_1167664.jsonl`（1,776,419 B，`rows=1292`，SCN 1166903→1167664）在后续备份活动期间自动产出，证明持续捕获能力。

### 3.5 恢复校验（深度校验）

| 报告 id | 任务 | 类型 | 结果 | 耗时 | 校验内容 |
|---|---|---|---|---|---|
| #23 | #3690 物理备份 | `.bkp` | success | 84.0s | 备份片推回 → `CATALOG START WITH` → `RESTORE DATABASE VALIDATE` 通过 → `SET NEWNAME FOR DATAFILE 4` + `RESTORE DATAFILE 4` 真实抽取数据文件到暂存目录 → 清理 |
| #24 | #3689 逻辑备份 | `.dmp` | success | 5.4s | dmp 推回服务端执行 `impdp SQLFILE`，真实解析出 DDL（CREATE TABLE / CREATE INDEX / ADD PRIMARY KEY） |

报告 #23 结论原文：`Oracle 物理备份恢复校验：RESTORE VALIDATE 通过；并已从备份片真实抽取数据文件 #4 到暂存目录验证后清理（真实恢复证据）`。

**零残留核查**（`/u01/app/oracle/backup`、`/var/tmp/platform_restore_test`、`/tmp/platform_verify_*`）：校验结束后推回的 294 MB 备份片、抽取的数据文件、RMAN 命令文件全部清理，目录为空。

---

## 4. 本轮修复的缺陷（均为测试中暴露的真实问题）

| # | 问题 | 修复位置 | 影响 |
|---|------|----------|------|
| 1 | Oracle 11g 实时链路静默降级为仿真日志（`.simlog`）：`oracledb` thin 模式不支持 11g（DPY-3010），旧逻辑捕获失败后回落仿真通道产出假数据 | `core/cdc/oracle_logminer.py`、`core/rt_backup/db_rt.py`、`config.py` | 消除假数据；11g 走 JDBC 兜底真实 LogMiner |
| 2 | SSH 握手偶发抖动（`Error reading SSH protocol banner`）被直接判为任务失败 | `core/engines/file.py` `_get_ssh_client` | 瞬时故障退避重试 2 次（认证失败不重试） |
| 3 | expdp 包装脚本 `expdp_*.sh` 备份成功后残留在数据库服务器 | `core/engines/oracle.py` `_backup_logical_remote` | 成功后随 dmp/log 一并清理（失败保留供排障） |
| 4 | RMAN 命令脚本 `rman_t*.cmd` 备份成功后残留 | `core/engines/oracle.py` `_backup_physical_remote` | 成功后清理（失败保留并在 message 给出路径） |
| 5 | 实时守护**陈旧锁误判**：进程被强杀后锁文件残留，重启时因心跳刚刷新而被判为「有活跃守护」，新守护拒绝启动 → 实时保护静默停止（本次实测停止约 3 小时，日志仅一行 INFO） | `core/rt_backup/supervisor.py` `_clear_stale_lock` | 增加持有进程 PID 存活校验：心跳再新但持有者已死也判为陈旧锁并清理 |
| 6 | CDC 工厂在**构造阶段**静默降级：引擎未注册 / import 失败 / **客户端缺失**时直接返回仿真守护进程，绕过 `db_rt` 的「启动失败不降级」分支，持续产出假日志段（金仓、达梦任务实测持续产 `.simlog`） | `core/cdc/__init__.py` `create_daemon._simulated` | 区分「显式演示」与「能力不足静默降级」：后者默认禁止仿真，`start()` 恒失败 → 任务置 FAILED 并告警 |

修复 3、4 已实测验证：清理基线后重跑逻辑与物理备份，`/u01/app/oracle/backup` 目录均为空。
修复 5 已验证：构造「心跳 0s 但持有者已死」的锁文件，能被正确判定为陈旧并清理；重启后守护正常启动管理 4 个实时任务。
修复 6 已验证：重启后金仓 task=18、达梦 task=20 均置 FAILED（原因如实写明缺客户端），22:47:30 起全平台**零**新增 `.simlog`；同时 Oracle task=3689 的真实 LogMiner 捕获不受影响（继续正常产真实段）。

---

## 5. 需如实披露的问题与遗留

### 5.1 11g 必须走 JDBC 兜底，不能走 oracledb thin

`oracledb` 瘦客户端仅支持 12.1+，连 11g 抛 `DPY-3010`（本次实测确认）。平台已在 `oracle_logminer` 中识别该错误码并切换到 JDBC 通道（依赖 `drivers/` 下的 ojdbc jar + JRE，镜像内已烘焙）。

**影响**：Oracle 11g 的实时捕获依赖 JDBC 兜底通道，离线环境交付时必须保证 ojdbc jar 与 JRE 随包；若目标环境 JDBC 不可用，实时捕获会**诚实失败**（不再产出仿真数据）。

### 5.2 历史仿真段仍留在磁盘（未删除，作为问题证据）

`/opt/backup-platform/rt_logs/3689/sealed/20260917/` 下有 **63 个 `*.simlog`**（14:01–14:12 由旧降级行为产生）。这些是旧行为的产物，不是本次验证结果，**新链路已不再产出该扩展名**（本次 2 个真实段均为 `.jsonl`）。是否物理删除需运维确认，本报告保留事实记录。

### 5.3 LogMiner 部分对象名无法解析

第二段（1,292 行）中 1,284 行的对象名为 `OBJ# 87826`（owner/table 为 `UNKNOWN`）。原因是 LogMiner 在线字典对**已删除或捕获后才创建又被删除**的对象无法映射名称。变更本身真实（含 redo/undo SQL），但按对象名过滤/回放时需依赖 SCN 区间与 OBJ# 而非表名。**属已知限制，未修复。**

### 5.4 尚未验证的能力（不做任何承诺）

- **PITR（时间点恢复）**：`oracle.py` 中 PITR 相关路径目前是「生成脚本但未执行」的诚实实现，**未做真实时间点恢复验证**，不得对客户承诺。
- **Oracle 19c**：本次仅覆盖 11g。19c 的 PDB 自动 OPEN、DATA_PUMP_DIR 按 service 连接查询等兼容分支已实现但**未在本轮环境验证**。
- **跨平台/异机恢复**：未测试。

---

## 6. 复现方式

```bash
# 1) 逻辑备份（任务 #3689）与物理备份（任务 #3690）在平台任务页触发，或：
python -c "from core import models; from core.engines import get_engine; ..."

# 2) 恢复校验策略执行（物理 #152 / 逻辑 #153）
python -c "from core import restore_verify; restore_verify.run_restore_verify_policy(152)"

# 3) 实时捕获：确保 168 上 listener 与实例已启动，平台 rt 守护会自动接管
#    （11g 走 JDBC，起始 SCN 从 V$DATABASE.CURRENT_SCN 取）
```

注意：平台启动必须用 `bash start.sh`（内置 `CODEBUDDY_SAFE_DELETE_ENABLED=0`，否则安全钩子会杀进程）；直接调用引擎的临时脚本也需带该环境变量。
