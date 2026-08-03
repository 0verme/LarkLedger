# LarkLedger（飞账）

> 自托管的飞书 / Lark AI 记账机器人。通过文字、语音、小票照片或支付截图完成记账、查询、修改、撤销、分类月预算和消费报告。

[![CI](https://github.com/0verme/lark-ledger/actions/workflows/ci.yml/badge.svg)](https://github.com/0verme/lark-ledger/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)

LarkLedger 将账本保存在你自己的 PostgreSQL 中。大模型只负责把消息转换成经过严格校验的业务动作：它拿不到数据库连接，不能生成或执行 SQL，所有读写都由项目中预先定义的参数化查询完成。

## 功能

- 文字、语音、图片和图文富文本记账：`昨天打车38.5`、`工资到账10000`
- 复杂文字批量记账：一条消息可处理最多 30 笔收支，并可同时设置最多 10 项预算
- 文字 + 图片记账：一条飞书富文本可附加说明并包含最多 5 张账单图片
- 批量图片记账：从支付流水截图中逐笔校验并记录最多 30 笔独立交易
- 修改或撤销最近一笔：`上一笔改成8块`、`撤销刚才那笔`
- 外币金额约算：`午饭1300日元`、`上一笔改成20美元`
- 按时间、收支方向和分类汇总：`这个月餐饮花了多少`
- 分类月预算：支持一条消息批量设置最多 10 项，达到 80% 和 100% 时分别提醒一次
- 消费报告：展示分类占比、支出趋势、收支对比和消费建议
- 多用户隔离：所有账目操作都以飞书用户 `open_id` 为边界
- 事件幂等：按飞书 `event_id` 去重，避免重复投递造成重复记账
- 自托管：FastAPI、PostgreSQL、Docker Compose

完整使用示例与限制见[飞账用户手册](docs/help.md)。

## 快速开始：长连接部署

长连接无需公网域名、HTTPS 回调或内网穿透，适合个人服务器、NAS 和本地开发。当前 `compose.yaml` 只启动 LarkLedger 应用；开始前请准备一个应用容器可以访问的 PostgreSQL 数据库。

### 1. 准备 PostgreSQL

创建独立数据库和低权限用户，并记下 SQLAlchemy 异步连接地址：

```text
postgresql+asyncpg://用户名:密码@数据库主机:5432/数据库名
```

数据库在宿主机上时，容器内不能用 `localhost` 访问它。Windows 和 macOS 通常可使用 `host.docker.internal`；Linux 或远程数据库请填写容器可达的主机名或私网地址。

### 2. 配置应用

```bash
cp .env.example .env
```

Windows PowerShell 可使用 `Copy-Item .env.example .env`。至少修改以下配置：

```dotenv
LARK_LEDGER_EVENT_MODE=websocket
LARK_LEDGER_DATABASE_URL=postgresql+asyncpg://用户名:密码@数据库主机:5432/数据库名
LARK_LEDGER_LARK_APP_ID=cli_xxxxxxxxxxxxx
LARK_LEDGER_LARK_APP_SECRET=replace-me
LARK_LEDGER_AI_API_KEY=replace-me
LARK_LEDGER_VISION_API_KEY=replace-with-dashscope-key
LARK_LEDGER_TRANSCRIPTION_API_KEY=replace-with-dashscope-key
```

长连接模式不需要 `LARK_LEDGER_LARK_VERIFICATION_TOKEN` 或 `LARK_LEDGER_LARK_ENCRYPT_KEY`。文字、图片和语音使用独立的 API 配置：文字模型负责解析消息和生成建议，视觉模型负责图片记账，ASR 模型先把语音转为文字。图片或语音 Key 未配置时，仅禁用对应功能，不影响文字记账。

### 3. 配置飞书应用

1. 在[飞书开放平台](https://open.feishu.cn/app)创建企业自建应用并开启机器人能力。
2. 添加接收消息、发送消息，以及获取和上传图片或文件资源所需的最小权限。
3. 在「事件与回调」选择「使用长连接接收事件」，然后点击验证。
4. 订阅 `im.message.receive_v1`（接收消息 v2.0）。
5. 发布应用版本，并将机器人加入需要使用的会话。

长连接模式不要填写请求地址。只有在应用已经运行并建立连接后，开放平台的连接验证才会成功。

### 4. 启动并检查

```bash
docker compose up -d --build
curl http://localhost:8000/healthz
```

容器启动时会先执行 `alembic upgrade head`。长连接正常时，健康检查类似：

```json
{"status":"ok","event_mode":"websocket","long_connection":"connected"}
```

`connecting` 或 `reconnecting` 表示连接尚未就绪或正在重连。查看日志可使用 `docker compose logs -f app`。

## 事件接入方式

| 模式 | 适用场景 | 公网 HTTPS | 飞书回调凭据 | 配置值 |
| --- | --- | --- | --- | --- |
| 长连接（推荐） | 本地、NAS、家庭服务器 | 不需要 | 不需要 Verification Token / Encrypt Key | `websocket` |
| Webhook | 已有公网入口、反向代理或平台化部署 | 需要 | Verification Token；推荐 Encrypt Key | `webhook` |

切换到 Webhook 时，将 `LARK_LEDGER_EVENT_MODE` 设为 `webhook`，配置 Verification Token，并在飞书开放平台把事件发送到：

```text
https://你的域名/webhooks/feishu
```

推荐同时配置 Encrypt Key。服务会校验 `X-Lark-Signature`、解密加密事件并处理 URL verification。Webhook 模式下长连接不会启动；长连接模式下 Webhook 端点返回 404。

两种入口共用同一套 `event_id` 幂等、消息解析、账本操作和回复流程。详细配置、生产部署与安全检查见[环境与部署指南](docs/environment.md)。

## 安全边界

```text
飞书消息 → 来源校验 / 事件去重 → 媒体下载 → AI 结构化解析
                                              ↓
                              Pydantic 严格校验（禁止额外字段）
                                              ↓
                              固定业务动作 → SQLAlchemy → PostgreSQL
```

AI 只能返回预定义的记账、修改、撤销、汇总、报告、预算管理或帮助动作。消费建议只接收分类、趋势和收支总额等聚合数据，不接收逐笔备注或用户标识。更多设计细节见[架构说明](docs/architecture.md)。

## 本地开发

需要 Python 3.11+ 和可访问的 PostgreSQL：

```bash
python -m venv .venv
# Linux / macOS
source .venv/bin/activate
# Windows PowerShell
.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
alembic upgrade head
uvicorn lark_ledger.main:app --reload
```

提交前运行：

```bash
ruff check .
mypy src
pytest --cov
```

开发流程和设计原则见[贡献指南](CONTRIBUTING.md)。

## 文档

- [用户手册](docs/help.md)：消息示例、预算和报告、使用限制、常见问题
- [环境与部署指南](docs/environment.md)：完整配置、两种事件模式、生产安全检查
- [架构说明](docs/architecture.md)：组件、数据流、信任边界和当前运行限制
- [贡献指南](CONTRIBUTING.md)：开发环境、质量检查和提交要求
- [安全策略](SECURITY.md)：漏洞报告和部署安全建议

## 当前限制

- 普通文字可在一条消息中新增最多 30 笔账并同时设置最多 10 项预算；单笔支付详情和
  小票不会按商品拆成多笔记录，支付流水截图可新增最多 30 笔。
- 修改上一笔、撤销、查询和报告不能混入批量记账消息，需要单独发送。
- 只能修改或撤销当前用户最近一笔未撤销记录，不能搜索任意历史记录。
- 后台处理任务运行在 Web 进程内，不是持久队列；高可用或高吞吐部署需另行接入任务队列。
- 暂不支持用户自定义分类、个人时区/币种、数据导出、共享账本和恢复已撤销记录。
- 外币仅在新增、修改上一笔和设置预算时按最新参考汇率换算；汇总与报告不支持切换展示币种。

## License

LarkLedger 使用 [Apache License 2.0](LICENSE) 开源。
