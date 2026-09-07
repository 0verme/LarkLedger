# 飞牛 NAS Release 镜像生产部署

本文档是 LarkLedger 在飞牛 NAS 上的生产部署路径。生产环境使用 GitHub Release 发布的 GHCR 固定版本镜像，不在 NAS 上 `git pull` 或重新 build。

## 首次部署

1. 准备应用目录（包含当前 Release 的 `compose.image.yaml` 与 `scripts/`），复制 `.env.example` 为 `.env`，填写真实的 PostgreSQL、飞书和 AI 配置。
2. 确认 NAS 可以拉取 `ghcr.io/0verme/larkledger:X.Y.Z`，并且 `.env` 中的 `LARK_LEDGER_DATABASE_URL` 指向可从容器访问的 PostgreSQL。当前仓库的生产 Compose 不管理 PostgreSQL；备份脚本使用 `pg_dump` 直连该数据库。
3. 如需改变备份位置，在 `.env` 或 shell 环境中设置 `LARK_LEDGER_BACKUP_DIR`。默认目录是应用目录下的 `backups/`。可用 `LARK_LEDGER_BASE_URL` 覆盖验收地址，默认是 `http://127.0.0.1:8000`。
4. 执行明确的三段版本号：

   ```bash
   ./scripts/deploy-fnos.sh 0.14.0
   # v0.14.0 也可以，脚本会规范化为 0.14.0
   ```

部署脚本会加锁、拉取固定 GHCR tag、读取旧状态、执行 custom-format PostgreSQL backup，再用目标镜像运行 `alembic upgrade head`，启动 app，检查 `/healthz`、`/readyz`、`/version`、`/ops/status`，全部通过后才原子更新 `.deploy-state/current.env`。

## 日常升级

GitHub Release 与 GHCR 镜像准备完成后，在 NAS 执行：

```bash
./scripts/deploy-fnos.sh X.Y.Z
```

例如：

```bash
./scripts/deploy-fnos.sh 0.14.0
```

`latest`、`main`、`master`、`HEAD`、`develop`、浮动的 `X.Y` 和明显非法版本都会被拒绝。生产 Compose 不包含 `build:`，部署不会从 NAS 源码构建镜像。

## 部署验收

可以单独重复执行有限重试的验收：

```bash
./scripts/ops/verify-deployment.sh X.Y.Z
```

不传版本时只检查端点可访问：

```bash
./scripts/ops/verify-deployment.sh
```

验收至少要求：

- `/healthz` 返回 HTTP 200；
- `/readyz` 返回 HTTP 200，并确认数据库 migration current；
- `/version` 返回目标 `version`，并输出 `git_sha`、`build_time`；
- `/ops/status` 可访问。

## Rollback

仅当脚本能证明**当前数据库 revision 等于目标旧镜像的单一 Alembic head** 时，才允许 code-only rollback：

```bash
./scripts/rollback-fnos.sh 0.13.0
```

脚本会先拉取目标旧镜像、读取其 `alembic heads`，在没有 schema 变化的证明前不会切换 app；通过证明后仍会先 backup，再启动旧镜像并执行同一套验收。rollback 成功才更新 deployment state。

**镜像 rollback != 数据库 rollback。** 如果 target head 与当前数据库 revision 不同、head 无法读取或存在多个 head，脚本会输出 `SCHEMA-CHANGING ROLLBACK` 并停止，不会自动执行 `alembic downgrade`。此时先备份现状，再按照 [backup-restore.md](backup-restore.md) 和 [release-sop.md](release-sop.md) 的人工 restore / rollback 流程处理数据库。

## 禁止

- 生产使用 `latest`、`main`、`master`、`HEAD`、`develop` 或浮动 `X.Y`；
- 生产执行 `git pull main` 后在 NAS 本地 build；
- 绕过部署前 PostgreSQL backup；
- migration 未完成或验收未通过就宣布部署成功；
- 为了回滚自动执行未经证明安全的 `alembic downgrade`；
- 将 `.env`、dump、密码、Token 或 deployment state 提交到 Git。

源码 `compose.yaml` 仍可用于本地开发和经过审查的 advanced source deployment，但不是 FNOS production 首选。
