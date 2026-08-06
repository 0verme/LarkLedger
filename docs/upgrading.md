# 升级指南

LarkLedger 当前处于 `0.x` Alpha 阶段。最新发布版本和 `main` 接受修复，旧版本不承诺长期维护。生产部署应固定 Git tag 或 `ghcr.io/0verme/larkledger` 镜像标签，不要长期跟随未固定的 `latest` 或任意提交。

## 当前正式版本

| 项 | 事实 |
| --- | --- |
| 最新正式版本 | **v0.2.0** |
| 包版本 / `__version__` | `0.2.0` |
| Git tag | `v0.2.0` |
| GHCR | `ghcr.io/0verme/larkledger:0.2.0`（亦有 `0.2` / `latest` 由发布流水线写入） |
| Alembic head | `20260806_0007` |
| 推荐首次部署 | 源码 Compose 或固定镜像标签；WebSocket + 文字-only 路径见 [README](../README.md) |

## 升级前

1. 阅读 [CHANGELOG](../CHANGELOG.md)，确认配置、行为和迁移影响。
2. **备份 PostgreSQL**，并验证备份可以恢复。
3. 记录当前 Git tag 或镜像标签。
4. 使用当前版本完成健康检查，并确认没有正在处理的批量消息。
5. 不要在升级过程中运行多个会同时执行迁移的应用副本。

## 使用源码 Compose

```bash
git fetch --tags origin
git checkout v0.2.0
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
export LARK_LEDGER_IMAGE_TAG=0.2.0
docker compose -f compose.image.yaml pull
docker compose -f compose.image.yaml run --rm app alembic upgrade head
docker compose -f compose.image.yaml up -d
curl http://127.0.0.1:8000/healthz
```

PowerShell：`$env:LARK_LEDGER_IMAGE_TAG = "0.2.0"`。

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

## 开发中 main（P05b 事件 Worker）

main 分支已加入后台事件 Worker（P05b），**本阶段不新增迁移**，复用 `20260806_0007` 的字段与索引。

- **默认行为变化：** `LARK_LEDGER_WORKER_ENABLED` 默认 `true`。升级后入口（Webhook 后台任务 / WebSocket 回调）只负责领取事件并立即返回，处理由进程内 Worker 完成（领取 → 租约 → 指数退避重试 → dead）。若想保持 v0.2.0 的进程内同步处理，显式设置 `LARK_LEDGER_WORKER_ENABLED=false`。
- **存量事件：** 升级前已处于 `received` 的事件会被 Worker 自动领取处理；已 `succeeded` / `dead` / `legacy_succeeded` 的事件不会被领取。历史 `failed` 且未设 `next_attempt_at` 的行不会被 Worker 自动捞取（避免重放旧错误），如需处理请人工介入（本版本无重放命令）。
- **失败处理：** 可重试错误写入 `failed` 并按指数退避重试（默认最多 3 次），永久错误（payload 损坏 / 契约错误 / 重复约束 / 非 429 的 4xx）直接进入 `dead`。崩溃后未完成的事件在租约过期（默认 300 秒）后由其他 Worker 接管。
- **幂等边界（诚实声明）：** 本版本没有 Transactional Outbox，业务写入与事件状态不是原子提交；重复处理由现有 `(source_message_id, source_item_index)` 唯一约束阻止重复入账，但**不**宣称"绝不重复记账"。没有回复自动补偿、没有人工重放 `dead`。
- **回退：** 代码回退到 `v0.2.0` tag 即可关闭 Worker 行为；数据库结构不变，无需降级。

## 迁移 `20260806_0008`（Transactional Outbox / 回复 Outbox）

- **升级：** 新增 `reply_outbox` 表，保存自包含的飞书回复意图（`event_id`、`message_id`、`reply_type`、`sequence`、`payload_json` 信封、`payload_blob` 文件/图片字节、`status`、`attempt_count`、P06b 预留的 `next_attempt_at` / `lease_owner` / `lease_expires_at`、`sent_at`、脱敏 `last_error_code` / `result_summary`）。唯一约束 `(event_id, reply_type)` 保证同一事件不重复插入同一回复；`(status, next_attempt_at)` 与 `lease_expires_at` 索引为后续 P06b Worker 铺路。已有 `processed_events` 行不受影响，无需回填。
- **行为：** 业务变更与回复意图在同一事务提交；`succeeded` 语义变为"业务已处理且回复意图已可靠写入 Outbox"。崩溃窗口重试不会重复执行业务。提交后会同步尝试发送一次，发送失败把 Outbox 标记为 `failed`（本版本无后台重试）。
- **降级数据损失：** `alembic downgrade` 删除 `reply_outbox` 表及全部待发送回复意图（`pending` / `failed` 行被丢弃，其回复需要重新生成）。降级前请确认备份。

## 开发中 main（P06a Transactional Outbox）

main 分支在 P05b 之上加入了 Transactional Outbox（P06a），新增迁移 `20260806_0008`。

- **业务 + Outbox 同事务：** `MessageProcessor` 以 `LedgerService(commit_changes=False)` 执行业务（只 flush），生成回复意图并插入 `reply_outbox`，与业务同一 `commit` 提交。业务成功 ⟹ 一定有 Outbox 记录。
- **succeeded 语义：** 事件 `succeeded` 表示"业务已处理且回复意图已可靠入 Outbox"，不再表示"飞书已收到回复"。飞书发送失败记录在 Outbox（`failed`），不会让事件进入业务重试。
- **崩溃恢复：** 业务 + Outbox 已提交而事件状态未更新时，事件被重新领取会先检查 Outbox，跳过业务并收敛为 `succeeded`（不再以 `IntegrityError→dead` 作为正常恢复路径）。
- **兼容单次发送：** 提交后从已提交的 Outbox 同步发送一次：成功标记 `sent`，失败标记 `failed`；无后台回复 Worker、无自动重试、无 Outbox lease（P06b 范围）。
- **回退：** 代码回退到 `v0.2.0` tag 即可关闭 Worker 与 Outbox 行为；若已运行 `0008`，数据库需显式 `alembic downgrade 20260806_0007`（会丢弃待发送回复意图），或先备份后处理。

当前 Alembic head：`20260806_0008`。
