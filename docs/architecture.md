# 架构说明

> Documentation is Chinese-first. For an English project overview, see the [English README](../README.en.md).

本文说明 LarkLedger `0.1.x` / 向 `0.2.x` 演进中的运行组件、消息数据流和安全边界。用户操作见[用户手册](help.md)，部署配置见[环境与部署指南](environment.md)。

## 组件

| 组件 | 职责 |
| --- | --- |
| FastAPI 应用 | 管理生命周期，提供 `GET /healthz` 和 Webhook 入口 |
| Webhook / 长连接接收器 | 接收飞书事件并转换为统一事件结构 |
| `EventService` | 按 `event_id` 抢占并去重事件，调用消息处理器 |
| `MessageProcessor` | 归一化文字、图片、音频和富文本消息，下载媒体、调用 AI、执行业务动作并回复飞书 |
| `AIInterpreter` | 按独立配置路由文字、单图/多图和语音服务，解析单笔、复杂文字批量或最多 30 笔图片流水，并生成聚合消费建议 |
| `ExchangeRateService` | 获取并缓存外币到默认账本币种的最新参考汇率 |
| `LedgerService` | 执行固定的记账、修改、撤销、汇总、预算和报告逻辑 |
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
                                      文本或报告卡片回复
```

Webhook 端点完成来源校验、请求解析和后台任务登记后立即确认回调。长连接 SDK 在线程中维护连接，把事件安全转交给 ASGI 事件循环。两种入口最终都调用同一个 `EventService` 和 `MessageProcessor`。

飞书 `post` 富文本会先归一化：保留标题、正文、链接文字和备注文字，忽略 `@` 与样式节点，并按出现顺序提取、去重最多 5 个图片 Key。只有文字时走文字模型；包含图片时并行下载全部图片，再把正文和图片作为一次视觉请求处理。超过图片上限或任一下载、格式校验失败时不会执行账本动作。

## 事件幂等与可重放载荷

收到事件后，`EventService` 在 **T1（领取事务）** 中把 `event_id` 与一份版本化的可重放 JSON 载荷写入 `processed_events` 并提交。主键冲突表示事件已经领取，本次投递不再处理。新账目还会保存飞书 `message_id`，并通过唯一约束避免同一来源消息重复创建。

### 事务边界（v0.2.0 / P00）

```text
T1  claim：insert processed_events(event_id, payload_json, …) → commit
T2  process：从数据库读回载荷 → 反序列化为业务事件 → 同步 MessageProcessor.process
```

- T1 成功只表示“事件已被领取且载荷已落库”，**不是**业务成功。
- T2 仍可能失败（AI、数据库、回复等）。失败时状态可记为 `failed` 并写入有限的 `last_error_code`（异常类型名），但 **不会** 取消 claim。
- **当前仍是 claim-first**：同一 `event_id` 的重投不会自动重试 T2。
- **本版本没有** Worker 轮询、lease、自动 retry、死信队列或回复 Outbox；**不**宣称 at-least-once，也**不**解决“已入账但回复失败”。
- 未来 **v0.2.1** 将基于已持久化的 payload 与状态实现可靠投递与补偿。

### 载荷内容与隐私

载荷由 `event_payload` 模块集中构建与校验，当前 `payload_version = 1`。信封字段包括：`payload_version`、`event_id`、`transport`（`webhook` | `websocket`）、`received_at`，以及归一化后的业务 `event`（`sender.sender_id` 的 open_id/user_id，`message` 的 message_id / message_type / content / 可选 chat_id）。

- **会持久化（重放必需）：** 消息正文 JSON 字符串、图片/文件 **资源标识**（如 `image_key`、`file_key`）、发送者 open_id 等。
- **不会持久化：** App Secret、Verification Token、Encrypt Key、Authorization、Webhook 签名 Header、完整 HTTP Request、SDK 实例、图片/音频 **二进制**。

因此 PostgreSQL 与其备份应按**敏感财务数据**保护。应用日志只记录 `event_id`、`message_id`、`transport`、异常类型等标识，**不**完整 dump 消息正文或 payload。

### 媒体重取限制

图片与语音处理在运行时通过飞书 `messages/{message_id}/resources/{file_key}` 重新下载。载荷只保存资源标识。飞书侧资源是否长期可下载取决于开放平台保留策略与机器人权限；**P00 不保证**历史媒体在任意时刻仍可取回。未来 Worker 重放媒体事件时可能因资源过期而失败，需要单独运维策略。

### 历史行

升级前仅有 `event_id` / `processed_at` 的行在迁移后标记为 `status=legacy_succeeded` 且 `payload_json IS NULL`，**不可重放**，仅保留去重语义。

## AI 与数据库边界

AI 只允许返回 `ParsedCommand` 定义的字段，额外字段会被拒绝。支持的动作是：

- 新增账目
- 批量新增最多 30 笔文字账目，并可同时设置最多 10 项预算
- 修改或撤销最近一笔
- 查询汇总或生成报告
- 设置、查看或删除分类月预算
- 返回帮助

Schema 不包含 SQL、表名、任意过滤表达式或数据库标识。`LedgerService` 把已校验动作映射为固定 SQLAlchemy 查询，并始终带上当前用户的 `open_id` 边界。

报告建议使用文字 AI 配置，只发送币种、分类合计、趋势、收入、支出、结余和记录数等聚合数据，不发送逐笔备注或用户标识。文字、图片（包括富文本正文与最多 5 张图片）和音频分别发送给部署者配置的文字、视觉和转写服务；图片或语音服务未配置时不会回退到文字模型。

## 数据模型

- `ledger_entries`：账目、用户、**用户内唯一五位 `short_id`（聊天引用层）**、金额、币种、分类、备注、发生时间、来源消息、来源项序号和软删除时间；UUID 仍为内部主键；`(user_open_id, short_id)` 与来源消息项唯一。
- `category_budgets`：每个用户和分类唯一的长期月预算。
- `budget_alerts`：记录预算在每个自然月已发送的 80% / 100% 阈值提醒。
- `processed_events`：已领取的飞书事件。新事件含版本化 `payload_json`、`payload_version`、`transport`、`status`、`received_at` 与可选 `last_error_code`；历史无载荷行不可重放。

所有日期范围都使用左闭右开语义。账目发生时间以带时区时间保存；相对时间、自然月和预算统计按全局配置的 IANA 时区计算。

外币代码只存在于结构化指令和确认回复中。业务层在写入前通过进程级汇率缓存将金额约算成管理员配置的默认币种，`ledger_entries.currency` 仍保存默认币种，因此现有汇总、预算和报告不需要进行混合币种聚合。汇率刷新失败时可使用过期缓存；没有任何缓存时操作失败且不会写入数据库。

## 运行与故障边界

- 应用生命周期负责启动或停止长连接，并在关闭时释放数据库引擎。
- Webhook 后台任务和长连接消息任务都运行在 Web 进程内；事件载荷已写入 PostgreSQL，但 **v0.2.0 仍无跨进程 Worker 消费这些载荷**。
- 报告图片渲染或上传失败时会发送不含图片的文字卡片；建议生成失败时使用本地规则生成后备建议。
- 复杂文字以及单图或多图中的批量账目先逐项严格校验，再用数据库保存点隔离单项写入，最终统一提交并返回成功、失败和收支合计。复杂文字中的预算也逐项隔离处理。所有批量账目共用原始消息的 `message_id` 和逐项索引，沿用来源幂等约束。完整异常只记录到带错误编号和处理阶段的日志中，用户回复仅包含可执行的分类错误。
- PostgreSQL 不由当前 Compose 文件管理，迁移在应用容器启动 Uvicorn 前执行。
- 高可用部署需要自行设计持久队列、重试、可观测性、数据库备份和凭据轮换。
