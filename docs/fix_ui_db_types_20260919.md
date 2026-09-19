# 数据库类型下拉缺陷修复与实测记录（2026-09-19）

## 1. 用户反馈的现象

| 页面 | 现象（用户截图） |
|---|---|
| 数据迁移 → 新建迁移计划 | 源/目标「类型」下拉**只有 2 项**（MySQL/MariaDB、PostgreSQL） |
| 数据同步 → 新建同步任务 | 源/目标「数据库类型」下拉**完全是空的**，无法选择 |

## 2. 根因

### 2.1 同步页下拉为空：META 竞态

- 类型清单来自 `BKP.META.db_types`，而它**只在 `app.js` 的 `DOMContentLoaded` 里
  `await /api/meta` 之后**才被填充。
- `static/js/sync.js` 在**同一个 DOMContentLoaded** 里**同步**调用
  `BKP.fillDbTypeSelect(...)`，永远抢在 `/api/meta` 返回之前 → 拿到空数组 →
  `innerHTML` 被写成空串。

对照实验（`dukpy` 真实执行修复前的源码）：

```
修复前 bkp-core.js : 竞态(META 未就绪)时下拉 option 数 = 0
修复后 bkp-core.js : 7（自动回落到 /api/meta 后重新填充）
```

### 2.2 迁移页只有 2 项：模板硬编码

`templates/migration.html` 的两个 `<select>` 里直接写死了
`<option value="mysql">` 与 `<option value="postgresql">`，
MariaDB / 金仓 / 达梦 / Oracle / SQL Server 这些**已支持迁移**的类型根本不出现。

### 2.3 连带发现的同类问题

| # | 问题 | 影响 |
|---|---|---|
| 1 | 同步任务的类型下拉用 `db_types`（备份引擎全集） | 会列出 redis/mongodb/neo4j/opengauss/file 等**不能同步**的类型 |
| 2 | 迁移页目标类型若不在 MySQL/MariaDB，一律"不支持自动建库" | PostgreSQL/金仓目标必须手工建库，否则预检查不过 |
| 3 | `_source_stats` 只硬编码 MySQL 与 PostgreSQL | Oracle/达梦/SQL Server **源**被统计成 0 张表 → 预检查判"源库没有可迁移的业务表"→ 迁移根本进不去 |
| 4 | 创建接口不校验类型 | 伪造/误填的类型会落库，跑到一半才失败 |

## 3. 修复

### 前端

| 文件 | 改动 |
|---|---|
| `api/system.py` | 抽出 `build_meta()`（`/api/meta` 与页面注入共用，避免两处漂移）；新增 `sync_types`（= 同步插件注册表 `registry.available()`） |
| `app.py` | 新增 `@app.context_processor` 注入 `bkp_meta` |
| `templates/base.html` | 在 `bkp-core.js` **之前**内联 `window.__BKP_META__`（首屏即有类型清单，消除竞态） |
| `static/js/bkp-core.js` | 合并首屏 META；新增 `BKP.ensureMeta()`（并发去重、失败不抛）；`fillDbTypeSelect` 在 META 未就绪时**自愈**（拉一次再填），支持 `typesKey` 指定清单 |
| `static/js/sync.js` | 初始化改为 `await BKP.ensureMeta()` 后再填充；默认端口改用 `META.default_ports`（PG 5432 / Oracle 1521 / 达梦 5236 / 金仓 54321…）并做类型↔端口联动（仅替换未被用户改过的端口） |
| `templates/migration.html` | 两个 select 改为空（由 JS 动态填充，不再硬编码）；目标库标签加 id 供联动 |
| `static/js/app.js` | 迁移计划：`dmFillTypeSelects()` 用 `sync_types` 填充 + 端口联动 + 「不存在将自动创建 / 需已存在」提示联动；打开弹窗时兜底补齐；`openSyncModal` 同步任务同样改用 `sync_types` |

### 后端

| 文件 | 改动 |
|---|---|
| `core/sync/plugins/base.py` | 新增 `registry.available()`（已注册库型清单，注册即出现在界面） |
| `core/db_migrate.py` | ① `create_plan` 校验 `src_/tgt_db_type` 必须在注册表内（否则 400，附支持清单）；② `_source_stats` 通用化：MySQL/MariaDB、PG/金仓、Oracle/达梦、SQL Server（走 JDBC 兜底）各自方言 SQL，**统计失败返回 note 不阻断**；③ `_ensure_target_db` 扩展到 PostgreSQL/金仓（连维护库 `CREATE DATABASE`，兼容金仓 `pg_database`/`sys_database` 两种字典表），Oracle/达梦/SQL Server 明确不擅自建库；④ 预检查里"统计不到"不再被判成"源库没有表" |
| `api/sync.py` | 创建同步任务时校验类型属于同步插件注册表（400 + 支持清单） |

## 4. 实测证据（真实环境，非自述）

环境：本机 MySQL 5.7（3307）、本机 PostgreSQL（5785）、Docker `mariadb:10.11`（172.17.0.3:3306）。

### 4.1 源对象统计（覆盖多库型）

| 源 | 结果 |
|---|---|
| MySQL 3307 `types_src` | `{'tables': 2, 'rows': 5}`（与手工建表插入的 2 表 5 行一致） |
| MariaDB 容器 `forensic_demo` | `{'tables': 1, 'rows': 2}` |
| PostgreSQL 5785 `postgres` | `{'tables': 1, 'rows': 0}` |
| 不支持类型（redis） | `{'tables': 0, 'rows': 0, 'note': '暂不支持统计该源类型（redis）'}` |

### 4.2 目标库自动建库

```
MariaDB 目标 : (True, '目标库 types_dst 已自动创建')
PostgreSQL 目标: (True, '目标库 types_pg_dst 已自动创建') → 再次调用 (True, '目标库已存在')
Oracle 目标   : (False, '目标类型 oracle 不支持自动建库，请先手工创建目标库')
```

### 4.3 迁移计划全链路（真实执行）

- 计划 #18：`mysql@127.0.0.1:3307/types_src` → `mariadb@172.17.0.3:3306/types_dst2`
  - 预检查：源对象统计「表 2 张 / 约 5 行」、目标库自动创建、连通性全部 True
  - 迁移：`全库迁移：2 张表，读取 5 行，写入 5 行，错误 0 行`
  - 校验：`全部 2 张表行数一致`，状态 `completed`
- 计划 #19（utf8mb4 干净源）：`mysql types_utf8_src`（3 行，含中文/引号/emoji/decimal）→ 容器 MariaDB
  - `读取 3 行，写入 3 行，错误 0 行`，状态 `completed`
  - 源库与目标库**逐值比对一致**：
    ```
    源  : ((1, '北京-测试', Decimal('123.456'), '中文备注一'),
           (2, '上海/数据', Decimal('999.001'), '含特殊字符 引号\'与双引号"'),
           (3, 'GuangZhou-ABC', Decimal('12.500'), 'emoji 与符号: 数据✓'))
    目标: 完全相同 → 逐值一致: True
    ```

### 4.4 类型校验

```
POST /api/db-migrate  src_db_type=redis
 → 400  src 端数据库类型 redis 不支持迁移（已支持: dameng, kingbase, mariadb, mysql, oracle, postgresql, sqlserver）

POST /api/sync-tasks  src_db_type=redis
 → 400  源端数据库类型 redis 不支持同步（已支持: …）
```

### 4.5 前端（渲染产物 + 真实执行前端源码）

- `/migration`、`/sync` 渲染结果均含首屏 `window.__BKP_META__`，
  `sync_types = ['dameng','kingbase','mariadb','mysql','oracle','postgresql','sqlserver']`；
- 迁移页两个 select 已无硬编码 `<option>`；
- `dukpy` 真实执行 `bkp-core.js` 的 `ensureMeta` / `fillDbTypeSelect` 源码：
  - 竞态路径（META 未就绪）→ 填充 **7** 项并确认回落请求 `/api/meta` 至少 1 次；
  - 首屏注入路径 → **0** 次额外请求，选项顺序与 `sync_types` 完全一致；
  - `exclude` / 默认 `db_types` 行为正确。
- 负向对照（证明测试有效）：
  - 手动去掉 `sync.js` 的 `await BKP.ensureMeta()` → 对应用例 **FAILED**，恢复后 PASS；
  - 用修复前的 `bkp-core.js` 源码在同一 harness 下执行 → 竞态时 **0** 项（复现空下拉）。

回归用例：`tests/test_ui_db_types.py`（19 条，覆盖 JS 执行 / 渲染产物 / 源码防回退 / 后端契约）。

## 5. 如实说明（未覆盖项）

1. **未做像素级浏览器点击验证**：本机为 glibc 2.17 的老系统，Playwright 的 node 驱动
   要求 GLIBC ≥ 2.27，真实浏览器跑不起来。替代手段是"真实渲染产物断言 + 真实执行前端
   源码断言"（见 4.5），可复现、可回归；但**没有截图类证据**。
2. Oracle / 达梦 / SQL Server 作为**源**的统计 SQL 只有单测覆盖（断言走了各自方言 SQL），
   未连真实实例（168/137 当前离线）；金仓自动建库也未在真实实例验证（已兼容两种字典表名）。
3. 迁移页现在列出的类型 = 同步插件注册表里的类型；**Oracle/达梦/SQL Server 作为目标**
   仍要求目标库已存在（平台不擅自创建表空间/文件组）。
4. 迁移的行数统计取自系统统计信息（采样值），迁移结果以 verify 阶段逐表实时 `COUNT(*)` 为准。
