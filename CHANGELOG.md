# Changelog

All notable changes to LarkLedger are documented in this file. The project follows [Semantic Versioning](https://semver.org/) while remaining in the `0.x` Alpha stage.

## [0.9.0] - 2026-08-12

### Added (P34 — Application Service Boundary; P35 — Channel-Neutral Client API; P36 — Adapter Contract)

- **正式通道无关 Client API（P35）**：稳定契约 `/api/v1`（`/api/client/v1` 为同一组 handler 的兼容别名）。新增 `GET /ledgers/{ledger_id}`、`GET /recurring-rules`、`GET /goals`、`GET /overview`、`GET /insights`；`/transactions` 与 `/entries` 等价（list / create / get / patch / delete，`id` 接受 UUID 或短 ID）。错误 envelope 增加 `request_id`（可关联日志排错）；OpenAPI 声明 `clientBearer` security scheme；写请求继续强制 `Idempotency-Key`。
- **API Token 管理（P35）**：Web Dashboard「系统 → API 令牌」页（创建 / 一次性明文展示 / 撤销），复用 `llv1_` Bearer（只存 SHA-256 摘要、可过期、scope 只缩权、撤销立即失效）。
- **架构守护（P34 / P36）**：`tests/architecture/` AST 守护——Core/Application 不得 import `fastapi`、Feishu 客户端、渠道路由或 token 传输；Domain 不得 import Application；`RequestContext` 不携带渠道密钥；Domain 错误不泄漏 `HTTPException`。依赖方向固定为 Adapter → Application → Domain → Core。
- **Adapter Contract Suite（P36）**：`tests/contracts/` 的 C01–C08 用单一 `CanonicalExpectation` 事实源证明 Feishu / Web（`ClientApplicationService`）/ Client API 对同一业务事实产生一致 Domain Result——expense 创建、家庭 payer（created_by ≠ paid_by）、private 账户三端不可见、预算口径、Recurring 确认保留 payer、Goal 进度、Insights 结构化 metric、重复投递 exactly-once（API 幂等 key + Feishu 事件幂等）。
- **OpenAPI 契约测试**：`tests/contracts/test_openapi_v1.py` 守护 `/api/v1` 的 §22 必需面、双前缀等价、Bearer 声明、稳定错误 schema 与中性写 DTO。
- **CI**：新增独立 `contracts` job（architecture guard + adapter contracts + OpenAPI）。

### Changed

- `ledger_entries.user_open_id` 标记 **deprecated**（v0.9.0）：仅保留用于历史行与安全回滚，新业务依赖 `created_by_user_id` / `paid_by_user_id` + `channel_identities`；删除安排后续 destructive migration。
- README / docs/architecture / docs/help / docs/roadmap 更新「飞书是 Adapter」分层原则；新增 [Client API 文档](docs/client-api.md)（创建 Token、Bearer、选账本、幂等、交易、查询、错误处理、Quick Start、硬件兼容）。

## [0.8.0] - 2026-08-13

### Added (P33 — Financial Goals; P33 — Deterministic Insights)

- **财务目标（P33 Goals）**：`financial_goals` + `goal_account_bindings` 两张新表（Alembic migration `20260813_0026`）。`FinancialGoal` 只保存用户定义的计划（名称 / 描述 / `savings` 类型 / 目标金额 / 币种 / 可选目标日期 / 用户管理的 `active|completed|archived` 状态），**没有** `current_amount` / `progress_percent` 列——`GoalProgressService` 从绑定账户的实时余额确定性重算进度（`current` / `remaining` / `progress_percent`），记账、删账、恢复、转账自动触发重算，目标永远不会保存第二套余额真相。DB 约束：`target_amount > 0`、`goal_type IN ('savings')`、`status` 枚举、`(ledger_id, id)` 唯一；绑定用 `(ledger_id, goal_id)` 与 `(ledger_id, account_id)` 复合外键保证同 Ledger 内绑定（跨账本绑定由结构排除），`(goal_id, account_id)` 唯一禁止重复绑定，删除目标级联其绑定但从不触碰账户 / 账目 / 转账。
- **目标合法性校验**：Savings Goal 只允许绑定现金 / 资产账户（拒绝 liability），目标币种必须与账户币种一致（不做自动换算），多账户绑定余额求和，全部确定性拒绝而非 AI 判断。
- **目标隐私（P33 核心修复）**：`GoalProgressService` 在计算进度前先做授权与可见性检查——引用任何 private 账户的目标对其他成员完全不可见（list 排除 / get / progress / update 一律 404），且错误类型与「目标不存在」不可区分，**杜绝存在性侧信道**。
- **确定性 Forecast**：过去 90 天净储蓄率 → `monthly_saving_rate` → `estimated_months_to_goal` / `projected_shortfall`，覆盖正 / 零 / 负 / 历史不足四种情况，AI 不参与计算。
- **飞书确定性命令**：`我的目标` / `目标` / `查看目标` / `财务目标` / `目标进度` 走 `MessageProcessor → ClientApplicationService → GoalService`，不经过 AI intent interpreter；Web `/goals` 页面（创建 / 编辑 / 进度 / 归档 / 完成 / 删除）同源。
- **确定性洞察（P33 Insights）**：`InsightService` + `InsightPolicy` 单点阈值。I01 支出变化（本月 vs 近 3 月平均，阈值 30% + 最低绝对额 + 最少历史天数）；I02 预算风险（usage > elapsed + 15% margin，统一 `InsightPolicy`）；I03 未来 30 天周期支出（只统计 active / visible，按币种分组，不跨币种求和，排除 paused / disabled / private 不可见）；I04 目标进度 / 预计缺口（deadline soon / shortfall / reached）。全部由确定性规则计算，数据不足时返回 `[]` 不制造伪洞察。
- **AI 边界**：AI 只可选改写洞察解释文案（`InsightExplanationService` 输入仅含结构化 type / summary / metric），不访问数据库、不计算任何财务事实；AI 失败自动回退确定性摘要，且 AI Provider **不是** readiness 必需依赖。
- **隐私侧信道防护（P33 核心）**：Insights / Overview / Budget / Member Stats 全部复用 `PrivacyService` 与 v0.7.0 相同口径——private 账户的余额、支出、分类、金额、目标、进度不会通过任何 totals / 类别聚合 / 成员统计 / revision 变化泄漏给其他成员；private Entry 的 create / delete / restore 对 B 的 Insights 无可观察差异。

### Changed

- Web Dashboard 现为 v0.8.0 版：`/goals` 目标卡片（创建 / 编辑 / 进度 / 归档 / 完成 / 删除）、Overview「值得关注」洞察卡片、`/insights` 空状态。
- 包版本 / `__version__` / 前端 package 版本 / `.env.example` 镜像标签全部升至 `0.8.0`。

### Removed / Clarified

- 明确边界：**洞察不是金融顾问**——不提供投资、股票、理财、贷款、税务建议，不做任何自动资金操作；目标不是虚拟账户 / 资金池（创建 / 修改 / 删除从不触碰账户、账目或转账）。

## [0.7.0] - 2026-08-12

### Added (P30 — Household Contribution; P31 — Household Overview; P32 — Account Privacy)

- **家庭共享账本（P30）**：多个真实用户共同记账一个 `household` Ledger。`ledger_entries` 分离「谁记账」与「谁付钱」：`created_by_user_id`（录入人）与 `paid_by_user_id`（实际付款人）独立存在，默认付款人为录入人，也可在录入时指定其他成员。成员解析服务按 household alias / 显示名 / open_id / UUID 确定性解析付款人引用，解析失败时给出受控提示而不误入 AI 记账。Alembic migration `20260811_0024`。
- **成员贡献统计**：`GET /ledgers/{ledger_id}/members/stats` 按 `paid_by_user_id` 聚合已确认账目（转账排除），而不是按录入人统计；预算支出对每位成员的共享支出只计一次。
- **家庭概览（P31）**：`HouseholdOverviewService` 提供确定性家庭总览——期间收入 / 支出 / 净额（转账排除、待确认周期账目不计入）、预算进度（隐私过滤后）、账户余额汇总（隐私过滤）、成员贡献（按付款人）、Top 分类、即将到期的周期规则、最近交易。Web `GET /api/web/v1/overview` 与 `/overview` 页面（家庭总览导航）；飞书确定性命令 `概览` / `家庭概览` / `家庭开销` 走 `MessageProcessor → HouseholdOverviewService` 真实路径。
- **账户隐私（P32）**：`accounts.visibility`（`shared` / `private`，默认 `shared`）与 `owner_user_id`（CHECK 约束），Alembic migration `20260812_0025`；**降级保护**：存在 private 账户时 downgrade 被拒绝。`PrivacyService` 在共享账本中执行账户可见范围过滤：private 账户的余额、交易、预算影响、周期规则 / Occurrence / Pending / 提醒数据对其他成员完全不可见（列表 / 详情 / 概览 / 成员统计 / 飞书命令，未授权访问返回 404），对 personal ledger 全部为 no-op。
- **隐私覆盖范围**：accounts（list / get / get_default 404 语义）、entries（Web list / detail / dashboard、analytics、预算支出、飞书 list / detail / summary / report / export / mutations）、transfers（双方账户均可见才可见）、recurring（list / get / locked 校验、创建 / 更新校验、名称解析）、pendings（Web list / detail + dashboard 计数）、member stats 与 household overview。
- **Web 隐私 UI**：`ClientAccount.visibility` + `owner_user_id`，`POST /accounts/{id}/visibility`（仅户主 / 账户 owner 可操作），Accounts 页面「共享 / 私人」徽标 + 切换 + 创建对话框。
- **Shared recurring 付款人冻结**：规则创建时把 `paid_by_user_id` 冻结进生成的 Pending，任何活跃成员可确认 shared 周期 Pending，确认人不改变真正付款人。

### Changed

- Web Dashboard 现为 v0.7.0 版（家庭总览页、共享 / 私人账户 UI、visibility 控制）。
- 飞书命令文档与帮助补充 payer 引用（如 `B 买菜 120` → created_by=A / paid_by=B）与隐私语义。

### Removed / Clarified

- 明确产品边界：**不是 AA / Splitwise**（无分摊、结算、债务关系、人均拆账）；**不是复式记账**；**不是企业财务**（无审计链、审批流、多币种结算、财务报告义务）。

## [0.6.0] - 2026-08-10

### Added (P29 — Recurring Rules)

- **Recurring rules** add `recurring_rules` and `recurring_occurrences` tables so a known future income / expense (每月房租、每年保险、每周健身房) schedules deterministically: `monthly` / `yearly` / `weekly` with an `anchor_day` that keeps the day-of-month stable across month boundaries (a 31st rule schedules Feb 28 then back to Mar 31). Alembic migration `20260810_0023`.
- **Confirmation-first, never auto-posted**: when a rule comes due the Recurring Worker generates exactly one confirmation pending (freezing the rule's ledger, account, amount, currency, category and planned date) plus a proactive Feishu reminder card, then advances `next_occurrence`. Only a confirmed pending becomes a ledger entry. The unique `(rule_id, occurrence_date)` database constraint is the idempotency authority, so concurrent workers / retries / crashes can never produce two pendings for the same period.
- **Rule lifecycle**: create / list / get / update / pause / resume / disable / skip. Pause stops generation; resume skips straight to the next valid future period without back-filling history; skip records a `skipped` occurrence (cancelling a still-pending confirmation if one exists) and advances the schedule; disable archives the rule while keeping its historical pendings / transactions intact. Updating a rule only affects future occurrences — already-generated pendings keep their frozen content.
- **Confirmed transactions are exactly-once**: confirming a recurring pending transitions its occurrence to `confirmed` and links the created entry; duplicate confirms are idempotent no-ops. Ledger / account validation re-runs at confirmation time (archived or cross-ledger accounts fail safely) and the frozen account is used even after the user switches default account or ledger.
- **Budget contract**: rules and their pendings never count toward budget; only a confirmed expense entry contributes actual spending, and income never enters the expense budget.
- **Feishu deterministic commands**: `每月8号房租3500`, `每年6月15日保险2000`, `每周健身房100`, `我的周期账单`, `暂停房租`, `恢复房租`, `跳过房租` / `跳过本期` — recurring-shaped near-misses get guidance instead of reaching the AI interpreter.
- **Web Recurring Rules page**: list with name / type / amount / account / frequency / next date / status / waiting-confirmation badge, plus create / edit / pause / resume / skip / disable via the `GET/POST/PATCH /recurring-rules` and `POST /recurring-rules/{id}/pause|resume|skip|disable` endpoints.

### Changed

- 账本管理命令接受常用同义词（`新建账本` / `创建新账本` / `切换到账本` / `设置默认账本` / `查看账本` / `我的账本`）；输入明显是想管理账本但语法未匹配时，回复账本命令用法提示而不是通用帮助。

### Added (P28 — Budget 2.0)

- **Period-scoped ledger budgets** add a `budgets` table keyed by `(ledger_id, period, category)` where `category IS NULL` is the ledger's total limit for the month and a non-empty category is a category limit. Periods are explicit first-day-of-month dates, never derived from timestamps. The legacy recurring `category_budgets` table is untouched and continues to act as the monthly default; a period row wins for its month. Alembic migration `20260809_0022`.
- **Plan-vs-actual progress overview** (`BudgetService`) recomputes actual spending from the live ledger entries in one `GROUP BY` query, so transfers can never be counted as budget usage and delete / restore / revision amount / revision category all resolve from the current facts. Budget limits and transaction facts stay separate — no mutable `used` counter to drift.
- **Budget status derivation** (`normal` / `warning` / `exceeded` / `none`) with a fixed 80% warning threshold, `remaining` / `usage_rate`, and a category with no budget reported by its record's absence (actual spend still shown) rather than a zero limit.
- **Application API and REST**: `get_budget_overview`, `set_total_budget`, `set_category_budget`, `delete_budget` on the unified `ClientApplicationService`; Web and Client `GET /budgets?period=`, `PUT`/`DELETE /budgets/{category}` and new `PUT`/`DELETE /budgets/total` with optional `period` (defaults to the current month).
- **Feishu** adds the `set_total_budget` action ("设置本月预算 12000") and `list_budgets` now reports the period total line; existing recurring category budget commands are unchanged.
- **Web Budgets page**: month navigation, total-budget hero with status, per-category cards with status chips / progress / remaining, rows for categories that spent without a budget, and inline set / edit / delete for total and category limits. The dashboard hero usage rate comes from the unified overview.

## [0.5.0] - 2026-08-09

### Added (v0.5.0 — Ledger-scoped Accounts & Transfers; P26/P27)

- **Ledger-scoped Account domain** adds `accounts` (`cash` / `asset` / `liability`) with per-ledger default and archived status, opening balances, rename/set-default/archive lifecycle, and ledger-scoped uniqueness. Historical `ledger_entries` are losslessly backfilled to each ledger's default account. Alembic migration `20260809_0019`.
- **Transfers and balance ledger** adds `transfers` and `transfer_revisions`, ledger-scoped composite foreign keys, distinct-account and positive-amount checks, reversal, amount revision, and derived per-account balance / asset / liability / net-worth queries. Transfers are never counted as income or expense. Alembic migration `20260809_0020`.
- **Entry account binding everywhere**: Web / Client entry list, detail, dashboard recents and revision snapshots now return the ledger-scoped `account_id` / `account_name`; CSV export adds an `account_name` column. `PATCH /entries/{id}` accepts `account_id` with same-ledger validation, archived / cross-ledger rejection, and `extra="forbid"` on the update schema so unknown fields are never silently ignored.
- **Frozen pending account targets**: non-transfer pending commands freeze a single `account_id` (create / update / batch) in addition to `ledger_id`; confirming after a ledger or default-account switch still writes to the frozen target. Transfer pendings freeze both sides plus the transfer id. Alembic migration `20260809_0021`.
- **Feishu account capabilities**: `account_hint` on create / update_last / update_entry, new `list_accounts` and `assets` actions, deterministic account / balance / asset commands, server-side hint resolution (ambiguous / archived / cross-ledger names are rejected with a clear reply), and account names in bookkeeping replies.
- **Web Account and Transfer pages**: account list / create / rename / set-default / archive / per-account balance / asset summary, transfer list / create / detail with audit / reverse, entry account column, account selectors in entry create/edit, and a `POST /entries` + `GET /transfers` + transfer-revision Web API surface.

### Added

- **Unified Client API** adds the transport-neutral `ClientApplicationService`,
  versioned `/api/client/v1` identity, ledger, household, entry, budget,
  analytics, report, CSV and Pending contracts, plus stable machine errors.
- Revocable, scoped bearer credentials persist only SHA-256 digests and track
  creation, last use, expiry and revocation; Web issuance remains protected by
  Dashboard Session + CSRF.
- Durable `Idempotency-Key` snapshots isolate actor, operation and ledger,
  detect payload conflicts, replay structured results, and are cleaned by the
  existing bounded Cleanup Worker.
- Alembic migration `20260809_0018` adds client credentials, idempotency records
  and minimal security audits without modifying existing identity or finance data.

- **Household Spaces MVP** adds owner/member households, persisted invitations,
  deterministic Feishu commands, authenticated Web management, and one
  automatically provisioned `household_shared` ledger per household.
- Alembic migration `20260809_0017` adds households, memberships, invitations,
  shared-ledger linkage, partial unique indexes, checks, and lossless 0016 data
  compatibility.

- **Personal multi-ledger MVP** adds an independent ledger management service,
  deterministic Feishu commands, authenticated Web APIs, and a minimal
  Dashboard selector for create/list/current/select/default/rename workflows.
- Alembic migration `20260809_0016` persists per-channel current-ledger state,
  normalizes names with owner-scoped uniqueness, and backfills existing
  identities without removing legacy compatibility columns.
- **Identity and Ledger foundation** introduces platform-independent `users`,
  `channel_identities`, and `ledgers`, plus a deterministic `RequestContext`
  carrying the actor, target ledger, and source channel.
- Alembic migration `20260809_0015` creates one internal user, Feishu identity,
  and default personal ledger for every existing domain user, then backfills
  ledger ownership without deleting or rewriting legacy `user_open_id` values.

### Changed

- Ledger authorization is centralized: personal ledgers require their owner,
  while household ledgers require an active membership. Invalid persisted
  Feishu or Dashboard selections fall back to the default personal ledger.
- Frozen pending confirmations re-authorize their creation-time ledger before
  execution; leaving or removal never redirects a pending write to another ledger.

- Ledger short IDs, category budgets, active media fingerprints, analytics,
  reports, exports, revisions, and pending confirmations are ledger-scoped.
  Pending confirmation keeps its creation-time ledger across later switches.
- New Dashboard sessions start from the user default ledger, while each active
  session and Feishu identity retains its own explicit current selection.
- New ledger entries, budgets, revisions, pending confirmations, and Dashboard
  sessions persist internal user/ledger identifiers. Ledger and Web queries use
  `ledger_id` as their primary scope, with a nullable legacy fallback retained
  for the expand-migration release.
- Feishu processing and Web Dashboard sessions now resolve external identities
  before entering the ledger core. Event and Reply Outbox routing identifiers
  remain transport metadata and are intentionally unchanged.

## [0.4.0] - 2026-08-08

### Added (v0.4.0 — Web Dashboard / 可视化账本与运维控制台; P11)

- **Optional Web Dashboard** built with React, TypeScript, Vite, React Router, TanStack Query, and lightweight SVG charts. FastAPI serves the production build from the same application image; no Node.js production server or second backend is introduced.
- **Feishu OAuth and server-side sessions** map the signed-in identity to `user_open_id`. Sessions use revocable PostgreSQL records, HttpOnly Secure cookies, explicit TTL, OAuth state validation, CSRF protection, redirect-origin validation, fixation resistance, and logout revocation. Roles remain deliberately small: `USER` and environment-configured `ADMIN`.
- **Ledger management** provides a financial overview, server-side pagination and filtering, entry detail, revision timeline, service-layer update, soft-delete, and restore. Every operation forces the authenticated session user and preserves existing revision and transaction rules.
- **Pending console** lists frozen confirmation previews and reuses the existing locked, idempotent confirmation/cancellation path. Web confirmation never re-calls AI and cannot bypass expiry, ownership, or concurrent-execution guards.
- **Analytics and finance views** provide backend-aggregated trend, category, and monthly data, budgets, existing reports, and constrained CSV downloads with the same user isolation, formula-injection defense, 5,000-row limit, 5 MiB limit, and UTF-8 BOM as the bot path.
- **Administrator operations console** exposes redacted Event and Reply Outbox metadata, Dead lists, result replay, guarded event replay with dry-run and explicit second confirmation, readiness-derived health, and a read-only secret-safe configuration view. Payloads, reply bodies, blobs, credentials, database URLs, and worker nonces are never returned.
- **Dashboard session migration** `20260808_0014` adds revocable `dashboard_sessions` without changing ledger, pending, Event, or Outbox state machines.

### Changed

- The production Dockerfile now builds Vite assets in a Node stage and copies only `dist` into the Python runtime image. `DASHBOARD_ENABLED=false` leaves the original bot, workers, migrations, and API paths independent of the Dashboard.
- CI now includes a locked frontend job (`npm ci`, ESLint, TypeScript, unit tests, production build) alongside Python 3.11/3.12, PostgreSQL integration, documentation, and security jobs. Tag releases continue to build multi-architecture `linux/amd64` and `linux/arm64` images.
- Documentation now covers Dashboard OAuth, HTTPS and trusted-proxy deployment, administrator mapping, session security, disabled mode, upgrades, architecture, and production image delivery.

### Fixed

- `撤销 #C-XXXXX` now deterministically cancels the pending confirmation instead of falling through to AI and potentially undoing the latest ledger entry.
- Exact duplicate visual messages now share a privacy-safe SHA-256 fingerprint while a confirmation is active, preventing repeated deliveries from creating multiple confirmation codes or cards. Terminal confirmations do not block an intentional later resend.
- Create commands whose JSON provider returns `occurred_at: null` now use the request timestamp, preserving the established “book it now” behavior for voice and other undated input while retaining strict validation for every other field.

### Security

- Web API tests cover OAuth state, session expiry and logout, CSRF, user/admin authorization, IDOR resistance, cross-user short-ID collisions, pending ownership, replay permissions, export isolation, and safe error responses.
- Dashboard startup fails when enabled without a strong session secret, a valid HTTPS production origin, or required Feishu OAuth credentials; access tokens and long-lived authentication material are never exposed to browser storage.

### Validation

- The complete P11 series passed Python lint/type/unit coverage gates, real PostgreSQL integration tests, Alembic single-head checks, frontend lint/type/unit/build, documentation links, dependency audit, and production Docker builds.
- Real NAS acceptance verified OAuth login, user/admin roles, Dashboard overview, ledger detail/edit/revision/delete/restore, voice pending confirmation, CSV download, analytics, budgets, reports, health, Event, and Outbox views while the Feishu WebSocket bot remained connected. Dashboard-disabled bot operation was also verified. Event Replay Dry Run was not executed because the production database contained no Dead Event; no synthetic failure row was inserted.

## [0.3.0] - 2026-08-07

### Added (v0.3.0 — High-risk Confirmation / 高风险确认; P07)

- **Risk routing (`risky_only`)** decides per write: simple, unambiguous single-entry text writes go straight to the ledger; image / voice / batch / likely-duplicate writes first create a **pending confirmation** and wait for the user. Read, query, and short-ID mutation commands are never confirmed.
- **`pending_commands`** table (migration `20260806_0012`) stores a **frozen** `ParsedCommand` (`payload_json`) plus a frozen user preview (`preview_json`) — confirming never re-calls AI or re-recognizes media. `confirmation_code` is user-unique, never reused, case-insensitive, and parsed by regex only.
- **Confirmation IDs** `#C-A83F2` (a `C`-prefixed five-character Crockford code) are distinct from ledger short IDs `#XXXXX` and never confused by parsing.
- **Text confirmation commands**: `确认 #C-A83F2`, `取消 #C-A83F2`, `查看待确认` are parsed deterministically before the AI interpreter and are the always-available fallback.
- **Card confirmation buttons**: the preview card carries 确认 / 取消 buttons; a new `card.action.trigger` callback (webhook branch + long-connection registration) verifies the operator's user, the confirmation code, and current status. Double clicks are idempotent (row lock + status check).
- **Confirmation execution** runs the frozen command through `LedgerService` + reply outbox **in one transaction** under `SELECT FOR UPDATE`: two concurrent confirms execute exactly once, confirm vs cancel has one winner, expired pendings never execute, and a crash between commit and event status converges without re-running business.
- **Duplicate detection**: same user + direction + amount + currency + near `occurred_at` + (same category or source type) with a note-similarity check flags a likely duplicate; the preview shows the existing short ID and the user decides.
- **Expiry and retention** reuse the P06d Cleanup Worker: due `pending` rows become `expired`; terminal rows (executed / cancelled / expired / failed) are deleted after `pending_retention_days` (default 7).
- **Operator CLI**: `python -m lark_ledger.admin list-pending` and `expire-pending` show safe aggregates and run the expiry sweep.

### Changed

- Event `succeeded` for a high-risk message now means "pending confirmation created and preview outbox written", not "ledger written"; the confirmation itself is a later event.
- `MessageProcessor` gains risk routing and a pending confirmation store; both `WORKER_ENABLED` modes share the same outbox delivery primitives.

### Security

- Pending payloads and previews are treated like ledger data; logs and the operator CLI output only the confirmation code, status, risk reason, and aggregate counts — never the frozen payload, OCR text, transcripts, or `open_id`.

### Known limitations (v0.3.0)

- Confirmation codes are not security credentials: they only select the requesting user's own pendings, and every action re-verifies `user_open_id`.
- No multi-level approval, no shared/multi-user confirmation, no web admin; v0.3.0 is not a full finance approval system.
- Card action callbacks require the Feishu interactive-card capability; text commands are the guaranteed fallback.
- This is not a complete financial approval flow: there is no multi-level approval, multi-person approval, web admin, shared ledger, Redis-backed queue, or multi-IM integration.
- Some transient pre-notifications remain best-effort. Outside Feishu's reply-UUID idempotency window, an extreme crash timing can duplicate a reply, but never re-execute the accounting business operation.

## [0.2.1] - 2026-08-06

### Added (v0.2.1 — guarded manual event replay; P06e)

- `python -m lark_ledger.admin replay-event` provides a controlled operator path for event replay. It is dry-run by default and requires non-empty, length-bounded `--operator` and `--reason`; `--execute` is the only way to change state.
- A locked execute-time preflight permits only `dead`, `failed`, or expired-lease `processing` events with a supported payload, consistent source message, no outbox, no source ledger result, and a replay-safety marker proving the current transactional-outbox contract. Ambiguous historical rows are refused instead of guessed.
- Accepted replay atomically writes an append-only `event_replay_audits` row and resets the event to `received`. `attempt_count` starts a new bounded automatic retry window at zero; `manual_replay_count` and the audit's previous attempt count preserve history.
- Any existing Outbox refuses business replay and directs the operator to result replay. Existing source ledger rows refuse replay with a duplicate-business-risk outcome. CLI output and logs contain only safe statuses, counts, and error codes—not payload, operator reason, user financial text, or identifiers from the stored message.
- `business_committed_at` is written atomically with business + outbox and outlives outbox retention: even after cleanup deletes the outbox, the automatic crash-window pre-check and manual replay both refuse to re-run business from this durable marker. Migration `20260806_0011` backfills it only for historical events that have an outbox.
- Migration `20260806_0011` adds replay metadata and the audit table. Historical events intentionally keep an unproven safety marker and therefore require investigation rather than automatic requeue.

### Added (v0.2.1 — terminal retention cleanup; P06d)

- A lifespan-managed Cleanup Worker deletes only terminal delivery records in bounded, short transactions: `processed_events` in `succeeded` / `legacy_succeeded` / `dead`, and `reply_outbox` in `sent` / `dead`. Non-terminal rows, active leases, ledger entries, and ledger revisions are never selected.
- Safe defaults retain successful events and sent replies for 30 days and dead events / replies for 90 days. Retention is configurable but must be at least one day; disabling requires explicit `LARK_LEDGER_CLEANUP_ENABLED=false`, so zero can never mean "delete everything".
- Cleanup runs outbox-before-event, uses status/time indexes plus `FOR UPDATE SKIP LOCKED`, and refuses to delete an event while any associated outbox audit remains. Multiple instances can clean concurrently; each batch is independently committed and repeatable.
- Cleanup failure is non-critical: a failed sweep is retried later, `/healthz` is unaffected, and an exited Cleanup Worker appears as `warning` in `/readyz` without blocking the core event/reply path. Logs include only cleanup kind, cutoff, count, elapsed time, and safe error type.
- Migration `20260806_0010` adds four status/time cleanup indexes without rewriting or deleting rows; downgrade removes only those indexes.

### Added (v0.2.1 — readiness and worker health; P06c)

- `GET /readyz` returns HTTP 200 only when PostgreSQL accepts `SELECT 1`, the database revision matches the single Alembic code head, the application is not shutting down, enabled Event / Reply Workers are running, and the WebSocket receiver is active when WebSocket mode is selected. Webhook mode and explicitly disabled workers remain valid configurations.
- `GET /healthz` keeps its compatible, database-independent liveness response. Neither probe calls Feishu, AI, external DNS, or the internet, and readiness never runs migrations or scans ledger / delivery tables.
- Event Worker, Reply Worker, and WebSocket consumer tasks now expose redacted lifecycle snapshots. Completion callbacks retrieve unexpected task exceptions (preventing unobserved-task warnings), log only safe error types, and make readiness fail without exposing stack traces, credentials, payloads, user IDs, message IDs, reply contents, or worker nonces.

### Added (v0.2.1 — reply delivery worker; P06b)

- **Reply delivery worker** (`ReplyWorker`): a background asyncio task started and stopped by the FastAPI lifespan. It claims committed `reply_outbox` rows in one transaction with `SELECT ... FOR UPDATE SKIP LOCKED`, writes `sending` / `lease_owner` / `lease_expires_at`, increments `attempt_count` (each entry into `sending` counts one attempt), commits, then uploads / sends via `ReplyDeliverer` and records lease-guarded outcomes. The database is the only queue; no Redis / Celery / RQ / Kafka / RabbitMQ. A lost wakeup only delays delivery by one poll interval.
- **Mode switch:** `LARK_LEDGER_REPLY_WORKER_ENABLED` (default `true`) makes the processor commit business + outbox and signal the worker instead of sending directly. `false` restores the compatible synchronous path, which claims each freshly committed row with the **same** lease-guarded primitives (`claim_by_id` → `ReplyDeliverer`) — no send path bypasses the outbox guards. The two modes never run at once.
- **Outbox lease semantics:** only the current `lease_owner` may write an outcome (guarded by `status='sending' AND lease_owner=<owner>`); an expired lease lets another worker reclaim the row (`attempt_count` increments again) and a stale worker can never overwrite the new owner's state. Outcome updates clear the lease. Default 300 s; no renewal in this version.
- **Reply retry with exponential backoff:** transient failures (network / timeout / 408 / 429 / 5xx / transient upload failures) are recorded as `failed` with `next_attempt_at = now + min(base × 2^(attempt-1), max)` (defaults 2 s / 3600 s) plus ~10% jitter. Unknown errors are conservatively retried up to `reply_max_attempts`.
- **Reply dead-lettering:** permanent errors (unsupported `payload_version`, unknown `reply_type`, missing routing field, `payload_json` contract corruption, missing blob, size / checksum mismatch, non-408/429 4xx) or an exhausted attempt budget move the row to `dead` (default `reply_max_attempts=3`, first attempt counts as 1), clearing the lease and retaining the redacted error summary. A single bad row never kills the worker sweep.
- **Per-event reply ordering:** replies within one event are delivered in `sequence` order — a later reply waits while an earlier one is pending / retrying / in flight (enforced by a `NOT EXISTS` claim predicate, including under concurrency), and an earlier `dead` row allows later replies to proceed independently instead of blocking forever. `(event_id, sequence)` index added.
- **Staged upload + send:** a file upload persists its `remote_file_key` and a report-image upload its `remote_image_key` while the worker still holds the lease, so a retry after a message-send failure reuses the upload instead of re-uploading. A report-card image upload failure degrades to the stored text-only card.
- **Feishu `uuid` idempotency:** every reply carries the outbox row id as the Feishu reply API's `uuid` idempotency key (≤50 chars). Within the 1-hour window a re-send after a "sent but not marked" crash is deduplicated by Feishu and the existing remote `message_id` is returned; it is also persisted to `remote_message_id`. Beyond 1 hour an extreme duplicate-reply window remains (disclosed) — it can never cause duplicate business execution or double bookkeeping.
- **Result replay (internal):** `OutboxReplayService` resets `failed` / `dead` rows back to `pending` (clearing backoff, lease, and error summary) so the worker re-sends the exact persisted payload. It never re-calls AI, never re-runs a business command, never regenerates CSV, and never re-renders a report. This is an internal, testable capability; no user-facing command is exposed yet.
- **New settings:** `LARK_LEDGER_REPLY_WORKER_ENABLED`, `REPLY_WORKER_POLL_INTERVAL_SECONDS` (1.0), `REPLY_WORKER_BATCH_SIZE` (10), `REPLY_MAX_ATTEMPTS` (3), `REPLY_LEASE_SECONDS` (300), `REPLY_RETRY_BASE_SECONDS` (2.0), `REPLY_RETRY_MAX_SECONDS` (3600).
- **Migration `20260806_0009`:** adds `reply_outbox.remote_message_id`, `remote_file_key`, `remote_image_key` (all nullable, no backfill) and the `ix_outbox_event_sequence` index. Downgrade drops them (audit/keys lost; undelivered intents are not lost).

### Added (v0.2.1 — transactional reply outbox; P06a)

- **Transactional Outbox:** new `reply_outbox` table (migration `20260806_0008`) stores durable, self-contained Feishu reply intents written in the **same transaction** as the ledger change they confirm. A successful business commit now guarantees a reply intent exists for later delivery or compensation.
- **Atomic business + reply:** `MessageProcessor` runs `LedgerService` with `commit_changes=False` (flush only), builds the reply intents from its result, and commits business + outbox together. Business failure and outbox failure roll back together; no business is ever committed without a matching outbox row.
- **Self-contained reply payloads:** text replies persist the final text verbatim; CSV exports and report images persist raw bytes in `payload_blob` (with `size` and `sha256`), so a later worker (P06b) can deliver after a container restart without re-calling AI, re-querying the ledger, or reopening a temporary file. `(event_id, reply_type)` is unique; `sequence` orders multi-message replies.
- **New `succeeded` semantics:** an event `succeeded` now means "business handled and reply intents durably written to the outbox", **not** "Feishu received the reply". Feishu send outcomes live on the outbox rows.
- **Crash-window recovery:** if business + outbox commit but the event status update is lost, the re-claimed event checks for an existing outbox row, skips business (no duplicate entries / replies), and converges to `succeeded` — the outbox pre-check replaces `IntegrityError → dead` as the normal recovery path.
- **Compatible post-commit single send:** after commit, the processor sends each committed intent once synchronously: success marks `sent`, failure marks `failed` (with a redacted, length-capped `result_summary`); a failed send never re-runs business and never fails the event. A failed CSV file send keeps the v0.2.0 direct fallback notice.
- **Outbox status enum:** `ReplyStatus` (`pending` / `sending` / `sent` / `failed` / `dead`) is centralized; P06a writes `pending` → `sent` | `failed` only, with `sending` and `dead` reserved for P06b. Status updates are conditional (`pending/sending/failed` only), so a `sent` row is never re-sent.

### Added (v0.2.1 — event worker, lease, retry, dead; P05b)

- Background PostgreSQL-driven **event worker** (`EventWorker`) started and stopped by the FastAPI lifespan. It claims `processed_events` rows in one transaction with `SELECT ... FOR UPDATE SKIP LOCKED`, writes `processing` / `lease_owner` / `lease_expires_at`, increments `attempt_count`, commits, then runs the `MessageProcessor` on the payload reloaded from the database. No Redis / Celery / RQ / Kafka / RabbitMQ; the database is the only queue.
- **Entry-point mode switch:** `LARK_LEDGER_WORKER_ENABLED` (default `true`) makes Webhook / WebSocket entries **claim only** and return immediately; processing moves to the worker. `false` restores the legacy in-process synchronous path. The two modes are mutually exclusive, so an event is never processed twice by both paths. Duplicate `event_id` still returns the dedup result immediately.
- **Lease semantics:** only the current `lease_owner` may write a processing outcome (guarded by `status='processing' AND lease_owner=<owner>`); an expired lease lets another worker reclaim the event (`attempt_count` increments again), and a stale worker can never overwrite the new owner's state. Lease duration defaults to 300 s; no renewal in this version.
- **Retry with exponential backoff:** retryable failures (network / timeout / 429 / 5xx / transient DB) are recorded as `failed` with `next_attempt_at = now + min(base × 2^(attempt-1), max)` (defaults 2 s / 3600 s) plus ~10% jitter. Unknown errors are conservatively retried.
- **Dead-lettering:** permanent errors (unparseable / unsupported-version payload, contract `ValueError` / `TypeError`, duplicate-constraint `IntegrityError`, non-429/408 4xx) or an exhausted attempt budget (default `event_max_attempts=3`, first attempt counts as 1) move the event to `dead`, clearing the lease and retaining the redacted error summary. No human replay in this version.
- **Crash recovery:** events left `processing` with an expired lease are reclaimed by another worker; a cancelled worker leaves its row for later takeover.
- **New settings:** `LARK_LEDGER_WORKER_ENABLED`, `WORKER_POLL_INTERVAL_SECONDS` (1.0), `WORKER_BATCH_SIZE` (10), `EVENT_MAX_ATTEMPTS` (3), `EVENT_LEASE_SECONDS` (300), `EVENT_RETRY_BASE_SECONDS` (2.0), `EVENT_RETRY_MAX_SECONDS` (3600).

### Added (v0.2.1 foundation — event state model only; P05a)

- Event state model foundation (P05a): `processed_events` gains `attempt_count`, `next_attempt_at`, `lease_owner`, `lease_expires_at`, `result_summary`, `source_message_id`, `user_open_id`, and `updated_at`; the `dead` terminal status is defined; migration `20260806_0007` backfills existing rows safely (already-processed rows get `attempt_count=1`, legacy payload-less rows stay non-replayable). Indexes support the worker's status/retry-window queries and operator lookups by source message or user.
- The sync path records `attempt_count` (each transition to `processing` counts one attempt) and a safe, length-capped, credential-redacted `result_summary`; successful events clear error fields.

### Changed

- Head migration is now `20260806_0011` (P06e adds guarded event replay audit state; P06d added terminal cleanup indexes at `20260806_0010`).
- `ReplyOutboxStore` gains P06b claim / lease primitives: `claim_batch` (worker, `FOR UPDATE SKIP LOCKED` + per-event ordering), `claim_by_id` (synchronous path), lease-guarded `mark_sent` / `record_failure` (both guarded by `status='sending' AND lease_owner=<owner>`), and `persist_file_key` / `persist_image_key`. `attempt_count` is incremented at claim (entering `sending`), not on failure; the old unguarded `mark_failed` is replaced by `record_failure`. The compatible single send and the Reply Worker share the same `ReplyDeliverer`.
- `FeishuClient.reply_text` / `reply_card` / `reply_file` now accept a `uuid` idempotency key and return the remote reply `message_id`.
- `MessageProcessor` takes `reply_worker_enabled` and an optional `wakeup`; with the worker enabled it only signals delivery after the outbox commit, otherwise it drives the synchronous claim / send loop.
- `LedgerService` gains `commit_changes: bool = True`; internal methods no longer commit, `execute()` commits once when enabled, and the batch-budget path uses savepoints. The Transactional Outbox path constructs the service with `commit_changes=False` so the processor owns the transaction.
- `EventService` gains a `claim()` (T1-only) path and routes `handle_safely` by worker mode; the synchronous `handle()` is retained for `WORKER_ENABLED=false` and tests. The `succeeded` status now means "business handled + reply intents written to the outbox".

### Security

- Event rows store only a single-line error summary with credentials (URL passwords, Authorization headers, Bearer tokens) redacted and a 512-character cap; full tracebacks are never persisted.
- Worker logs include `event_id`, `status`, `attempt_count`, a shortened owner label, retry time, and `error_code`, never the message body or payload.
- Reply outbox rows and reply worker logs carry the same discipline: a redacted, length-capped `result_summary`, and logs that never include reply text, financial body, file bytes, base64, full card JSON, credentials, or `Authorization` headers.

### Known limitations (v0.2.1)

- **Transactional Outbox (P06a) + Reply Worker (P06b) are provided:** business changes and reply intents commit atomically, a crashed event converges to `succeeded` without re-running business, and committed replies are delivered by the background reply worker with a lease, exponential-backoff retry, and reply `dead` handling.
- **Still missing (later work packages):** a user-visible result replay / manual-resend command (`OutboxReplayService` is internal) and a web admin UI / outbox visualization. Guarded operator event replay is CLI-only; readiness and terminal retention cleanup are available.
- **Pre-business error / notice replies** (e.g. "图片识别功能尚未配置", stage error prompts) are still sent directly and are **not** persisted to the outbox.
- **Extreme duplicate-reply window (disclosed):** if Feishu sends successfully but the local `sent` mark is lost and the re-send is more than 1 hour later (past the Feishu `uuid` dedup window), a duplicate reply may reach the user — it can never cause duplicate business execution or double bookkeeping.
- The release must not claim "never double-bookkeeps"; the `(source_message_id, source_item_index)` unique constraint remains the fallback guard.

## [0.2.0] - 2026-08-05

Theme: **auditable ledger** — see entries, edit by short ID, soft-delete/restore, CSV export, formal Release/GHCR.

### Added

- Claim-time persistence of versioned, normalized Feishu event payloads on `processed_events` (payload envelope, transport, status, received_at) so future workers can replay without relying on in-memory events only.
- User-scoped five-character Crockford Base32 ledger `short_id` values (`#XXXXX` in chat replies), with migration backfill and unique constraint per user.
- Bot actions `list_entries` and `get_entry` for recent/filtered ledger lines and single-entry detail by short ID (keyset pagination via `查看 #XXXXX 之前的N笔`).
- Bot actions `update_entry`, `delete_entry`, and `restore_entry` for targeted short-ID mutations, with `ledger_entry_revisions` audit snapshots (also written by update-last / undo-last).
- Bot action `export_entries` for user-scoped CSV Schema v1 export (default last 90 days, optional full history / include deleted, 5000-row and 5MB caps, formula-injection hardening, Feishu file message delivery).
- Alembic migrations `20260805_0004` (event payload), `20260805_0005` (short ID), `20260805_0006` (revisions). Head: `20260805_0006`.
- WebSocket + text-only + PostgreSQL quickstart path in README / `.env.example` / deployment docs.

### Changed

- `EventService` documents and implements an explicit T1 claim / T2 sync-process boundary; duplicate `event_id` delivery behavior is unchanged (still claim-first, no automatic retry).
- Create / update-last / undo-last success messages include the affected entry short ID.
- Documentation and `.env.example` converge on a WebSocket + text-only + PostgreSQL quickstart; image/voice/Webhook remain documented extension paths. Runtime `Settings.event_mode` default remains `webhook` unless `.env` sets `websocket`.

### Security

- Event payloads may store message text and media resource identifiers; binaries, secrets, and signature headers are not stored. Logs continue to avoid dumping full payload bodies.
- CSV export hardens formula-injection risk on user-controlled text fields and never embeds `open_id` or internal UUIDs in the file.

### Known limitations (not fixed in 0.2.0)

- Still **claim-first**: failed events are not auto-retried; successful writes with failed replies are not auto-compensated.
- No pre-write confirmation for image / voice / batch.
- CSV file send failures are not auto-retried.
- No web admin UI, no shared ledgers; JSON export is not a formal capability.
- Planned themes without ship dates: **v0.2.1** reliable delivery; **v0.3.0** high-risk confirmation.

### Support

- Alpha release. Fixes target the latest release and `main`.
- Review the [upgrade guide](docs/upgrading.md) before changing deployed versions. Back up PostgreSQL before migrations.

## [0.1.0] - 2026-08-03

### Added

- Self-hosted Feishu/Lark bookkeeping through text, voice, receipt images, payment-flow screenshots, and rich-text posts.
- Single-entry and batch bookkeeping, including up to 30 entries and 10 category budgets in one text request.
- Queries, last-entry correction and undo, monthly category budgets, threshold alerts, consumption report cards, and aggregated AI suggestions.
- Foreign-currency conversion with cached reference rates.
- WebSocket long-connection and Webhook event transports sharing `event_id` idempotency.
- PostgreSQL persistence, Alembic migrations, Docker Compose deployment, and multi-user isolation by Feishu `open_id`.

### Security

- AI providers receive only media or the minimum data required for parsing and aggregated advice; they have no database connection and cannot execute SQL.
- Webhook verification, encrypted-event handling, strict Pydantic schemas, parameterized queries, and guidance for secret handling are included.
- Runtime dependencies require a patched `cryptography` release and CI audits installed packages for known vulnerabilities.

### Support

- This is an Alpha release. Security and compatibility fixes are provided only for the latest release and `main`.
- Review the [upgrade guide](docs/upgrading.md) before changing deployed versions.

[0.2.0]: https://github.com/0verme/LarkLedger/releases/tag/v0.2.0
[0.1.0]: https://github.com/0verme/LarkLedger/releases/tag/v0.1.0
