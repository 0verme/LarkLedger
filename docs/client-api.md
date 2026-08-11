# LarkLedger Client API（`/api/v1`）

> v0.9.0 起，`/api/v1` 是**通道无关的稳定客户端契约**（Channel-neutral Client
> API）。Feishu 与 Web Dashboard 不是 API 的前端，而是与它并列的 Adapter——
> 三者最终进入同一个 Application Layer（`ClientApplicationService`）。
> 飞书/Web 可以有渠道专属的文案与交互，但账本规则、权限、预算、隐私、
> Recurring、Goal、Insight 的口径必须一致。

旧路径 `/api/client/v1` 继续可用（同一组 handler 的兼容别名），但新客户端
一律使用 `/api/v1`。

---

## 1. 创建 API Token（Personal Access Token）

Token 必须在**已登录的 Web Dashboard** 中创建（`系统 → API 令牌` 页面），或
通过 Web 接口：

```http
POST /api/web/v1/client-credentials
Cookie: lark_ledger_session=...; X-CSRF-Token: ...
Content-Type: application/json

{ "name": "esp32-button", "scopes": ["ledger:read", "ledger:write"], "expires_at": null }
```

响应（**明文只出现这一次**）：

```json
{
  "id": "…",
  "name": "esp32-button",
  "token_prefix": "llv1_AbCdEfGh",
  "scopes": ["ledger:read", "ledger:write"],
  "token": "llv1_9f8a…完整明文…"
}
```

- 服务端**只保存 SHA-256 摘要**，不保存明文；关闭页面后无法再查看。
- 建议设置 `expires_at`（ISO 8601）。
- 可随时在 Web 撤销；撤销立即生效（旧 token 返回 401）。
- **不要把 token 提交到 Git，也不要写进公开的硬件固件仓库。**

Token 格式：`llv1_` + 高熵随机串（`secrets.token_urlsafe`，非可预测 ID）。

### Scopes

第一版只有三个：

| scope | 含义 |
| --- | --- |
| `ledger:read` | 查询账本、账户、交易、预算、周期规则、目标、总览、洞察 |
| `ledger:write` | 记账 / 修改 / 删除、转账、预算设置 |
| `pending:write` | 确认 / 取消待确认单 |

Token scope 只能**缩小**用户权限，不能扩大：

```text
最终有效权限 = 用户本身权限 ∩ token scope
```

---

## 2. Bearer 认证

```http
Authorization: Bearer llv1_xxxxx
```

认证失败（缺失 / 无效 / 过期 / 已撤销）统一返回：

```json
{ "error": { "code": "authentication_required", "message": "valid bearer credential required", "request_id": "…" } }
```

状态码一律 `401`，**不区分 token 是否曾经存在**（防枚举）。

---

## 3. 选择 Ledger

API 使用 **X-LarkLedger-Ledger-ID 之外的显式上下文**：Bearer 凭证在创建时绑定
一个"当前账本"，客户端可以查询和切换：

```http
GET  /api/v1/ledgers                  # 列出我可访问的账本
GET  /api/v1/ledgers/{ledger_id}      # 单个账本（无权 → 404）
POST /api/v1/ledgers/{ledger_id}/select   # 把该账本设为 token 的当前账本
```

之后所有资源接口都在**当前账本**上下文中执行；服务端校验 actor 是账本成员，
不信任客户端传入的任何 ledger 标识。跨账本资源一律 `404`（不泄漏存在性）。

---

## 4. 幂等（Idempotency-Key）

所有写操作（POST / PATCH / DELETE）**必须**携带：

```http
POST /api/v1/transactions
Idempotency-Key: abc123
```

- 唯一性范围：`actor + ledger + operation + idempotency_key`。
- 相同 key + 相同请求体重试 → 返回第一次的业务结果，**不重复写入**（响应带
  `"replayed": true`）。
- 相同 key + 不同请求体 → `409 conflict`，不会静默执行。

幂等记录持久化在 PostgreSQL（`client_idempotency_records`），不是内存态；
并发重试由数据库唯一约束保证只写入一次。

---

## 5. 创建交易（Transaction）

```http
POST /api/v1/transactions
Authorization: Bearer llv1_xxx
Idempotency-Key: breakfast-20260814

{
  "type": "expense",
  "amount": "18.00",
  "currency": "CNY",
  "category": "餐饮",
  "note": "早餐",
  "account_id": "…",              // 可选；不填用默认账户
  "paid_by_user_id": "…",         // 可选；家庭账本中指定付款人
  "occurred_at": "2026-08-14T08:00:00+08:00"
}
```

- 金额使用 **Decimal-as-string**，禁止 float；`amount > 0`。
- `direction` 取值 `expense` / `income`；`category` 1–64 字符。
- 与飞书「早餐18」、Web 记账进入**同一个** `ClientApplicationService`，
  产生语义一致的 `LedgerEntry`。差异只允许在认证、输入解析、响应展示、
  渠道能力。

创建成功：

```json
{
  "message": "已记录 #XXXX 支出 ¥18.00 · 餐饮（早餐） · 账户：默认账户",
  "resource": { "id": "…uuid…", "short_id": "XXXX", "account_id": "…" },
  "replayed": false
}
```

交易别名：`/api/v1/transactions` 与 `/api/v1/entries` 等价。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/v1/transactions` | 分页列表（`page` / `page_size`，默认 25，上限 100） |
| POST | `/api/v1/transactions` | 创建（幂等） |
| GET | `/api/v1/transactions/{id}` | 详情（`id` 接受 UUID 或 short_id） |
| PATCH | `/api/v1/transactions/{id}` | 修改（幂等，需 `expected_updated_at`） |
| DELETE | `/api/v1/transactions/{id}` | 删除（幂等，需 `expected_updated_at`） |
| POST | `/api/v1/entries/{short_id}/restore` | 恢复（幂等） |

---

## 6. 常用查询

```http
GET /api/v1/me
GET /api/v1/accounts                     # 当前账本账户
POST /api/v1/accounts                    # 创建账户
GET /api/v1/transfers                    # 转账
POST /api/v1/transfers                   # 创建转账（幂等）
GET /api/v1/budgets?period=2026-08       # 预算概览
GET /api/v1/recurring-rules              # 周期规则
GET /api/v1/goals                        # 目标（含确定性进度）
GET /api/v1/overview                     # 家庭/个人总览
GET /api/v1/insights                     # 确定性洞察
```

所有读接口继续应用 `PrivacyService` 与账本隔离：private 账户的数据、
引用 private 账户的目标/洞察，对非授权成员一律 404 / 不可见，不泄漏存在性。

---

## 7. 错误处理

统一 envelope：

```json
{
  "error": {
    "code": "not_found",
    "message": "Resource not found",
    "request_id": "…"
  }
}
```

HTTP 语义（稳定契约，不在不同 endpoint 漂移）：

| 状态码 | 含义 |
| --- | --- |
| 200 | 成功 |
| 201 | 创建成功 |
| 204 | 成功无内容（撤销等） |
| 400 | 输入校验失败（注：本 API 校验失败用 422） |
| 401 | 认证失败 / 过期 / 已撤销 |
| 403 | scope 不足（token 缩权） |
| 404 | 不存在 / 无权访问（不区分） |
| 409 | 冲突 / 幂等 key 冲突 / 版本冲突 |
| 422 | 请求体验证失败 |
| 503 | 幂等进行中 / 临时故障 |

错误码：`authentication_required`、`permission_denied`、`resource_not_found`、
`validation_error`、`conflict`、`expired`、`rate_limited`、`temporary_failure`。

`request_id` 可用于日志关联排错；日志中最多记录 token 前缀与 id，**绝不
记录完整 token**。

---

## 8. 完整示例（Quick Start）

```bash
# 1. Web 创建 API Token（见上文），保存返回的明文
TOKEN="llv1_…"

# 2. 查询我的身份
curl -s http://ledger.example/api/v1/me -H "Authorization: Bearer $TOKEN"

# 3. 查询账本
curl -s http://ledger.example/api/v1/ledgers -H "Authorization: Bearer $TOKEN"

# 4. 选择账本（token 的当前账本）
LEDGER="…"
curl -s -X POST http://ledger.example/api/v1/ledgers/$LEDGER/select \
  -H "Authorization: Bearer $TOKEN"

# 5. 记账（带 Idempotency-Key）
curl -s -X POST http://ledger.example/api/v1/transactions \
  -H "Authorization: Bearer $TOKEN" -H "Idempotency-Key: breakfast-1" \
  -H "Content-Type: application/json" \
  -d '{"type":"expense","amount":"18.00","currency":"CNY","category":"餐饮","note":"早餐","occurred_at":"2026-08-14T08:00:00+08:00"}'

# 6. 重试同一请求（同 key）→ replayed: true，不重复记账
# 7. 查询总览
curl -s http://ledger.example/api/v1/overview -H "Authorization: Bearer $TOKEN"
```

---

## 9. 稳定性承诺

`/api/v1` 自 v0.9.0 起是稳定契约：

- 不随便改字段名、错误结构、金额类型（Decimal-as-string）或日期格式（ISO 8601）。
- 新增字段优先为可选字段；破坏性变更走新版本路径（`/api/v2`）。
- CI 有 OpenAPI 契约测试与架构守护测试防止回归。
- OpenAPI schema：运行实例 `/openapi.json`（含 `clientBearer` security scheme）。

## 10. 硬件 / CLI 兼容性

未来 ESP32 / Raspberry Pi / CLI 客户端只需：

```text
HTTPS + Bearer token + JSON + Idempotency-Key
```

不依赖浏览器 cookie、CSRF 或飞书事件。本 API 面向受信任的自托管客户端；
公网 SaaS 场景再引入正式 gateway / rate limit。
