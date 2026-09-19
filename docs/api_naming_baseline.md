# API 命名存量债务基线

生成方式：`python scripts/api_contract_check.py --update-baseline`
（固化到 `scripts/api_contract_baseline.json`）

最近一次固化：2026-09-19，共 **8 条**（均来自同一类问题：历史资源名单数）。
门禁只拦**新增**违规；下列存量项不阻塞构建，但**不得再增加**，并按计划收敛。

| # | 路径（含 `/api` 与 `/api/v1` 双前缀各一份） | 规则 | 详情 |
|---|---|---|---|
| 1 | `/api/db-migrate` | R2-集合复数 | 末段 `db-migrate` 非复数 |
| 2 | `/api/db-migrate/<int:plan_id>` | R2-集合复数 | 同上 |
| 3 | `/api/migration` | R2-集合复数 | 末段 `migration` 非复数 |
| 4 | `/api/migration/<int:plan_id>` | R2-集合复数 | 同上 |
| 5 | `/api/v1/db-migrate` | R2-集合复数 | 与 1 同源（双注册） |
| 6 | `/api/v1/db-migrate/<int:plan_id>` | R2-集合复数 | 与 2 同源 |
| 7 | `/api/v1/migration` | R2-集合复数 | 与 3 同源 |
| 8 | `/api/v1/migration/<int:plan_id>` | R2-集合复数 | 与 4 同源 |

## 为什么这些还没有被"修掉"

命名统一不能破坏式下线：存量前端（`static/js/*.js`）与外部集成仍在调用历史路径。
当前做法是**规范别名 + 双注册**（`api/contract.py:CANONICAL_ALIASES`、见
`docs/api_conventions.md` §1）：

- `/db-migrate*` → 规范名 `/migration-plans*`
- `/migration*` → 规范名 `/migration-protection-plans*`

同一视图函数注册两个路径，**不存在两套实现漂移**。

## 收敛计划

| 阶段 | 动作 | 退出条件 |
|---|---|---|
| 阶段一（当前） | 规范别名上线，前端新代码一律走规范名；门禁固定这 8 条基线，禁止新增 | `scripts/api_contract_check.py` 输出「本次新增 0 条」 |
| 阶段二 | 前端存量调用（`templates/` + `static/js/`）全部切到规范名 | 全仓 grep 不到 `/api/db-migrate`、`/api/migration` 字面量 |
| 阶段三 | 历史路径加 `Sunset` 头并公告一个版本周期 | 至少一个发布周期无调用告警 |
| 阶段四 | 删除历史路由 + 重新 `--update-baseline` | 基线收敛为 0 条 |

> 每次完成一个阶段的收敛，都要重跑 `--update-baseline` 收紧基线——
> 基线一旦放宽就不再起到"阻止退化"的作用。
