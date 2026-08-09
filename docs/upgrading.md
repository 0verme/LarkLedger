# 升级指南

LarkLedger 当前处于 `0.x` Alpha 阶段。最新发布版本和 `main` 接受修复，旧版本不承诺长期维护。生产部署应固定 Git tag 或 `ghcr.io/0verme/larkledger` 镜像标签，不要长期跟随未固定的 `latest` 或任意提交。

## 当前正式版本

| 项 | 事实 |
| --- | --- |
| 最新正式版本 | **v0.4.0** |
| 包版本 / `__version__` | `0.4.0` |
| Git tag | `v0.4.0` |
| GHCR | `ghcr.io/0verme/larkledger:0.4.0`（亦有 `0.4` / `latest` 由发布流水线写入） |
| Alembic head | `20260809_0017` |
| 推荐首次部署 | 源码 Compose 或固定镜像标签；WebSocket + 文字-only 路径见 [README](../README.md) |

## 升级前

1. 阅读 [CHANGELOG](../CHANGELOG.md)，确认配置、行为和迁移影响。
2. **备份 PostgreSQL**，并验证备份可以恢复。
3. 记录当前 Git tag 或镜像标签。
4. 使用当前版本完成健康检查，并确认没有正在处理的批量消息。
5. 不要在升级过程中运行多个会同时执行迁移的应用副本。

## 从 v0.3.0 升级到 v0.4.0

阶段 3 migration `20260809_0017` 在 `0016` 之后新增 `households`、`household_members`、`household_invitations`，并允许 Ledger 以显式 `household_id` 作为 `household_shared` 授权根。升级不修改已有 User、个人 Ledger、ChannelIdentity、DashboardSession 或财务数据。升级后核对每个家庭只有一个 active owner 和一个 shared ledger；个人默认账本仍必须为 personal。

降级到 `0016` 只在不存在家庭公共账本时执行，以避免把共享账本伪装成某个个人所有或删除财务数据。若已经启用家庭空间，先备份并完成明确的家庭数据退役方案；migration 会拒绝破坏性降级，不会自动删除或转移历史账目。

当前 `Unreleased` 的阶段 2 migration `20260809_0016` 在 `0015` 身份地基之上增加账本规范化名称和飞书入口当前账本，并回填所有入口为用户默认账本。它还把短 ID、预算分类与活跃媒体指纹唯一约束改为账本作用域，使同一用户的不同账本可以安全复用。阶段 1 的可空兼容列本阶段继续保留，以便旧测试夹具和滚动升级；`0015` 已回填全部历史行，应用的新写入始终提供经过授权的 `ledger_id`。

升级前必须备份并执行 `alembic upgrade head`。升级后核对 `alembic current` 为 `20260809_0016`，再分别在两个账本创建同分类预算、同短 ID 测试账目以及 Pending。降级到 `0015` 会移除入口选择和规范化名称；若多账本数据已经产生旧键冲突，降级会保留全部财务数据并不恢复无法无损建立的旧用户级唯一约束，因此代码回退前应先评估兼容性。

v0.4.0 新增可选 Web Dashboard 与迁移 `20260808_0014`（`dashboard_sessions`）。先备份 PostgreSQL，再拉取 `v0.4.0` 或固定镜像 `ghcr.io/0verme/larkledger:0.4.0`，然后执行 `alembic upgrade head`。Dashboard 默认关闭，所以只升级代码和 migration 不会改变机器人、Worker 或现有公网路由。

当前 `Unreleased` 的迁移 `20260809_0015` 增加内部 `User`、`ChannelIdentity`
和默认个人 `Ledger`。升级会按现有 `user_open_id` 无损创建映射并回填账目、预算、
revision、Pending 与 Dashboard Session；旧列在本次扩展迁移中保留，以便回滚和核验。
升级前仍应备份 PostgreSQL。升级后可核对每个 `channel_identities` 均有且仅有一个
默认个人账本，再开始后续多账本功能。

需要启用 Dashboard 时，再配置 HTTPS origin、强随机 Session Secret、飞书 OAuth 回调与管理员 open_id；完整清单见[环境与部署指南 · Web Dashboard](environment.md#web-dashboard可选)。生产镜像已经通过 Node build stage 嵌入静态资源，运行容器不包含 Node server。

验收至少包括：OAuth 登录、当前用户账目隔离、修改后 revision、删除/恢复、pending 确认、CSV 下载、管理员健康与 replay dry-run；随后在飞书发送一笔文字记账确认机器人路径未回归。再以 `DASHBOARD_ENABLED=false` 重启一次，确认 `/api/web/v1/*` 不暴露且飞书仍正常。

若回退到 v0.3.0，先关闭 Dashboard。保留 `dashboard_sessions` 表不会影响旧代码；若必须 downgrade，会删除所有 Web 会话，但不会删除账目、revision、pending、Event 或 Outbox 数据。

## 从 v0.2.1 升级到 v0.3.0

v0.3.0「高风险确认」新增迁移 `20260806_0012` 和 `pending_commands` 表。升级前请备份 PostgreSQL，并确认备份可以恢复；不要把事件清理或尚未实现的 P10 备份脚本当作数据库备份。

1. 拉取 `v0.3.0` 源码或固定镜像 `ghcr.io/0verme/larkledger:0.3.0`。
2. 在启动新应用前执行 `alembic upgrade head`；唯一新 head 必须是 `20260807_0013`。
3. 核对确认配置：`LARK_LEDGER_PENDING_ENABLED`（默认 `true`）、`PENDING_EXPIRES_SECONDS`（默认 86400）、`PENDING_RETENTION_DAYS`（默认 7）、`PENDING_DUPLICATE_WINDOW_MINUTES`（默认 60）和 `PENDING_MAX_LIST`（默认 10）。
4. 重启并检查 `/healthz`、`/readyz`，再验证一笔简单文字直写和一条图片/语音/批量待确认路径。

简单明确的单笔文字（如 `午饭32元`）行为不变，仍然直接入账。图片、语音、批量和疑似重复写入会先建立待确认预览，使用 `确认 #C-XXXXX` 或卡片按钮后才写账；`取消 #C-XXXXX` 不写账。确认执行冻结的结构化命令，不重新调用 AI。

回滚代码前应停止应用并再次备份数据库。若仅回退应用代码而保留数据库，旧代码不会使用 `pending_commands`；若执行 `alembic downgrade 20260806_0011`，会删除 `pending_commands` 表及其中全部 pending / executed / cancelled / expired / failed 确认记录、冻结命令和预览。已经确认后写入的账目不会因该 downgrade 自动删除。需要审计或恢复这些确认数据时，不得执行该 downgrade。

## 从 v0.2.0 升级到 v0.2.1

v0.2.1「可靠投递」新增迁移 `20260806_0008`～`20260806_0011`（回复 Outbox、投递元数据、清理索引、重放状态）。`alembic upgrade head` 会依次应用；关键行为变化：

- **入口模式**：`LARK_LEDGER_WORKER_ENABLED`（默认 `true`）与 `LARK_LEDGER_REPLY_WORKER_ENABLED`（默认 `true`）使事件与回复由后台 Worker 处理。若想保持 v0.2.0 的进程内同步处理，显式设回 `false`。
- **succeeded 语义**：`succeeded` 表示"业务已处理且回复意图已可靠入 Outbox"，不再表示"飞书已收到回复"。业务写入与回复意图同事务提交。
- **事件重试与 dead**：`failed` 事件按指数退避自动重试（默认最多 3 次），永久错误进入 `dead`；已 `succeeded` / `dead` 的历史事件不会被领取。
- **回复重试与 dead**：`pending` / 到期 `failed` 的 Outbox 行由 Reply Worker 自动投递，临时错误按退避重试，永久错误进入 `dead`；发送失败**绝不**重新执行业务。
- **readiness**：升级后可用 `GET /readyz` 检查数据库、revision、Worker 与接收器；`/healthz` 保持原语义。
- **清理**：`LARK_LEDGER_CLEANUP_ENABLED` 默认 `true`，成功记录保留 30 天、dead 保留 90 天；需要关闭时显式设 `false`。
- **人工重放**：管理员可用 `python -m lark_ledger.admin replay-event` 重放安全的 `dead` / `failed` 事件（默认 dry-run，需 `--execute`）。

升级步骤：备份 → 拉取 v0.2.1 → `alembic upgrade head` → 重启 → 检查 `/healthz` 与 `/readyz` → 一笔文字记账验收。代码回退到 v0.2.0 tag 即可关闭 Worker / Outbox / Cleanup 行为；若已运行 `0008` 之后的迁移，需显式 `alembic downgrade` 并评估待发送回复意图的丢失。

## 使用源码 Compose

```bash
git fetch --tags origin
git checkout v0.2.1
docker compose run --rm app alembic upgrade head
docker compose up -d --build
curl http://127.0.0.1:8000/healthz
```

现有 `compose.yaml` 启动命令也会在应用启动前运行迁移；显式运行一次便于在启动服务前发现数据库错误。

开发库叠加：

```bash
docker compose -f compose.yaml -f compose.dev.yaml up -d --build
```

## 使用 GHCR 镜像

```bash
export LARK_LEDGER_IMAGE_TAG=0.2.1
docker compose -f compose.image.yaml pull
docker compose -f compose.image.yaml run --rm app alembic upgrade head
docker compose -f compose.image.yaml up -d
curl http://127.0.0.1:8000/healthz
```

PowerShell：`$env:LARK_LEDGER_IMAGE_TAG = "0.2.1"`。

`compose.image.yaml` **不会**在 `up` 时自动迁移；升级必须显式 `alembic upgrade head`。

## 从 v0.1.0 或更早 main 升级到 v0.2.0

1. 备份数据库。
2. 拉取 `v0.2.0` 代码或镜像。
3. 执行 `alembic upgrade head`（将依次应用 `20260805_0004`～`20260806_0007`，若尚未应用）。
4. 重启应用并检查 `/healthz`。
5. 验收：文字记账回复含 `#XXXXX`；`最近10笔`；按短 ID 查看/修改；按需验证 CSV 导出。

新增配置项方面，v0.2.0 **不强制**新增必填环境变量。快速路径仍建议在 `.env` 中显式设置 `LARK_LEDGER_EVENT_MODE=websocket`（代码默认仍为 `webhook`）。

## 验证与回退

- 验证 `/healthz`、事件模式、**文字记账与短 ID 回复**、（如启用）图片/语音、重复事件去重。
- 建议按[环境指南](environment.md)做一笔最小验收：`午饭32元` → `最近10笔` → 按短 ID 查看/修改。
- 应用代码可以退回原 Git tag 或镜像标签；数据库只能在确认对应 Alembic downgrade 安全且已有备份时回退。
- 不要仅回退容器而保留旧代码无法理解的新数据库结构。
- 发生迁移失败时保留日志和当前数据库状态，避免反复重跑未知步骤；报告问题时只提供脱敏信息。

## 迁移 `20260805_0004`（事件可重放载荷）

- **升级：** 为 `processed_events` 增加 `payload_json`、`payload_version`、`transport`、`status`、`received_at`、`last_error_code`。已有行写入 `status=legacy_succeeded` 且载荷为空，仅保留 `event_id` 去重，**不可**被未来 Worker 重放。
- **行为：** 新事件在 claim 时持久化归一化业务载荷（可能含消息正文与媒体资源标识）。数据库与备份需按敏感财务数据保护。当前版本仍为 claim-first，**无**自动重试、死信或回复补偿。
- **降级数据损失：** `alembic downgrade` 删除上述新列及其中全部载荷与状态信息；`event_id` / `processed_at` 保留。降级前请确认不再需要这些恢复元数据，并已完成备份。

## 迁移 `20260805_0005`（账目五位短 ID）

- **升级：** 为 `ledger_entries` 增加 `short_id`（五位 Crockford Base32），回填存量行，并建立 `UNIQUE (user_open_id, short_id)` 与 `NOT NULL`。UUID 主键与账目金额等业务字段不变。
- **行为：** 新建账目自动分配用户内唯一短 ID；成功记账、修改、删除、恢复等回复会展示 `#XXXXX`。软删除后短 ID 不回收。当前已支持按短 ID 列表分页边界、查看详情、修改、删除与恢复。
- **降级数据损失：** 删除 `short_id` 列与唯一约束；聊天中的 `#XXXXX` 引用失效。金额与 UUID 保留。

## 迁移 `20260805_0006`（账目 revision 审计）

- **升级：** 新增 `ledger_entry_revisions` 表，保存按短 ID 或「上一笔」进行的修改/删除/恢复前后快照。
- **行为：** 修改、软删除、恢复与 revision 同事务提交；无实际变化或幂等删除/恢复不写 revision。
- **降级数据损失：** 删除 revision 表及全部审计历史；账目本体不变。

## 迁移 `20260806_0007`（可靠投递事件状态模型）

- **升级：** 为 `processed_events` 增加 `attempt_count`、`next_attempt_at`、`lease_owner`、`lease_expires_at`、`result_summary`、`source_message_id`、`user_open_id`、`updated_at`。存量行安全回填：已进入处理的行（`processing` / `succeeded` / `failed`）`attempt_count=1`，其余为 0；`updated_at` 取 `processed_at`；`source_message_id` / `user_open_id` 从已有 payload 中提取。历史无 payload 行仍为 `legacy_succeeded` 且不可重放。
- **行为：** 当前版本**没有** Worker，`next_attempt_at` / `lease_owner` / `lease_expires_at` 保持 NULL；失败事件只记录脱敏的单行 `result_summary`。**这仍是 claim-first，不代表可靠投递已经完成。**
- **降级数据损失：** `alembic downgrade` 删除上述新列与索引，丢弃重试 / 租约 / 结果元数据与定位列；`payload_json` 与 `status` 保留。

## v0.2.1：事件 Worker（P05b，复用 `20260806_0007`）

main 分支已加入后台事件 Worker（P05b），**本阶段不新增迁移**，复用 `20260806_0007` 的字段与索引。

- **默认行为变化：** `LARK_LEDGER_WORKER_ENABLED` 默认 `true`。升级后入口（Webhook 后台任务 / WebSocket 回调）只负责领取事件并立即返回，处理由进程内 Worker 完成（领取 → 租约 → 指数退避重试 → dead）。若想保持 v0.2.0 的进程内同步处理，显式设置 `LARK_LEDGER_WORKER_ENABLED=false`。
- **存量事件：** 升级前已处于 `received` 的事件会被 Worker 自动领取处理；已 `succeeded` / `dead` / `legacy_succeeded` 的事件不会被领取。历史 `failed` 且未设 `next_attempt_at` 的行不会被 Worker 自动捞取。P06e 虽提供管理员重放命令，但迁移不会猜测历史事件的原子性标记，因此无法证明安全的存量行仍会拒绝并要求人工取证。
- **失败处理：** 可重试错误写入 `failed` 并按指数退避重试（默认最多 3 次），永久错误（payload 损坏 / 契约错误 / 重复约束 / 非 429 的 4xx）直接进入 `dead`。崩溃后未完成的事件在租约过期（默认 300 秒）后由其他 Worker 接管。
- **该历史阶段的幂等边界：** P05b 当时尚无 Transactional Outbox，业务写入与事件状态不是原子提交；重复处理仅由 `(source_message_id, source_item_index)` 唯一约束兜底，也尚无回复补偿或 `dead` 人工重放。后续 `0008` 与 `0011` 分别补上原子 Outbox 与受控重放，但 `0011` 不会把这批历史事件误标为安全。
- **回退：** 代码回退到 `v0.2.0` tag 即可关闭 Worker 行为；数据库结构不变，无需降级。

## 迁移 `20260806_0008`（Transactional Outbox / 回复 Outbox）

- **升级：** 新增 `reply_outbox` 表，保存自包含的飞书回复意图（`event_id`、`message_id`、`reply_type`、`sequence`、`payload_json` 信封、`payload_blob` 文件/图片字节、`status`、`attempt_count`、P06b 预留的 `next_attempt_at` / `lease_owner` / `lease_expires_at`、`sent_at`、脱敏 `last_error_code` / `result_summary`）。唯一约束 `(event_id, reply_type)` 保证同一事件不重复插入同一回复；`(status, next_attempt_at)` 与 `lease_expires_at` 索引为后续 P06b Worker 铺路。已有 `processed_events` 行不受影响，无需回填。
- **行为：** 业务变更与回复意图在同一事务提交；`succeeded` 语义变为"业务已处理且回复意图已可靠写入 Outbox"。崩溃窗口重试不会重复执行业务。提交后会同步尝试发送一次，发送失败把 Outbox 标记为 `failed`（本版本无后台重试）。
- **降级数据损失：** `alembic downgrade` 删除 `reply_outbox` 表及全部待发送回复意图（`pending` / `failed` 行被丢弃，其回复需要重新生成）。降级前请确认备份。

## v0.2.1：Transactional Outbox（P06a）

main 分支在 P05b 之上加入了 Transactional Outbox（P06a），新增迁移 `20260806_0008`。

- **业务 + Outbox 同事务：** `MessageProcessor` 以 `LedgerService(commit_changes=False)` 执行业务（只 flush），生成回复意图并插入 `reply_outbox`，与业务同一 `commit` 提交。业务成功 ⟹ 一定有 Outbox 记录。
- **succeeded 语义：** 事件 `succeeded` 表示"业务已处理且回复意图已可靠入 Outbox"，不再表示"飞书已收到回复"。飞书发送失败记录在 Outbox（`failed`），不会让事件进入业务重试。
- **崩溃恢复：** 业务 + Outbox 已提交而事件状态未更新时，事件被重新领取会先检查 Outbox，跳过业务并收敛为 `succeeded`（不再以 `IntegrityError→dead` 作为正常恢复路径）。
- **兼容单次发送：** 提交后从已提交的 Outbox 同步发送一次：成功标记 `sent`，失败标记 `failed`；无后台回复 Worker、无自动重试、无 Outbox lease（P06b 范围）。
- **回退：** 代码回退到 `v0.2.0` tag 即可关闭 Worker 与 Outbox 行为；若已运行 `0008`，数据库需显式 `alembic downgrade 20260806_0007`（会丢弃待发送回复意图），或先备份后处理。

## 迁移 `20260806_0009`（回复投递元数据 + 顺序索引）

- **升级：** 为 `reply_outbox` 增加可空列 `remote_message_id`（远端回复消息 ID，`sent` 时写入）、`remote_file_key` / `remote_image_key`（上传资源键，上传成功后写入，重试时复用不重复上传），并新增 `(event_id, sequence)` 索引支撑同一事件内按 `sequence` 顺序领取。唯一约束 `(event_id, reply_type)` 不变，已支持全部真实回复组合。历史 `pending` / `sent` / `failed` 行不受影响，无需回填。
- **行为：** 详见下文「v0.2.1：Reply Worker（P06b）」。
- **降级数据损失：** `alembic downgrade` 删除上述三列与顺序索引；已记录的远端消息 ID 与上传资源键丢失（后续投递会重新上传字节，安全但重复），`sent` 消息审计信息减少。待发送意图不丢失。

## v0.2.1：Reply Worker（P06b）

main 分支在 P06a 之上加入后台回复 Worker（P06b），新增迁移 `20260806_0009`。

- **默认行为变化：** `LARK_LEDGER_REPLY_WORKER_ENABLED` 默认 `true`。T2 提交后处理器只唤醒后台 `ReplyWorker`，不再直接发送；Worker 用 `FOR UPDATE SKIP LOCKED` 领取已提交的 Outbox 行、写入租约与 `attempt_count`、上传 / 发送、按租约守卫标记 `sent` / `failed` / `dead`。若想保持提交后同步发送，显式设置 `LARK_LEDGER_REPLY_WORKER_ENABLED=false`（同步路径仍走同一套 claim / 租约 / 结果守卫原语，不会绕过状态守卫）。两种模式不会同时运行。
- **存量 Outbox：** 升级前 `pending` 行会被 Worker 自动投递；P06a 已 `failed`（`attempt_count=1`）的行到期后被 Worker 领取并计为第 2 次尝试；已 `sent` 行不会被重发。
- **回复重试与 dead：** 临时错误按指数退避重试（默认最多 3 次），永久错误（payload 版本 / 类型不支持、契约损坏、blob 缺失或 checksum 不一致、非 408/429 的 4xx）或重试耗尽写入 `dead`。发送失败**绝不**重新执行业务。进程重启后继续投递 `pending` / `failed`。
- **幂等（诚实声明）：** 每次回复携带飞书回复 API 的 `uuid` 幂等键（Outbox 行 ID），1 小时内重发由飞书去重；极端情况下（飞书已发送但本地未标记 `sent` 后崩溃，且重发间隔超过 1 小时）用户可能收到重复回复，但**绝不会**重复执行业务或重复记账。
- **回退：** 代码回退到含 `0008` 的提交即可关闭 Reply Worker（若 `REPLY_WORKER_ENABLED=false` 不启用）与 `0009` 新增列的行为；若已运行 `0009`，数据库需显式 `alembic downgrade 20260806_0008`（丢弃投递元数据，意图不丢失），或先备份后处理。

## v0.2.1：Readiness（P06c）

P06c 不新增迁移（v0.2.1 head 为 `20260806_0011`）。升级代码并重启后可使用
`GET /readyz` 检查 PostgreSQL、数据库 revision、已启用的 Event / Reply Worker，以及
WebSocket 模式下的接收器。数据库未初始化、revision 落后或领先、代码存在多个 head、
Worker task 异常退出、receiver 未启动或应用正在 shutdown 时返回 HTTP 503。

`GET /healthz` 保持原响应格式与 liveness 语义，不访问数据库。`/readyz` 只做本地轻量
检查，不自动运行 migration，也不探测飞书或 AI。升级脚本仍需先显式执行
`alembic upgrade head`；readiness 不能替代迁移步骤。

## 迁移 `20260806_0010`（终态清理索引）

- **升级：** 为 Event 的 `(status, processed_at)` / `(status, updated_at)` 和 Outbox 的
  `(status, sent_at)` / `(status, updated_at)` 增加 4 个索引，支撑 P06d 小批量保留期扫描。
  迁移不改写、不删除任何数据。
- **默认行为：** `CLEANUP_ENABLED=true`，成功 Event / sent Outbox 保留 30 天，dead Event /
  dead Outbox 保留 90 天，每小时按每类最多 500 行的短事务清理。保留期必须至少 1 天；
  需要关闭时显式设置 `CLEANUP_ENABLED=false`。
- **安全边界：** 只删除终态投递记录，不删除账本、revision、非终态、有效 lease 或仍有关联
  Outbox 的 Event。清理不是备份；调整期限前评估审计需求。
- **回退：** `alembic downgrade 20260806_0009` 只删除 4 个清理索引，不恢复已经按配置过期
  并由应用清理的数据。若要回退代码并停止后续清理，应先设置 `CLEANUP_ENABLED=false` 并备份。

## 迁移 `20260806_0011`（受控事件重放）

- **升级：** 为 `processed_events` 增加 `manual_replay_count`、可空 `replay_safety_version`
  与可空 `business_committed_at`，并新建不保存 payload 的 `event_replay_audits` 审计表。
- **历史安全边界：** 存量事件的 `replay_safety_version` 保持 NULL；迁移无法证明旧业务写入
  是否与 Outbox 原子提交，因此不会把历史失败事件自动标记为可重放。升级后新接收事件由应用写入
  当前安全版本。迁移只对「存在关联 Outbox」的存量事件回填 `business_committed_at`，其余存量行
  保持 NULL 且不猜测——该证据与业务、Outbox 同事务写入，在 Outbox 被终态清理删除后仍可拒绝
  自动 Worker 与人工重放重复执行业务。
- **操作：** `python -m lark_ledger.admin replay-event ...` 默认 dry-run；只有 `--execute`
  会在 `FOR UPDATE` 后再次预检，并把审计与 `received` 状态重置放在同一事务。任何已有 Outbox
  或来源账目结果都会拒绝重新执行业务。
- **尝试语义：** `attempt_count` 是当前自动重试窗口，人工重放时归零；历史值保存在审计，
  `manual_replay_count` 累计重放次数。Worker 领取后从第 1 次开始，仍受原最大尝试数约束。
- **回退：** `alembic downgrade 20260806_0010` 会删除重放审计与两个重放元数据列，但不修改
  账本、Outbox 或事件 payload。需要保留审计时不得执行该 downgrade。

## 迁移 `20260806_0012`（高风险确认 pending_commands）

- **升级：** 新增 `pending_commands` 表，保存**冻结**的高风险命令（图片 / 语音 / 批量 / 疑似重复）等待用户确认。`payload_json` 是冻结的 `ParsedCommand`（确认绝不重新调用 AI）；`preview_json` 是冻结的用户预览聚合。确认单编号存储为 `CA83F2`（展示 `#C-A83F2`），用户内唯一、不复用。`source_event_id` 无外键，事件清理不会级联删除待确认单。
- **行为变化（v0.3.0）：** `LARK_LEDGER_PENDING_ENABLED` 默认 `true`，图片 / 语音 / 批量 / 疑似重复写入先进入待确认；简单单笔文字仍直写。确认 / 取消可用文本命令 `确认 #C-XXXXX` / `取消 #C-XXXXX` 或预览卡片按钮。确认单默认 24 小时过期，终态确认单默认保留 7 天后由 Cleanup Worker 清理。
- **回退：** `alembic downgrade 20260806_0011` 删除 `pending_commands` 表及全部待确认单（已确认执行写入的账目不受影响）。需要保留待确认数据时不得执行该 downgrade。

## 迁移 `20260807_0013`（图片待确认去重）

- **升级：** `pending_commands` 新增可空的 `source_fingerprint`，并为同一用户的活跃指纹增加部分唯一索引。指纹是带版本与边界的 SHA-256，只保存摘要，不保存图片字节。
- **行为变化：** 完全相同的单图或富文本多图在已有 `pending` / `executing` 确认单时不再重复识别、建单或发卡。确认单进入终态后可以再次发送。`撤销 #C-XXXXX` 等同于取消确认单，不会撤销最近账目。
- **存量数据：** 旧行没有原始图片可用于安全回填指纹，因此保持 `NULL`，不自动合并或删除。
- **回退：** 降级到 `20260806_0012` 会删除指纹索引和字段，不修改待确认状态或账本。

当前 Alembic head：`20260809_0017`。
