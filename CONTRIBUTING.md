# 参与贡献

[English](CONTRIBUTING.en.md) | 简体中文

感谢你帮助改进 LarkLedger。较大的功能或行为变更请先通过 Issue 讨论；小型修复和文档改进可以直接提交 Pull Request。

## 开发环境

需要 Python 3.11 或 3.12，以及可访问的 PostgreSQL。仓库使用 `src` 布局、Alembic 迁移和异步 SQLAlchemy。仅修改业务逻辑时可以运行 SQLite 快速测试；涉及迁移、约束、并发或幂等行为时必须运行 PostgreSQL 集成测试。

```bash
python -m venv .venv
# Linux / macOS
source .venv/bin/activate
# Windows PowerShell
.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
cp .env.example .env
alembic upgrade head
```

填写 `.env` 时使用测试应用、测试数据库和测试 AI 凭据，不要使用生产账本。长连接和 Webhook 的配置见[环境与部署指南](docs/environment.md)。

## 开发流程

1. Fork 仓库，从 `main` 创建主题分支。
2. 保持改动聚焦；行为变化应补充自动化测试，数据库结构变化应增加 Alembic 迁移。
3. 更新受影响的 README、环境配置、用户手册或架构说明。
4. 运行与改动相关的测试，并在提交前完成全部质量检查：

```bash
ruff check .
mypy src
pytest --cov
```

5. 在 PR 中说明问题、实现方式、验证结果，以及配置、迁移、安全或兼容性影响。

CI 会在 Python 3.11 和 3.12 上运行 Ruff、mypy 和 SQLite 快速测试，并在 PostgreSQL 16 上单独验证迁移、约束和并发行为。覆盖率不得低于 88%。

## 文档事实来源

- 用户可见的命令、回复和使用限制以 `docs/help.md` 为准；README 只保留能力摘要和上手入口。
- 环境变量以 `src/lark_ledger/config.py` 的代码默认值为准，`docs/environment.md` 负责解释，`.env.example` 提供可复制的推荐配置。
- 运行架构、幂等和故障边界以 `docs/architecture.md` 为准；威胁模型和披露流程以 `SECURITY.md` 为准。
- 改动配置时同步检查 `config.py`、`.env.example` 和环境指南；改动用户行为时同步检查用户手册；改动发布或迁移行为时同步更新 `CHANGELOG.md` 与升级指南。

项目文档以中文为主，README 和贡献指南提供英文版本。术语统一使用“长连接模式（WebSocket）”和“Webhook 模式”。仅修改文档的 Pull Request 同样欢迎，但仍需运行链接检查和与改动相关的基础校验。

## 设计原则

- PostgreSQL 是账本的唯一事实来源，数据库变更必须可迁移。
- AI 只输出最小化、严格校验的业务动作，不能接触数据库连接或执行 SQL。
- 所有账本、预算、报告和修改操作必须按飞书用户标识隔离。
- Webhook 与长连接必须共用事件幂等和业务处理流程，不能绕过来源校验或 `event_id` 去重。
- 金额、时间、软删除、预算阈值和多用户隔离等关键行为必须有自动化测试。
- 默认保持自托管，避免引入强制托管服务依赖。
- 日志和异常不得泄露消息正文、媒体内容、Token、密钥或数据库连接串。

更多上下文见[架构说明](docs/architecture.md)和[安全策略](SECURITY.md)。

## 提交与隐私

使用清晰、聚焦的提交。不要提交 `.env`、真实凭据、数据库备份、用户账本、聊天消息、媒体文件或任何可识别个人身份的数据；测试夹具必须完全使用虚构信息。

参与项目即表示你同意按项目的 [Apache License 2.0](LICENSE) 贡献代码与文档。

参与讨论和贡献还应遵守[社区行为准则](CODE_OF_CONDUCT.md)。
