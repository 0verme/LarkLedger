# 升级指南

LarkLedger 当前处于 `0.x` Alpha 阶段。最新发布版本和 `main` 接受修复，旧版本不承诺长期维护。生产部署应固定镜像或 Git tag，不要长期跟随未固定的 `latest` 或任意提交。

## 升级前

1. 阅读 [CHANGELOG](../CHANGELOG.md)，确认配置、行为和迁移影响。
2. 备份 PostgreSQL，并验证备份可以恢复。
3. 记录当前 Git tag 或 `ghcr.io/0verme/larkledger` 镜像标签。
4. 使用当前版本完成健康检查，并确认没有正在处理的批量消息。
5. 不要在升级过程中运行多个会同时执行迁移的应用副本。

## 使用源码 Compose

```bash
git fetch --tags origin
git checkout v0.1.0
docker compose run --rm app alembic upgrade head
docker compose up -d --build
curl http://localhost:8000/healthz
```

现有 `compose.yaml` 启动命令也会在应用启动前运行迁移；显式运行一次便于在启动服务前发现数据库错误。

## 使用 GHCR 镜像

在 shell 中设置要部署的版本，或将其写入 Compose 使用的环境文件：

```bash
export LARK_LEDGER_IMAGE_TAG=0.1.0
docker compose -f compose.image.yaml pull
docker compose -f compose.image.yaml run --rm app alembic upgrade head
docker compose -f compose.image.yaml up -d
curl http://localhost:8000/healthz
```

PowerShell 使用 `$env:LARK_LEDGER_IMAGE_TAG = "0.1.0"`。

## 验证与回退

- 验证 `/healthz`、事件模式、文字记账、图片/语音专项能力和重复事件去重。
- 应用代码可以退回原 Git tag 或镜像标签；数据库只能在确认对应 Alembic downgrade 安全且已有备份时回退。
- 不要仅回退容器而保留一个旧代码无法理解的新数据库结构。
- 发生迁移失败时保留日志和当前数据库状态，避免反复重跑未知步骤；报告问题时只提供脱敏信息。

## 迁移 `20260805_0004`（事件可重放载荷）

- **升级：** 为 `processed_events` 增加 `payload_json`、`payload_version`、`transport`、`status`、`received_at`、`last_error_code`。已有行写入 `status=legacy_succeeded` 且载荷为空，仅保留 `event_id` 去重，**不可**被未来 Worker 重放。
- **行为：** 新事件在 claim 时持久化归一化业务载荷（可能含消息正文与媒体资源标识）。数据库与备份需按敏感财务数据保护。当前版本仍为 claim-first，**无**自动重试、死信或回复补偿。
- **降级数据损失：** `alembic downgrade` 删除上述新列及其中全部载荷与状态信息；`event_id` / `processed_at` 保留。降级前请确认不再需要这些恢复元数据，并已完成备份。

## 迁移 `20260805_0005`（账目五位短 ID）

- **升级：** 为 `ledger_entries` 增加 `short_id`（五位 Crockford Base32），回填存量行，并建立 `UNIQUE (user_open_id, short_id)` 与 `NOT NULL`。UUID 主键与账目金额等业务字段不变。
- **行为：** 新建账目自动分配用户内唯一短 ID；成功记账/修改上一笔/撤销上一笔的回复会展示 `#XXXXX`。软删除后短 ID 不回收。尚不提供按短 ID 查询或改删命令。
- **降级数据损失：** 删除 `short_id` 列与唯一约束；聊天中的 `#XXXXX` 引用失效。金额与 UUID 保留。

## 迁移 `20260805_0006`（账目 revision 审计）

- **升级：** 新增 `ledger_entry_revisions` 表，保存按短 ID 或「上一笔」进行的修改/删除/恢复前后快照。
- **行为：** 修改、软删除、恢复与 revision 同事务提交；无实际变化或幂等删除/恢复不写 revision。
- **降级数据损失：** 删除 revision 表及全部审计历史；账目本体不变。
