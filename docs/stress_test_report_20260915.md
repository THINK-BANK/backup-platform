# AIDBM 全面压力测试报告（2026-09-15）

## 1. 测试目标

验证平台在**批量任务 + 全接口 + 数据返回**三个维度下的稳定性与正确性：

1. 同时发起 **1000+ 备份任务**不报错（HTTP 无 5xx、无连接异常、记录不丢不重）；
2. **所有接口**（自动发现的全部 GET 接口 + 主要写接口）并发压测，统计 QPS / P50 / P95 / P99 / 状态码分布；
3. **数据返回校验**：字段完整性、CRUD 往返一致、关联一致性、分页筛选、异常入参返回 4xx 而非 5xx。

## 2. 测试环境与工具

| 项 | 值 |
|---|---|
| 平台 | 本机 192.168.220.140:8080（werkzeug 内置服务器，多线程） |
| 元数据库 | SQLite（WAL） |
| 压测工具 | `scripts/stress_test_full.py`（本次新增，Python + requests + 线程池） |
| 压测负载 | 轻量自定义脚本备份（真实走完整备份链路：脚本执行 → 产物拉回 → size/sha256 → 落记录） |

用法：

```bash
python scripts/stress_test_full.py                                  # 全量 1000 任务
python scripts/stress_test_full.py --tasks 1200 --concurrency 150    # 更大并发
python scripts/stress_test_full.py --tasks 60 --api-req 2            # 快速冒烟
python scripts/stress_test_full.py --skip-backup                     # 只压接口
python scripts/stress_test_full.py --report /tmp/stress.json         # 结果落盘(JSON)
```

阶段划分：

- **A 批量建任务**：并发创建 N 个备份任务（1000+）
- **B 批量触发备份**：并发对 N 个任务发起"立即备份"，统计 success/failed
- **B2 落库对账**：直连元库核对每个触发都有记录，且 success 记录 size>0、有 sha256、产物文件存在
- **C 全接口压测**：自动从 `app.url_map` 发现全部 GET 接口（动态参数用真实 id 填充），并发请求
- **D 数据返回校验**：18 项断言（结构/往返/关联/分页/异常入参/并发读一致）
- **E 混合读写**：持续 N 秒读压测 + 20% 写操作混杂
- **F 写接口并发**：创建-删除配对压测任务/恢复校验策略，并混入非法负载验证不会 500

## 3. 压测结果（最终全量：1200 任务 / 并发 150）

| 阶段 | 请求数 | QPS | P95 | 5xx / 连接异常 | 结论 |
|---|---|---|---|---|---|
| A 批量建任务 | 1200 | 159.5 | 1297.9 ms | **0** | OK |
| B 批量触发备份 | 1200 | 13.4 | 11331.3 ms | **0** | OK |
| B2 落库对账 | 1200 条记录 | - | - | success 1200 / failed 0 | OK |
| C 全接口（112 个 GET × 5） | 560 | 81.8 | 1271.2 ms | **0** | OK |
| E 混合读写（30s） | 2646 | 84.2 | 1273.6 ms | **0** | OK |
| F 写接口并发 | 720 | 208.4 | 586.1 ms | **0** | OK |
| D 数据返回校验 | 18 项 | - | - | - | **18/18 通过** |

- 平台进程资源峰值：RSS **360 MB**、线程数 **172**、CPU 累计 381s（多核）
- 压测期间平台 ERROR 日志：**0 条**
- 总耗时 153.9s，全程无 5xx、无 SQLite "database is locked"、无请求超时

> 说明：C/E 阶段出现的 4xx（404/400）是压测脚本用**已被删除的任务 id**、以及目标库不存在时 `list-tables` 的 400，属于预期响应，不计入失败。

## 4. 发现并修复的缺陷（压测暴露，均已修复）

| # | 问题 | 影响 | 修复 |
|---|---|---|---|
| 1 | 内置 Web 服务未开启多线程（`app.run` 缺 `threaded`） | 批量提交/触发时请求排队，高并发直接超时 | `run.py` 增加 `threaded=config.WEB_THREADED`（默认开，可用环境变量关闭） |
| 2 | SQLite 连接未设置 busy 超时 | 多线程并发写元库时抛 `database is locked` | `core/db.py get_conn()` 增加 `timeout=30` + `PRAGMA busy_timeout=30000` + `synchronous=NORMAL` |
| 3 | **调度器 reload 并发竞态**：`reload_scheduler()` 遍历 job 快照后逐个 `remove_job()`，多线程同时 reload 时抛 `JobLookupError: No job by the id of ... was found` | 并发建/删任务必现 **HTTP 500**（压测 60 并发即 11 次 500） | `core/scheduler.py`：序号 + 执行锁做**请求合并**（并发 N 次 reload 只真正重建 1~2 次），并对 `remove_job` 容错 |
| 4 | `/api/tasks/{id}/list-tables` 目标库连不上时返回 **500** | 服务端异常误报（实为配置/环境问题），污染监控与压测 | 改为返回 **400 + error 文案**（前端可提示） |
| 5 | `/api/records` 不支持 `limit`，固定返回 500 条 | 无法分页，大数据量下拉取过重 | 支持 `limit`（上限 500 保护），前端/脚本可分页 |

修复前后对比（同一压测规模 60 任务/30 并发）：

| 阶段 | 修复前 5xx | 修复后 5xx |
|---|---|---|
| A 批量建任务 | 11 | **0** |
| C 全接口 | 2 | **0** |
| E 混合读写 | 16 | **0** |

## 5. 性能观察与调优建议

1. **备份执行吞吐约 13~15 个/秒**（轻量脚本）。瓶颈在单次备份的固定开销（元库写入串行锁、日志、产物复制与 sha256），而非并发度：把 `max_concurrent_backups` 从 2 调到 16，300 任务压测 QPS 仍为 14.5，**无提升**。因此不建议盲目放大并发，真实环境应按目标数据库承载能力设置。
2. **生产部署建议用 gunicorn**：内置 werkzeug 服务器在高并发下线程数无上限（压测峰值 172 线程），推荐 `gunicorn -w 4 --threads 8 -b 0.0.0.0:8080 run:app`。
3. **客户端连接池**：压测客户端 `pool_maxsize` 必须 ≥ 并发数，否则会出现 "Connection pool is full" 的客户端侧瓶颈（脚本已设为 512）。
4. **批量操作建议合并 reload**：`reload_scheduler()` 已做合并，但业务层若批量建 1000 个任务，仍建议批量结束后只触发一次调度重载（当前合并机制已把 N 次降为 1~2 次）。

## 6. 回归测试

压测修复后回归（与既有基线一致，零新增失败）：

```
tests/test_rt_journal.py  tests/test_rt_scheduler.py  tests/test_record_display.py
tests/test_link_sources_contract.py  tests/test_custom_backup.py  tests/test_backup_restore_all.py

18 failed, 124 passed  # 18 failed 全部为 test_backup_restore_all.py 的既有环境基线
                       # （Oracle/PG/Redis/MySQL 增量等依赖外部数据库，与本次改动无关）
```

## 7. 产物

- 压测工具：`scripts/stress_test_full.py`
- 结果数据（JSON，含逐接口延迟与状态码）：`docs/stress_report_20260915.json`
