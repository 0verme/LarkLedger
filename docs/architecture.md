# 架构说明

> Documentation is Chinese-first. For an English project overview, see the [English README](../README.en.md).

本文说明 LarkLedger `0.1.x` / 向 `0.2.x` 演进中的运行组件、消息数据流和安全边界。用户操作见[用户手册](help.md)，部署配置见[环境与部署指南](environment.md)。

## 组件

| 组件 | 职责 |
| --- | --- |
| FastAPI 应用 | 管理生命周期，提供 `GET /healthz` 和 Webhook 入口 |
| Webhook / 长连接接收器 | 接收飞书事件并转换为统一事件结构 |
| `EventService` | 按 `event_id` 抢占并去重事件；Worker 模式下只领取，同步模式下调用消息处理器 |
| `EventWorker` | 后台事件 Worker（P05b）：`FOR UPDATE SKIP LOCKED` 领取、数据库租约、指数退避重试与 dead 处理 |
| `MessageProcessor` | 归一化文字、图片、音频和富文本消息，下载媒体、调用 AI、执行业务动作并回复飞书 |
| `AIInterpreter` | 按独立配置路由文字、单图/多图和语音服务，解析单笔、复杂文字批量或最多 30 笔图片流水，并生成聚合消费建议 |
| `ExchangeRateService` | 获取并缓存外币到默认账本币种的最新参考汇率 |
| `LedgerService` | 执行固定的记账、修改、撤销、列表、导出查询、汇总、预算和报告逻辑 |
| `export` 服务 | 将账目序列化为 CSV Schema v1（注入防护、行数/体积上限、文件名） |
| `ReportRenderer` | 生成消费报告 PNG 和飞书消息卡片；失败时降级为文字卡片 |
| PostgreSQL / Alembic | 保存账目、预算、告警阈值和已处理事件，管理 Schema 版本 |

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

### 事务边界（v0.2.0 / P00 + v0.2.1 / P05b）

```text
T1  claim：insert processed_events(event_id, payload_json, …) → commit
T2  process：从数据库读回载荷 → 反序列化为业务事件 → 处理
```

两种执行模式（由 `LARK_LEDGER_WORKER_ENABLED` 选择，生产默认开启）：

- **Worker 模式（默认）**：入口（Webhook 后台任务 / WebSocket 回调）只执行 T1，把事件写入 `received` 后立即返回，**不等待** AI、飞书或账本处理。后台 `EventWorker` 用 `SELECT … FOR UPDATE SKIP LOCKED` 原子领取 `received`、已到期重试或租约过期的行，写入 `processing`、`lease_owner`、`lease_expires_at` 并 `attempt_count+1` 后提交，再加载 payload 执行 T2。重复 `event_id` 仍立即返回去重结果。
- **同步模式（`WORKER_ENABLED=false`）**：保留 v0.2.0 的 claim-first 路径，T2 在领取后立即执行。供单元测试与关闭 Worker 的部署使用。

两种模式互斥：同一进程只会启用其中一种，不存在”同步处理一次、Worker 又处理一次”的竞争。

- T1 成功只表示”事件已被领取且载荷已落库”，**不是**业务成功。
- T2 失败时：Worker 模式按错误分类写入 `failed`（带指数退避的 `next_attempt_at`）或 `dead`（永久错误或达到最大尝试次数）；同步模式写入 `failed` 与脱敏的 `result_summary`，**不会**取消 claim。
- **本版本没有** Transactional Outbox、回复自动补偿或人工重放；业务写入与 `succeeded` 状态**不是**原子提交。若业务事务已提交而状态更新未提交，重试可能再次执行处理器，但现有 `(source_message_id, source_item_index)` 唯一约束会阻止重复入账（详见下文「事件 Worker」）。

### 事件状态模型（v0.2.1 / P05a 地基 + P05b Worker）

`EventProcessStatus` 集中定义状态集合，业务代码只写枚举成员，不散落任意字符串：

| 状态 | 语义 | 分类 |
| --- | --- | --- |
| `received` | 已领取、载荷已落库，尚未处理 | 初始状态；Worker 可捞取 |
| `processing` | 一次处理尝试正在进行（含租约） | 处理中；租约过期后可被接管 |
| `succeeded` | 处理成功 | 终态 |
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
- **dead：** 永久错误、payload 缺失 / 损坏 / 版本不支持，或达到最大尝试次数时写入 `dead`，清空租约与 `next_attempt_at`，保留脱敏错误摘要；不再自动领取。**本版本不提供人工重放 dead**（属后续工作包）。

**幂等与重复记账边界（诚实声明）：** 本版本**没有** Transactional Outbox，业务写入与 `succeeded` 状态**不是**原子提交。若业务事务已提交而状态更新失败，重试可能再次运行处理器。现有账本层的 `(source_message_id, source_item_index)` 唯一约束会在重试时抛出 `IntegrityError`，阻止重复入账并把事件移入 `dead`；改、删、恢复动作对已应用的结果是幂等的（重复执行会返回"没有变化 / 已删除"）。因此**不**宣称"绝不重复记账"，发布文案不得使用 at-least-once 之外更强的主张。

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
- `processed_events`：已领取的飞书事件。新事件含版本化 `payload_json`、`payload_version`、`transport`、`status`、`received_at`、`processed_at` 与可选 `last_error_code`。可靠投递状态（P05a）另含 `attempt_count`、`next_attempt_at`、`lease_owner`、`lease_expires_at`、`result_summary`、`updated_at`，以及为人工定位去规范化的 `source_message_id` / `user_open_id`；历史无载荷行保持 `legacy_succeeded` 且不可重放。

所有日期范围都使用左闭右开语义。账目发生时间以带时区时间保存；相对时间、自然月和预算统计按全局配置的 IANA 时区计算。

外币代码只存在于结构化指令和确认回复中。业务层在写入前通过进程级汇率缓存将金额约算成管理员配置的默认币种，`ledger_entries.currency` 仍保存默认币种，因此现有汇总、预算和报告不需要进行混合币种聚合。汇率刷新失败时可使用过期缓存；没有任何缓存时操作失败且不会写入数据库。

## 运行与故障边界

- 应用生命周期负责启动或停止长连接与事件 Worker，并在关闭时释放数据库引擎。Worker 关闭时会请求停止、取消任务并等待结束，不悬挂后台任务。
- 默认（`WORKER_ENABLED=true`）下，Webhook 后台任务和长连接消息任务只负责领取事件；业务处理由进程内事件 Worker 执行。`WORKER_ENABLED=false` 时回到 v0.2.0 的进程内同步处理路径。多进程 / 多副本部署时每个进程都是 Worker，靠数据库租约与 `SKIP LOCKED` 保证并发安全。
- 报告图片渲染或上传失败时会发送不含图片的文字卡片；建议生成失败时使用本地规则生成后备建议。
- CSV 导出上传或发送失败时回复可理解错误；**不会**自动重试（回复补偿属后续工作包）。临时导出文件在上传结束或失败后删除。
- 复杂文字以及单图或多图中的批量账目先逐项严格校验，再用数据库保存点隔离单项写入，最终统一提交并返回成功、失败和收支合计。复杂文字中的预算也逐项隔离处理。所有批量账目共用原始消息的 `message_id` 和逐项索引，沿用来源幂等约束。完整异常只记录到带错误编号和处理阶段的日志中，用户回复仅包含可执行的分类错误。
- 基础 `compose.yaml` 只启动应用；`compose.dev.yaml` 可叠加本地 PostgreSQL 16。源码 Compose 在启动 Uvicorn 前执行 `alembic upgrade head`。
- 高可用部署仍需自行设计备份与凭据轮换。事件重试、租约接管和 dead 由 Worker 自动完成，但**没有** Transactional Outbox、回复自动补偿或人工重放；业务写入与事件状态不是原子提交，重复处理由现有账本唯一约束兜底。**不**宣称"绝不重复记账"。
