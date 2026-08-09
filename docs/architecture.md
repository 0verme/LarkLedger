# 架构说明

> Documentation is Chinese-first. For an English project overview, see the [English README](../README.en.md).

本文说明 LarkLedger v0.5.0 的运行组件、消息数据流、Web Dashboard 共享业务核心和安全边界。用户操作见[用户手册](help.md)，部署配置见[环境与部署指南](environment.md)。

## 统一 Client Application Service（阶段 4）

`ClientApplicationService` 是飞书、Web Dashboard 和 `/api/client/v1` 共用的应用层边界。它只接收认证适配器生成的 `RequestContext`，区分确定性管理命令、财务写命令、查询和 Pending 操作，并继续委托既有 `LedgerService`、`LedgerManagementService`、`HouseholdManagementService` 与查询服务。每次账本操作首先调用 `LedgerAuthorizationService`；传输层、AI 和请求 body 均不能覆盖 actor 或授权后的 ledger。

客户端认证由 `ClientCredentialService` 负责：高熵 `llv1_` Bearer 的 SHA-256 摘要持久化到 `client_credentials`，明文仅创建时返回；凭证包含最小 scope、当前账本、创建/最后使用/过期/撤销时间。撤销、过期、用户禁用以及家庭成员关系失效都会在每次请求时重新验证。Web 仍使用服务端 `DashboardSession` Cookie + CSRF，飞书仍由 `ChannelIdentity` 解析 User，三种认证不会互相降级或混用。

`client_idempotency_records` 以 `(actor_user_id, operation, ledger_id, idempotency_key)` 唯一约束绑定请求摘要和结构化响应快照，过期索引由 Cleanup Worker 分批清理。该表不复用 `processed_events` 或 `reply_outbox`，因此不会改变飞书事件 claim、回复投递或 Worker 重试语义。Pending 继续冻结 actor/ledger 并在行锁内一次执行。

## 身份、家庭与账本边界（Unreleased）

阶段 3 增加独立的 `HouseholdManagementService` 与统一 `LedgerAuthorizationService`。个人账本要求 `owner_user_id` 匹配；`household_shared` 账本不伪装成某个成员所有，而是通过 `household_id` 和有效 `household_members` 关系授权。用户默认账本仍只允许 personal；飞书入口当前账本保存在 `channel_identities.current_ledger_id`，Web 当前账本保存在 `dashboard_sessions.ledger_id`。

飞书个人账本与家庭命令都在 `AIInterpreter` 之前解析。新入口或新 Dashboard Session 没有显式选择时回退到用户默认个人账本；不会自动进入家庭账本。`IdentityService`、Dashboard 认证和账本选择都调用统一授权服务；失效持久选择会被清除并回退。Pending 创建时冻结 `actor_user_id + ledger_id`，确认时重新检查冻结账本权限并使用冻结值。

飞书 `open_id` 现在只作为 `ChannelIdentity` 的外部主体标识。入口在调用账务核心前
解析出 `RequestContext(actor_user_id, ledger_id, source_channel)`；账目、预算和 Web
查询以 `ledger_id` 为授权与数据隔离边界。迁移期保留旧 `user_open_id` 列用于安全
回滚，但它不再是新查询的首要作用域。

```text
Feishu / Web
      │ external subject
      ▼
ChannelIdentity ──► User ──► personal Ledger
                       │
                       └─► HouseholdMember ──► Household ──► shared Ledger
                                                            │
                                                            ▼
                                                   Ledger Core / PostgreSQL
```

Event 与 Reply Outbox 中的外部标识承担接收、重放、审计和投递职责，属于传输层元数据，
不会为了领域身份迁移而删除。

## 组件

| 组件 | 职责 |
| --- | --- |
| FastAPI 应用 | 管理生命周期，提供 `GET /healthz`、`GET /readyz`、Webhook、可选 Web API 与 Dashboard 静态资源 |
| Webhook / 长连接接收器 | 接收飞书事件并转换为统一事件结构 |
| `EventService` | 按 `event_id` 抢占并去重事件；Worker 模式下只领取，同步模式下调用消息处理器 |
| `EventWorker` | 后台事件 Worker（P05b）：`FOR UPDATE SKIP LOCKED` 领取、数据库租约、指数退避重试与 dead 处理 |
| `ReplyWorker` | 后台回复 Worker（P06b）：领取已提交的 `reply_outbox`，租约、重试、指数退避、dead，与事件 Worker 职责解耦 |
| `MessageProcessor` | 归一化文字、图片、音频和富文本消息，下载媒体、调用 AI、执行业务动作并回复飞书 |
| `AIInterpreter` | 按独立配置路由文字、单图/多图和语音服务，解析单笔、复杂文字批量或最多 30 笔图片流水，并生成聚合消费建议 |
| `ExchangeRateService` | 获取并缓存外币到默认账本币种的最新参考汇率 |
| `LedgerService` | 执行固定的记账、修改、撤销、列表、导出查询、汇总、预算和报告逻辑 |
| `LedgerManagementService` | 校验/规范化账本名，执行个人账本创建、列表、选择、默认和重命名，并验证所有权 |
| `HouseholdManagementService` | 创建/重命名家庭，管理成员与邀请，并原子创建家庭公共账本 |
| `LedgerAuthorizationService` | 集中验证个人 owner 或家庭有效成员权限，供账务核心、会话解析、选择和 Pending 复用 |
| `export` 服务 | 将账目序列化为 CSV Schema v1（注入防护、行数/体积上限、文件名） |
| `ReportRenderer` | 生成消费报告 PNG 和飞书消息卡片；失败时降级为文字卡片 |
| PostgreSQL / Alembic | 保存账目、预算、告警阈值和已处理事件，管理 Schema 版本 |

## Web Dashboard 与共享业务核心

```text
                    ┌──────────────┐
Feishu ───────────→ │ Event Worker │
                    └──────┬───────┘
                           ↓
                     Service Layer
                           ↓
                      PostgreSQL
                           ↑
                     Service Layer
                           ↑
Web Dashboard → Web API ───┘
```

Dashboard 是可选的 React/Vite 静态客户端，由同一 FastAPI 容器提供。`/api/web/v1/*` 从 PostgreSQL Session 取得已认证 `user_open_id`，请求 body/query 不能覆盖该身份。账目写操作继续进入 `LedgerService` 并产生 revision；确认/取消继续进入 `PendingCommandStore` 的锁与幂等路径；结果重发和事件重放继续使用现有 Replay Service 与安全预检。Web 不直接更新 ORM、不创建第二套确认状态机、Outbox、Worker 或任务队列。

普通用户只能读取和操作自己的账目、预算、报告、导出与 pending。环境变量中列出的管理员额外获得经过脱敏的 Event / Outbox、Dead / Replay、readiness 与只读安全配置；完整 payload、回复正文、blob、密钥和数据库连接信息不进入 Web 响应。

## 消息处理链路

```text
飞书 Webhook ─┐
              ├─→ 统一事件结构 → event_id 去重 → 消息和媒体读取
飞书长连接 ───┘                                  ↓
                                      AI 解析 / 音频转写
                                                ↓
                                      ParsedCommand 严格校验
                                                ↓
                                      外币金额约算（按需）
                                                ↓
                                      固定账本动作与事务
                                                ↓
                                      文本 / 报告卡片 / CSV 文件消息回复
```

Webhook 端点完成来源校验、请求解析和后台任务登记后立即确认回调。长连接 SDK 在线程中维护连接，把事件安全转交给 ASGI 事件循环。两种入口最终都调用同一个 `EventService` 和 `MessageProcessor`。

飞书 `post` 富文本会先归一化：保留标题、正文、链接文字和备注文字，忽略 `@` 与样式节点，并按出现顺序提取、去重最多 5 个图片 Key。只有文字时走文字模型；包含图片时并行下载全部图片，再把正文和图片作为一次视觉请求处理。超过图片上限或任一下载、格式校验失败时不会执行账本动作。

## 事件幂等与可重放载荷

收到事件后，`EventService` 在 **T1（领取事务）** 中把 `event_id` 与一份版本化的可重放 JSON 载荷写入 `processed_events` 并提交。主键冲突表示事件已经领取，本次投递不再处理。新账目还会保存飞书 `message_id`，并通过唯一约束避免同一来源消息重复创建。

### 事务边界（v0.2.0 / P00 + v0.2.1 / P05b + P06a/P06b Outbox）

```text
T1  claim：insert processed_events(event_id, payload_json, …) → commit
T2  process：从数据库读回载荷 → 反序列化为业务事件 → 业务写入/查询（flush）
             → 生成稳定的回复意图 → insert reply_outbox → commit（业务 + Outbox 原子）
T3  deliver：从已提交的 Outbox 领取回复意图 → 发送
             → 成功标记 sent，失败标记 failed（可重试）或 dead（永久）
T4  event status：只要 T2 已提交，事件即可标记 succeeded（飞书发送失败不影响事件）
```

事件处理两种执行模式（由 `LARK_LEDGER_WORKER_ENABLED` 选择，生产默认开启）：

- **Worker 模式（默认）**：入口（Webhook 后台任务 / WebSocket 回调）只执行 T1，把事件写入 `received` 后立即返回，**不等待** AI、飞书或账本处理。后台 `EventWorker` 用 `SELECT … FOR UPDATE SKIP LOCKED` 原子领取 `received`、已到期重试或租约过期的行，写入 `processing`、`lease_owner`、`lease_expires_at` 并 `attempt_count+1` 后提交，再加载 payload 执行 T2。重复 `event_id` 仍立即返回去重结果。
- **同步模式（`WORKER_ENABLED=false`）**：保留 v0.2.0 的 claim-first 路径，T2 在领取后立即执行。供单元测试与关闭 Worker 的部署使用。

两种事件模式互斥：同一进程只会启用其中一种，不存在”同步处理一次、Worker 又处理一次”的竞争。

回复投递两种模式（由 `LARK_LEDGER_REPLY_WORKER_ENABLED` 选择，生产默认开启），使用**同一套** Outbox claim / lease / 结果守卫原语，不会同时运行：

- **Reply Worker 模式（默认）**：T2 提交后处理器只**唤醒**后台 `ReplyWorker` 并返回。`ReplyWorker` 用 `SELECT … FOR UPDATE SKIP LOCKED` 领取已提交的 `reply_outbox` 行（`pending`；`failed` 且 `next_attempt_at` 到期；`sending` 且租约过期），写入 `sending`、租约并 `attempt_count+1` 后提交，再上传 / 发送，最后按租约守卫写入 `sent` / `failed` / `dead`。数据库仍是唯一事实来源，进程内唤醒丢失只推迟一个轮询周期。
- **兼容同步模式（`REPLY_WORKER_ENABLED=false`）**：T2 提交后处理器对刚提交的行逐个 `claim_by_id` → 用同一个 `ReplyDeliverer` 发送 → 按守卫标记结果。文件发送失败时保留 v0.2.0 的直接失败提示；报告图片上传失败时降级为文字卡片。

- T1 成功只表示”事件已被领取且载荷已落库”，**不是**业务成功。
- T2 中 `LedgerService` 以 `commit_changes=False` 构建（只 flush），回复意图与业务变更在同一 session、同一 commit 内落库。T2 失败时：Worker 模式按错误分类写入 `failed`（带指数退避的 `next_attempt_at`）或 `dead`（永久错误或达到最大尝试次数）；同步模式写入 `failed` 与脱敏的 `result_summary`，**不会**取消 claim。
- T3 消费的是**已提交**的 Outbox 行，不是内存对象。回复发送失败（`failed` 或 `dead`）**不会**重新执行业务，也**不会**令事件进入业务重试——事件与回复状态完全解耦。
- `succeeded` 语义（P06a）：**业务已处理且回复意图已可靠写入 `reply_outbox`**，不再表示”飞书已收到回复”。
- **提交后崩溃窗口**：若 T2 已提交而事件尚未标记 `succeeded`，事件被重新领取时，处理器先检查该事件是否已有 Outbox 行（有 ⟹ 业务已提交），从而**跳过业务**，直接收敛事件为 `succeeded`。不会重复入账、重复修改/删除/恢复，也不会重复插入 Outbox 行。

### 事件状态模型（v0.2.1 / P05a 地基 + P05b Worker）

`EventProcessStatus` 集中定义状态集合，业务代码只写枚举成员，不散落任意字符串：

| 状态 | 语义 | 分类 |
| --- | --- | --- |
| `received` | 已领取、载荷已落库，尚未处理 | 初始状态；Worker 可捞取 |
| `processing` | 一次处理尝试正在进行（含租约） | 处理中；租约过期后可被接管 |
| `succeeded` | 业务已处理**且**回复意图已可靠写入 `reply_outbox`（P06a 语义；不再表示飞书已收到回复） | 终态 |
| `failed` | 某次尝试失败，重试未到期 | 可重试候选；`next_attempt_at` 到期后 Worker 可捞取 |
| `dead` | 重试耗尽或永久错误，Worker 写入 | 终态 |
| `legacy_succeeded` | 迁移前无载荷的历史行，不可重放 | 终态 |

Worker 写入 `received → processing → succeeded | failed | dead`，每次进入 `processing`（含租约过期后的重新领取）`attempt_count` 加一。`next_attempt_at`、`lease_owner`、`lease_expires_at` 由 Worker 在领取 / 失败时写入，成功或失败后租约字段清空。

错误摘要 `result_summary` 只保存单行文本：取异常第一行、长度上限 512 字符，并脱敏 URL 中的密码、`Authorization` 头与 `Bearer` 令牌；完整异常栈与消息正文不会持久化。

### 事件 Worker（v0.2.1 / P05b）

后台 `EventWorker` 是一个 asyncio 任务，随 FastAPI lifespan 启动和停止；数据库是唯一队列与协调存储，**不引入** Redis / Celery / RQ / Kafka / RabbitMQ。多个进程或多个副本同时运行时，每个进程都可成为 Worker，但数据库租约保证并发安全。

- **领取（claim）：** 单个事务内执行 `SELECT … FOR UPDATE SKIP LOCKED` 选取候选行（`received`；`failed` 且 `next_attempt_at` 已到期；`processing` 且租约已过期，且 `payload_json` 非空），写入 `processing`、`lease_owner`、`lease_expires_at`，`attempt_count + 1` 并提交，然后才加载 payload 执行业务处理。两个 Worker 不会领取同一行；崩溃后未完成的行在租约过期后由其他 Worker 接管。
- **租约：** 只有持有租约的 Worker 能提交本次结果（完成或失败都带 `status='processing' AND lease_owner=<owner>` 条件）。租约过期或被接管后，旧 Worker 的更新 rowcount 为 0，不会覆盖新 Worker 的状态。默认租约 300 秒，无续期；若单条处理可能超过租约，调大 `LARK_LEDGER_EVENT_LEASE_SECONDS`。
- **attempt 语义：** 每次进入 `processing`（含租约接管）`attempt_count + 1`；最大尝试次数包含首次处理；达到上限后失败进入 `dead`。
- **重试分类：** 永久错误（payload 无法解析 / 版本不支持 / `ValueError`/`TypeError` 契约错误 / 重复约束 `IntegrityError` / 非 408/429 的 4xx）直接进入 `dead`；其余默认视为可重试（网络、超时、429、5xx、数据库临时故障），写入 `failed` 并按指数退避安排 `next_attempt_at`。分类策略保守且可解释，文档见代码注释。
- **退避：** `min(base × 2^(attempt-1), max)`，默认 base 2 秒、max 3600 秒，并带约 10% 随机抖动避免多个事件同时重试。测试使用可注入时钟，不真实等待。
- **dead：** 永久错误、payload 缺失 / 损坏 / 版本不支持，或达到最大尝试次数时写入 `dead`，清空租约与 `next_attempt_at`，保留脱敏错误摘要；不再自动领取。P06e 提供仅管理员使用的受控 CLI 重放，且默认 dry-run。

**幂等与重复记账边界（P06a）：** 业务写入与回复意图通过 Transactional Outbox 在同一事务提交，因此业务成功 ⟹ 一定有 Outbox 记录。崩溃窗口重试时，处理器先检查 Outbox（有 ⟹ 业务已提交）从而跳过业务，事件收敛为 `succeeded`，**不再**以 `IntegrityError→dead` 作为正常恢复路径。`(source_message_id, source_item_index)` 唯一约束仍是**兜底**保障（例如同一飞书消息被以不同 `event_id` 重新投递时），此时 `IntegrityError` 仍会把事件移入 `dead` 以阻止重复入账；改、删、恢复动作对已应用的结果是幂等的（重复执行会返回"没有变化 / 已删除"）。**不**宣称"绝不重复记账"，发布文案不得使用 at-least-once 之外更强的主张。

### 回复 Outbox（v0.2.1 / P06a Transactional Outbox + P06b Reply Worker）

业务变更与待发送回复记录放入**同一个数据库事务**：业务成功提交时，一定存在可供后续发送或补偿的 `reply_outbox` 记录。所有成功路径的回复（记账确认、列表/详情/汇总/预算/帮助、CSV 导出、消费报告卡片）都通过该表落库并发送；`错误提示 / 预业务通知`（如"图片识别功能尚未配置"）仍直接同步发送，**未**进入 Outbox（本版本如实声明）。

**表结构要点：** `reply_outbox` 每行是一条自包含的回复意图——回复目标 `message_id`、`reply_type`（`text` / `file` / `card`）、版本化 `payload_json` 信封，以及文件/报告图片的原始字节 `payload_blob`（附 `size` 与 `sha256`）。因此发送时不需要当前进程内存对象、原始 HTTP 请求、已关闭的 session、临时文件路径，也不需重新调用 AI 或重新查询账本。`(event_id, reply_type)` 唯一约束保证同一事件不会重复插入同一回复；`sequence` 稳定排序（如 CSV 导出先发文件再发确认文字），并配 `(event_id, sequence)` 索引支撑顺序领取。

**状态语义：** `status` 取值集中定义于 `ReplyStatus` 枚举（`pending` / `sending` / `sent` / `failed` / `dead`）。`attempt_count` 在每次进入 `sending` 时加一（包含首次发送与租约接管）；发送失败不再额外累加。发送结果更新带租约守卫（`status='sending' AND lease_owner=<owner>`），已 `sent` / `dead` 的记录不会被改写，旧 Worker 租约失效后不能覆盖新 Worker 结果。

**Reply Worker（P06b）：** 后台 asyncio 任务随 FastAPI lifespan 启停；数据库是唯一队列与协调存储。领取在单事务内 `SELECT … FOR UPDATE SKIP LOCKED`，写入 `sending`、`lease_owner`、`lease_expires_at`、`attempt_count+1` 并提交后才调用飞书 API。同一事件内按 `sequence` 升序投递：更小 `sequence` 尚未 `sent` / `dead` 时，后一条不会被领取（含并发下）；前一条 `failed` 等待重试时后一条等待；前一条 `dead` 后允许后一条独立发送，避免整条回复链永久卡死。

- **重试与 dead：** 临时错误（网络、超时、HTTP 408/429/5xx、上传临时失败）写入 `failed` 并按 `min(base × 2^(attempt-1), max)` 退避；永久错误（`payload_version` 不支持、未知 `reply_type`、路由字段缺失、`payload_json` 契约损坏、`payload_blob` 缺失 / 大小或 checksum 不一致、非 408/429 的 4xx）或重试耗尽直接写入 `dead`，清空租约与 `next_attempt_at`，不再自动领取。单条回复失败不会终止 Worker 循环。
- **上传与发送分阶段：** 文件上传成功、发送失败后重试时复用已持久化的 `remote_file_key`，不重复上传；报告图片同理复用 `remote_image_key`。上传仍失败时文件行按临时错误重试，卡片行降级为文字卡片（图片是可选项）。
- **幂等：** 每次回复都携带飞书回复 API 的 `uuid` 幂等键（取 Outbox 行 ID，稳定、≤50 字符）。同一行在 1 小时内重发时飞书直接返回已创建的 `message_id` 而不重复投递，极大缩小“发送成功但未标 `sent` 就崩溃”的重复窗口；发送成功还把远端 `message_id` 写入 `remote_message_id`。
- **结果回放（内部能力）：** `OutboxReplayService` 只把 `failed` / `dead` 行重新置为 `pending`（清退避、清租约、清错误摘要），由 Worker 再次发送**完全相同**的已持久化载荷；**不**重新调用 AI、**不**重新执行业务、**不**重新生成 CSV / 渲染报告。当前是内部可测试能力，**未**暴露用户命令入口。

**崩溃恢复：** 若业务 + Outbox 已提交而事件状态未更新（进程崩溃），事件重新被 Worker 领取时，处理器先检查该事件是否已有 Outbox 行；有则跳过业务、不重复插入，直接收敛事件为 `succeeded`。这是 P06a 的核心验收项，与旧的 `IntegrityError→dead` 兜底不同。回复发送本身在 `sent` 标记前崩溃时，Outbox 行留在 `sending`，租约过期后被重新领取并重发——1 小时内的重发由飞书 `uuid` 幂等去重，1 小时外的极端窗口可能重复一条回复，但**绝不会**重复执行业务或重复记账。

### Liveness 与 readiness（v0.2.1 / P06c）

- `GET /healthz` 只读取进程内事件模式与长连接状态，不打开数据库连接、不探测飞书或 AI；数据库故障、少量失败 / dead / 积压不会把存活进程误判为死亡。
- `GET /readyz` 对 PostgreSQL 执行轻量 `SELECT 1`，从 Alembic 配置解析代码唯一 head 并与数据库 `alembic_version` 比对，再读取应用 shutdown、Event Worker、Reply Worker 和 WebSocket receiver 的只读任务快照。Webhook 模式不要求 receiver；显式关闭 Worker 是合法兼容模式。
- Worker / receiver task 的完成回调会主动取回异常，只保留异常类型作为安全错误码，避免 `Task exception was never retrieved`。异常退出、未启动、迁移不一致或 shutdown 都返回 HTTP 503；探针不执行 migration、不扫描业务表、不返回数据库 URL、凭据、用户标识、payload、回复内容或完整 nonce。

### 终态保留与 Cleanup Worker（v0.2.1 / P06d）

- 只清理 `processed_events` 的 `succeeded` / `legacy_succeeded` / `dead` 和 `reply_outbox` 的 `sent` / `dead`。`received` / `processing` / `failed` Event、`pending` / `sending` / `failed` Outbox、有效 lease，以及全部账本 / revision 永不进入清理选择集。
- 默认成功记录保留 30 天，dead 保留 90 天；成功 Event 使用 `processed_at`，dead Event 使用 `updated_at`，sent Outbox 使用 `sent_at`，dead Outbox 使用 `updated_at`。所有截止时间使用时区感知 UTC；保留期最小 1 天，关闭需显式 `CLEANUP_ENABLED=false`。
- 每类清理在独立短事务中按时间索引选取至多 `batch_size` 个主键并使用 `FOR UPDATE SKIP LOCKED`，再按主键删除。顺序固定为 Outbox 后 Event；Event 只有在关联 Outbox 已全部按自身期限清除后才可删除，不依赖 CASCADE 提前丢失审计记录。多实例可并发运行且重复执行幂等。
- Cleanup Worker 随 lifespan 启停，单轮失败只记录清理类型、截止时间、耗时和安全错误码，下轮继续。它不属于核心承接硬门禁：异常退出时 `/readyz` 的 `cleanup_worker` 为 `warning`，整体仍可 ready；`/healthz` 不受影响。

### 受控人工事件重放（v0.2.1 / P06e）

- **事件重放不同于结果回放：** 事件重放会重新执行业务；结果回放只重发已持久化的 Outbox 内容。任何关联 Outbox（无论 sent / pending / failed / dead）都会拒绝事件重放，并指示使用结果回放。
- 新接收事件写入 `replay_safety_version=1`，证明其业务写入遵守 Transactional Outbox 原子边界。迁移不会为历史事件猜测该标记；无法证明原子性的存量行默认拒绝。预检同时校验状态、payload 版本、事件 / 来源消息一致性、lease、Outbox 和按 `source_message_id/source_item_index` 查询到的账目结果。
- 业务成功时 `business_committed_at` 与业务结果、Outbox 在同一事务写入，作为**独立于 Outbox 保留期**的持久证据：即使 Outbox 已被终态清理删除，自动 Worker 的崩溃窗口预检与人工重放都会据此拒绝再次执行业务。迁移仅对「存在关联 Outbox」的存量事件回填该证据，绝不猜测其余存量行。
- 只有无 Outbox、无来源账目结果且 payload 完整的 `dead` / `failed`，或 lease 明确过期的 `processing` 可候选重放。执行事务使用 `FOR UPDATE` 重新预检；状态重置为 `received`，清空调度、租约和错误字段。
- `attempt_count` 表示当前自动尝试窗口，人工重放时重置为 0，使 Worker 获得新的有限重试窗口；`manual_replay_count` 累计人工重放次数，`event_replay_audits.previous_attempt_count` 保存旧窗口计数。两名操作员并发时只有持锁者能成功重放，旧 Worker 的 lease-guarded 更新不能覆盖新状态。
- 每次执行尝试写入独立 `event_replay_audits`；成功审计与状态重置同事务。审计不复制 payload，终态 Event 清理也不级联删除审计。CLI 默认 dry-run，只有显式 `--execute` 修改数据，且输出不包含 payload、财务正文、operator 或 reason。

**尚未实现（后续版本）：** Web 管理后台 / Outbox 可视化、对用户可见的结果回放命令、简单文字等全部写入都强制审批的完整财务审批流。

### 高风险确认（v0.3.0 / P07）

`risky_only` 策略把写入分为三类：**简单单笔文字直写**、**高风险进确认**、**缺失/无法判定则拒绝或追问**。风险路由插入在命令冻结后、执行 `LedgerService` 前（`MessageProcessor.process`），判定依据为来源类型（image / audio / post 带图）、批量 action（BATCH / CREATE_ENTRIES / SET_BUDGETS）以及新写的疑似重复查重。

- **pending_commands 表**（迁移 `20260806_0012`）保存**冻结**的结构化命令（`payload_json`）与冻结的用户预览（`preview_json`）。确认只反序列化 `payload_json` 交给 `LedgerService`，绝不重新调用 AI 或重新识别媒体。`confirmation_code`（`CA83F2`，展示 `#C-A83F2`）用户内唯一、不复用、大小写不敏感、正则解析。
- **创建原子性：** 高风险消息在**同一事务**写入 pending 行 + 预览 CARD Outbox + 事件的 `business_committed_at`，事件收敛为 `succeeded`；崩溃重投时 Outbox 预检命中，跳过业务且不产生第二条 pending。
- **确认执行原子性：** `confirm_and_execute` 以 `SELECT FOR UPDATE` 锁行 → 校验 user_open_id / status=pending / 未过期 → `executing` → 用 `LedgerService(commit_changes=False)` 执行冻结命令 → 写确认结果文本 Outbox → 置 `executed`，单事务提交。两个并发确认只执行一次；确认与取消并发只有一个成功；旧卡片重复点击走幂等分支。确认是**新事件**（文本指令或 `card.action.trigger`），与可靠投递同一套 Worker / Outbox 基础。
- **卡片交互：** 预览卡片带确认/取消按钮，`value` 含 `k=larkledger_pending` 标记与确认码；`card.action.trigger` 回调（webhook 分支 + 长连接注册）核验 operator 用户、code 与状态，重复点击幂等。文本 `确认 / 取消 / 查看待确认 #C-XXXXX` 是始终可用的兜底。
- **过期与保留：** 到期 `pending` 由 Cleanup Worker 置为 `expired`；`executed / cancelled / expired / failed` 终态行按 `pending_retention_days`（默认 7 天）清理。
- **疑似重复：** 同用户 + 方向 + 金额 + 币种 + `occurred_at` 在 `pending_duplicate_window_minutes` 窗口内 +（分类相同或来源相同），Python 层备注相似度终判；命中进确认并在预览中展示现有短 ID，**不直接拒绝**。
- **隐私：** 预览与日志只包含结构化聚合（金额、分类、时间、截断备注），绝不写入 OCR 全文或语音转写；操作员 CLI 只输出安全聚合。

### 载荷内容与隐私

载荷由 `event_payload` 模块集中构建与校验，当前 `payload_version = 1`。信封字段包括：`payload_version`、`event_id`、`transport`（`webhook` | `websocket`）、`received_at`，以及归一化后的业务 `event`（`sender.sender_id` 的 open_id/user_id，`message` 的 message_id / message_type / content / 可选 chat_id）。

- **会持久化（重放必需）：** 消息正文 JSON 字符串、图片/文件 **资源标识**（如 `image_key`、`file_key`）、发送者 open_id 等。
- **不会持久化：** App Secret、Verification Token、Encrypt Key、Authorization、Webhook 签名 Header、完整 HTTP Request、SDK 实例、图片/音频 **二进制**。

因此 PostgreSQL 与其备份应按**敏感财务数据**保护。应用日志只记录 `event_id`、`message_id`、`transport`、异常类型等标识，**不**完整 dump 消息正文或 payload。

### 媒体重取限制

图片与语音处理在运行时通过飞书 `messages/{message_id}/resources/{file_key}` 重新下载。载荷只保存资源标识。飞书侧资源是否长期可下载取决于开放平台保留策略与机器人权限；**不保证**历史媒体在任意时刻仍可取回。Worker 重试或重放媒体事件时可能因资源过期而失败，会进入重试直至 dead，需要单独运维策略。

### 历史行

升级前仅有 `event_id` / `processed_at` 的行在迁移后标记为 `status=legacy_succeeded` 且 `payload_json IS NULL`，**不可重放**，仅保留去重语义。

## AI 与数据库边界

AI 只允许返回 `ParsedCommand` 定义的字段，额外字段会被拒绝。支持的动作是：

- 新增账目
- 批量新增最多 30 笔文字账目，并可同时设置最多 10 项预算
- 修改或撤销最近一笔；按短 ID 查看、修改、删除、恢复
- 列表查询与 **CSV 导出**（`export_entries`）
- 查询汇总或生成报告
- 设置、查看或删除分类月预算
- 返回帮助

Schema 不包含 SQL、表名、任意过滤表达式或数据库标识。`LedgerService` 把已校验动作映射为固定 SQLAlchemy 查询，并始终带上当前用户的 `open_id` 边界。导出在 SQL 层按用户过滤，默认最近 90 天、最多 5000 行；CSV 在应用内生成后经飞书文件上传 API 发回当前会话。导出为只读，不写 revision；**无**对象存储、公网链接、导出任务表或自动重试。

报告建议使用文字 AI 配置，只发送币种、分类合计、趋势、收入、支出、结余和记录数等聚合数据，不发送逐笔备注或用户标识。文字、图片（包括富文本正文与最多 5 张图片）和音频分别发送给部署者配置的文字、视觉和转写服务；图片或语音服务未配置时不会回退到文字模型。

## 数据模型

- `ledger_entries`：账目、用户、**用户内唯一五位 `short_id`（聊天引用层）**、金额、币种、分类、备注、发生时间、来源消息、来源项序号和软删除时间；UUID 仍为内部主键；`(user_open_id, short_id)` 与来源消息项唯一。
- `ledger_entry_revisions`：账目修改/删除/恢复的 append-only 快照（`before_json` / `after_json`，含 `snapshot_version`）；与账目变更同事务写入。
- `category_budgets`：每个用户和分类唯一的长期月预算。
- `budget_alerts`：记录预算在每个自然月已发送的 80% / 100% 阈值提醒。
- `processed_events`：已领取的飞书事件。新事件含版本化 `payload_json`、`payload_version`、`transport`、`status`、`received_at`、`processed_at` 与可选 `last_error_code`。可靠投递状态（P05a）另含 `attempt_count`、`next_attempt_at`、`lease_owner`、`lease_expires_at`、`result_summary`、`updated_at`，以及为人工定位去规范化的 `source_message_id` / `user_open_id`；P06e 增加 `manual_replay_count` 与仅对新事件写入的 `replay_safety_version`。历史无载荷行保持 `legacy_succeeded` 且不可重放。
- `event_replay_audits`：人工事件重放审计，保存 operator、reason、前后状态、旧尝试数、重放序号、结果与安全错误码；不保存 payload 副本，也不外键级联到可能被保留策略清理的 Event。
- `reply_outbox`：**Transactional Outbox（P06a）+ Reply Worker 状态（P06b）**——自包含的飞书回复意图，与业务变更同事务提交。字段包括 `event_id`（关联源事件）、`message_id`（回复目标）、`reply_type`、`sequence`、`transport`、`payload_version`、`payload_json`（内容信封）、`payload_blob`（CSV / 报告图片字节，附 `size` / `sha256`）、`status`、`attempt_count`、`next_attempt_at`、`lease_owner`、`lease_expires_at`、`sent_at`、`last_error_code`、`result_summary`，以及 P06b 投递元数据 `remote_message_id`（远端回复消息 ID）、`remote_file_key` / `remote_image_key`（上传资源键，重试复用不重复上传）。唯一约束 `(event_id, reply_type)` 保证幂等；索引 `(status, next_attempt_at)`、`lease_expires_at` 支撑领取，`(event_id, sequence)` 支撑同一事件内的顺序领取。

所有日期范围都使用左闭右开语义。账目发生时间以带时区时间保存；相对时间、自然月和预算统计按全局配置的 IANA 时区计算。

外币代码只存在于结构化指令和确认回复中。业务层在写入前通过进程级汇率缓存将金额约算成管理员配置的默认币种，`ledger_entries.currency` 仍保存默认币种，因此现有汇总、预算和报告不需要进行混合币种聚合。汇率刷新失败时可使用过期缓存；没有任何缓存时操作失败且不会写入数据库。

## 运行与故障边界

- 应用生命周期负责启动或停止长连接、事件 Worker 与回复 Worker，并在关闭时释放数据库引擎。关闭顺序：先停接收新事件，再请求事件 Worker 停止，再请求回复 Worker 停止（不再领取新 Outbox），最后取消并等待任务、释放引擎；不悬挂后台任务。未送达的 Outbox 行持久在库，下次启动继续投递。
- 默认（`WORKER_ENABLED=true`）下，Webhook 后台任务和长连接消息任务只负责领取事件；业务处理由进程内事件 Worker 执行。`WORKER_ENABLED=false` 时回到 v0.2.0 的进程内同步处理路径。多进程 / 多副本部署时每个进程都是事件 Worker 与回复 Worker，靠数据库租约与 `SKIP LOCKED` 保证并发安全。
- 默认（`REPLY_WORKER_ENABLED=true`）下，回复由后台 Reply Worker 投递；`false` 时使用同一套 claim / 租约 / 结果守卫原语的兼容同步路径。两者不会同时运行。
- 报告图片渲染或上传失败时：同步模式会发送不含图片的文字卡片；Reply Worker 模式下上传仍失败的文件行会按临时错误重试、卡片行降级为文字卡片；建议生成失败时使用本地规则生成后备建议。
- CSV 导出上传或发送失败时回复可理解错误；Reply Worker 模式下失败行按退避自动重试直至 `dead`，同步模式下保留 v0.2.0 的直接失败提示。临时导出文件在上传结束或失败后删除。
- 复杂文字以及单图或多图中的批量账目先逐项严格校验，再用数据库保存点隔离单项写入，最终统一提交并返回成功、失败和收支合计。复杂文字中的预算也逐项隔离处理。所有批量账目共用原始消息的 `message_id` 和逐项索引，沿用来源幂等约束。完整异常只记录到带错误编号和处理阶段的日志中，用户回复仅包含可执行的分类错误。
- 基础 `compose.yaml` 只启动应用；`compose.dev.yaml` 可叠加本地 PostgreSQL 16。源码 Compose 在启动 Uvicorn 前执行 `alembic upgrade head`。
- 高可用部署仍需自行设计备份与凭据轮换。事件重试、租约接管和 dead 由事件 Worker 自动完成；回复发送、重试、租约接管和 dead 由 Reply Worker 自动完成（P06b）。**已具备** Transactional Outbox、readiness、终态清理与受控 CLI 事件重放：业务变更与回复意图同事务提交，崩溃窗口重试不会重复执行业务；回复失败绝不重新执行业务；进程重启后继续投递 `pending` / `failed` 回复；结果回放只重发已持久化载荷；`/readyz` 能发现数据库 / migration / 后台任务异常。**仍未具备**：Web 管理后台 / Outbox 可视化、用户可见结果回放、AI 写入前确认、对所有外部 API 都能绝对避免重复回复（飞书已发送但本地未标记 `sent` 时崩溃，1 小时内由 `uuid` 幂等去重，1 小时外存在极小的重复回复窗口；该窗口不会导致重复执行业务或重复记账）。`(source_message_id, source_item_index)` 唯一约束仍是重复入账的兜底保障。**不**宣称"绝不重复记账"。
