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
