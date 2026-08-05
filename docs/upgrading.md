# 升级指南

LarkLedger 当前处于 `0.x` Alpha 阶段。最新发布版本和 `main` 接受修复，旧版本不承诺长期维护。生产部署应固定 Git 提交、镜像标签或未来的正式 Release tag，不要长期跟随未固定的 `latest` 或任意提交。

## 版本与镜像诚实说明

| 项 | 事实 |
| --- | --- |
| 包版本 / `__version__` | 当前仓库仍为 `0.1.0` |
| `main` 上的可核对账本能力 | 短 ID、列表/详情、定点改删恢复、CSV 等已合入，对应规划中的 **v0.2.0 能力集**，但**尚未**以 `v0.2.0` Tag / GitHub Release 发布 |
| GHCR | 不要假设 `ghcr.io/0verme/larkledger:0.1.0` 或 `:0.2.0` 一定可 pull，除非你在干净环境自行验证 |
| 推荐部署 | 在正式 Release 前优先使用源码 `docker compose ... --build` |

## 升级前

1. 阅读 [CHANGELOG](../CHANGELOG.md)，确认配置、行为和迁移影响。
2. **备份 PostgreSQL**，并验证备份可以恢复。
3. 记录当前 Git 提交、tag 或镜像标签（若有）。
4. 使用当前版本完成健康检查，并确认没有正在处理的批量消息。
5. 不要在升级过程中运行多个会同时执行迁移的应用副本。

## 使用源码 Compose

```bash
git fetch origin
git checkout <已验证的提交或未来正式 tag>
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

仅在你已确认目标标签可 pull 时使用：

```bash
export LARK_LEDGER_IMAGE_TAG=替换为已验证标签
docker compose -f compose.image.yaml pull
docker compose -f compose.image.yaml run --rm app alembic upgrade head
docker compose -f compose.image.yaml up -d
curl http://127.0.0.1:8000/healthz
```

PowerShell：`$env:LARK_LEDGER_IMAGE_TAG = "替换为已验证标签"`。

`compose.image.yaml` **不会**在 `up` 时自动迁移；升级必须显式 `alembic upgrade head`。

## 验证与回退

- 验证 `/healthz`、事件模式、**文字记账与短 ID 回复**、（如启用）图片/语音、重复事件去重。
- 建议按[环境指南](environment.md)做一笔最小验收：`午饭32元` → `最近10笔` → 按短 ID 查看/修改。
- 应用代码可以退回原 Git 提交或镜像标签；数据库只能在确认对应 Alembic downgrade 安全且已有备份时回退。
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

当前 Alembic head：`20260805_0006`。
