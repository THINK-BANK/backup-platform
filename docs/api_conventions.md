# AIDBM 对外接口契约（统一规范）

> 生效版本：v1.4.11（2026-09-19）。本文件是**对外契约**，修改必须走评审：
> 每条规则都有对应代码落点与自动化门禁，不是纸面规范。

| 规则 | 代码落点 | 门禁 |
|---|---|---|
| 版本前缀 | `api/contract.py`（`V1_PREFIX`） | `tests/test_api_contract.py` |
| 三段式错误 | `core/error_codes.py` + `api/contract.py:normalize_response` | `tests/test_api_contract.py` |
| 统一分页 | `api/contract.py:pagination_args/list_response` | `tests/test_api_contract.py` |
| 命名规范 | `scripts/api_contract_check.py` | 提交前钩子 + CI |
| OpenAPI | `api/openapi.py` → `/api/v1/openapi.json` | `tests/test_api_contract.py` |

---

## 1. 资源命名

规则代码：`scripts/api_contract_check.py:naming_violations`

- **R1 格式**：路径段必须小写 `kebab-case`，禁止大写、下划线、尾随斜杠；
  允许末段带扩展名（如 `/openapi.json`）。
- **R2 集合复数**：`GET` 集合端点末段应为复数（`GET /api/v1/records`）。
  单例/聚合资源登记在 `SINGULAR_ALLOWED` 白名单或加入 `ACTION_VERBS`；
  实例级子资源（`/plugins/<pid>/state`）只查格式，不强制复数。
- **R3 版本覆盖**：任何新增的 `/api` 路径必须能在 `/api/v1` 命中同一视图——
  由同一蓝图双注册保证（`app.py`：同一 `api_bp` 以两个前缀注册），
  因此不存在"规范路径与兼容路径两套实现漂移"的问题。

历史命名**不做破坏性下线**，而是注册**规范别名**：

| 历史路径 | 规范路径 | 别名登记 |
|---|---|---|
| `/api/db-migrate[/<id>]` | `/api/v1/migration-plans[/<id>]` | `api/contract.py:CANONICAL_ALIASES` |
| `/api/migration[/<id>]` | `/api/v1/migration-protection-plans[/<id>]` | 同上 |
| `/api/custom-script/template` | `/api/v1/custom-scripts/template` | 同上 |

存量债务明细与收敛计划见 `docs/api_naming_baseline.md`。

**新增接口清单**：① 资源用复数名词；② 不允许新出现 `/api/<动词>` 形式的动词端点，
动作一律用子资源（`POST /records/<id>/verify`）或方法语义；③ 先跑一次
`scripts/api_contract_check.py`，门禁只拦**新增**违规。

## 2. 分页

- 推荐：`?page=1&size=50`（`page` 从 1 起）。
- 兼容：`?limit=50&offset=0`，自动换算为 `page/size`（`limit/offset` 同时出现时
  以 `page/size` 为准）。
- 上限：`size ≤ MAX_PAGE_SIZE`（500，可按接口收紧），越界自动收敛而非报错。
- 响应信封（**规范路径 `/api/v1` 恒返回**）：

```json
{"items": [ ... ], "total": 128, "page": 1, "size": 50, "has_more": true}
```

- 兼容路径 `/api` 默认保持历史响应形状（裸数组 / `{"success":true,"data":[...]}`），
  显式 `?envelope=1` 或带 `page/size` 参数时才返回信封。

已接入分页的接口：`GET /records`、`/tasks`、`/sync-tasks`、`/restore-verify-reports`、
`/ai-alert-predictions`（如 `/api/ai-alert/predictions`）、`/cdc/<id>/events`、`/logs/operations`。

## 3. 错误契约（三段式）

```json
{
  "success": false,
  "code": "AIDBM-1004",
  "message": "记录不存在",
  "details": {"record_id": 42},
  "error": "记录不存在"
}
```

- `code`：**稳定业务码**，与 HTTP 状态解耦，是集成方唯一的判定依据（禁止文本匹配）。
- `message`：面向人的一句话原因（中文）。
- `details`：机器可读上下文，错误响应恒为非 null（无上下文时为 `{}`）。
- `error` / `success`：历史字段，保留以保证老前端零改动。

域划分（`core/error_codes.py:ERROR_TABLE`）：

| 域 | 含义 | 典型码 |
|---|---|---|
| 1xxx | 请求、鉴权、权限 | 1000 请求无效 / 1001 参数校验 / 1002 未登录 / 1003 无权限 / 1004 资源不存在 / 1006 冲突 |
| 2xxx | 连接与数据库 | 2001 连接失败 / 2002 库不存在 / 2003 客户端缺失 |
| 3xxx | 备份与恢复 | 3001 备份失败 / 3002 恢复失败 / 3003 空间不足 / 3004 产物无效 |
| 4xxx | 存储、主机与通道 | 4001 SSH 失败 / 4002 存储失败 / 4003 路径非法 / 4004 未纳管 SSH 主机 |
| 5xxx | 服务端 | 5001 内部错误 / 5002 未实现 / 5003 依赖缺失 |
| 9xxx | 未知 | 9000 |

原则：**错误码只增不改**（已发布码含义冻结）；未显式指定业务码的响应由
`code_for_status()` 按 HTTP 状态补兜底码，因此**任何** `/api` 错误响应都带 `code`。
存量 `jsonify({"error": ...})` 写法无需逐个改写，归一化钩子自动升级；
需要精确业务码时在视图里调用 `contract.mark_error_code("AIDBM-3001")`。

## 4. 版本策略

- `/api/v1` = 规范路径；`/api` = 兼容路径（响应带
  `Deprecation: true`、`Link: </api/v1/...>; rel="successor-version"`、
  `Warning: 299 - "Deprecated API prefix /api, use /api/v1"`）。
- 所有响应带 `X-API-Version: v1`。
- 破坏性变更流程：新增 `/api/v2`（双注册）→ 文档标注 `/api/v1` 弃用 → 至少一个大版本的
  兼容期后才下线。当前不做 URI 版本之外的鉴权差异。

## 5. 文档

- 机器可读：`GET /api/v1/openapi.json`（OpenAPI 3.0.3，含 `x-success-envelope`
  与 `x-error-codes` 扩展）。
- 人可读：`GET /api/docs`（离线页面，零 CDN 依赖，直接渲染上述 spec；
  docs 页面本身不在 spec 的 249 条路由内）。
- 服务元数据：`GET /api/v1/meta` 返回 `platform{name,version}` 与
  `api{version,canonical_prefix,deprecated_prefix,spec,docs,error_contract}`。

## 6. 门禁与执行方式

```bash
# 命名 + 版本覆盖检查（存量 8 条债务在基线内不阻塞，新增违规即失败）
.venv/bin/python scripts/api_contract_check.py

# 契约单测（双前缀、错误码、分页信封、OpenAPI）
.venv/bin/python -m pytest tests/test_api_contract.py -q

# 提交前钩子（安装：bash scripts/install_git_hooks.sh）
git commit ...        # 自动跑上面两项 + 快速测试集
```

CI：`.github/workflows/ci.yml` 的 `contract` job 在每次 push/PR 执行，
结论被镜像构建流水线依赖。

## 7. 尚未覆盖（诚实清单）

- 页面路由（非 `/api`）不参与命名检查：`/file_backup` 为遗留下划线路径，未收敛；
- WebSocket / 流式接口未纳入本契约（当前无此类端点）；
- `@success` 业务成功信封（成功响应的统一 `data` 包装）未做，成功体仍按历史形状返回——
  这是为避免一次性改造 249 条路由带来的破坏性变更，留待 v2 随新前缀一起切换。
