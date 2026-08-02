# LarkLedger（飞账）

## 飞书事件接入模式

LarkLedger 支持两种互斥的事件入口，两者共用同一个 `event_id` 幂等服务、
`MessageProcessor` 和记账/AI/回复流程：

- `LARK_LEDGER_EVENT_MODE=websocket`：使用飞书官方 Python SDK `lark-oapi` 建立长连接，
  适合本地开发、家庭服务器和飞牛 Docker，不需要公网域名、FRP、Verification Token
  或 Encrypt Key。
- `LARK_LEDGER_EVENT_MODE=webhook`：保留 `POST /webhooks/feishu` 开发者服务器回调，
  适合有公网 HTTPS 地址的部署。默认值为 `webhook`，继续支持验签、解密和 URL verification。

本地长连接启动：

```bash
pip install -e ".[dev]"
alembic upgrade head
# 在本地 .env 中设置 LARK_LEDGER_EVENT_MODE=websocket，并配置 App ID / App Secret
uvicorn lark_ledger.main:app --reload
curl http://127.0.0.1:8000/healthz
```

健康检查只返回当前事件模式与长连接状态，不会返回任何凭据。使用 Uvicorn `--reload`
时只有实际的 worker 生命周期会启动连接；进程退出或 Docker 收到停止信号时会关闭连接。

飞书后台配置长连接：进入「事件与回调」，选择「使用长连接接收事件」，点击「验证」；
看到长连接已经建立后保存配置，再添加事件 `im.message.receive_v1`（接收消息 v2.0），
确认机器人所需权限并发布应用版本。长连接模式不要填写请求地址。

Docker / 飞牛部署继续使用同一镜像和 Compose：将宿主机 `.env` 的事件模式设为
`websocket` 后运行 `docker compose up -d --build`。容器只需出站访问飞书、DeepSeek，
不需要暴露公网回调地址（端口 `8000` 可仅用于局域网健康检查）。切回公网 Webhook 时，
把模式改为 `webhook`，在飞书后台选择「将事件发送至开发者服务器」，配置
`https://你的域名/webhooks/feishu`、Verification Token，并按需配置 Encrypt Key。

> 自托管的飞书 / Lark AI 记账机器人：用文字、语音、小票照片或支付截图完成记账、查询、修改、撤销和消费汇总。

[![CI](https://github.com/0verme/lark-ledger/actions/workflows/ci.yml/badge.svg)](https://github.com/0verme/lark-ledger/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)

LarkLedger 将账本保存在你自己的 PostgreSQL 中。大模型只负责把自然语言、语音转写和图片内容转换成受严格校验的业务动作；它拿不到数据库连接，不能替代数据库，也不能生成或执行 SQL。

## 能做什么

面向使用者的完整操作方法、示例、限制和常见问题见 [`飞账机器人 Help 使用手册`](docs/help.md)。

- 文字记账：`糖水9块`、`昨天打车38.5`、`工资到账10000`
- 语音记账：下载飞书语音后转写，再按同一套安全流程处理
- 图片记账：识别小票照片和支付截图
- 修改与撤销：`上一笔改成8块`、`撤销刚才那笔`
- 分类汇总：`这个月餐饮花了多少`
- 多用户隔离：所有查询和修改均以飞书用户 `open_id` 为边界
- 事件幂等：按飞书 `event_id` 去重，新增记录也保留来源消息 ID
- 自托管：FastAPI + PostgreSQL + Docker Compose

## 安全边界

```text
飞书消息 → 验签/验 Token → 媒体下载 → AI 结构化解析
                                         ↓
                         Pydantic 严格校验（禁止额外字段）
                                         ↓
                         固定业务动作 → SQLAlchemy → PostgreSQL
```

AI 输出只能是 `create`、`update_last`、`undo_last`、`summary`、`help` 之一。结构中没有 SQL、表名或任意查询条件字段；数据库层只执行项目代码预先定义的参数化查询。

## 快速开始

### 1. 准备配置

需要 Docker 及 Docker Compose。复制示例配置：

```bash
cp .env.example .env
```

至少填写：

- `LARK_LEDGER_LARK_APP_ID`
- `LARK_LEDGER_LARK_APP_SECRET`
- `LARK_LEDGER_LARK_VERIFICATION_TOKEN`
- `LARK_LEDGER_AI_API_KEY`
- PostgreSQL 密码（同时修改 `.env` 的连接串与 `compose.yaml`）

默认时区为 `Asia/Shanghai`，默认币种为 `CNY`。AI 接口采用 OpenAI 兼容的 Chat Completions、JSON Schema structured output、音频转写接口；可通过 `AI_BASE_URL` 和模型名接入兼容服务。

### 2. 启动

```bash
docker compose up -d --build
curl http://localhost:8000/healthz
```

应用启动时自动执行 Alembic 迁移。生产环境请将服务放在 HTTPS 反向代理之后，不要直接暴露 PostgreSQL。

### 3. 配置飞书应用

1. 在[飞书开放平台](https://open.feishu.cn/app)创建企业自建应用并开启机器人能力。
2. 在「权限管理」添加接收消息、发送消息及获取消息资源所需权限。
3. 在「事件与回调」选择将事件发送至开发者服务器。
4. 请求地址填写 `https://你的域名/webhooks/feishu`。
5. 订阅 `im.message.receive_v1`（接收消息 v2.0），然后发布应用版本。
6. 推荐配置 Encrypt Key，并将其写入 `LARK_LEDGER_LARK_ENCRYPT_KEY`。配置后服务会校验 `X-Lark-Signature` 并解密事件。

飞书事件回调需要快速响应。LarkLedger 在完成来源校验和 `event_id` 入库后立即确认回调，再在后台进行 AI 与消息回复；生产规模较大时建议把后台任务替换为持久队列。

## 本地开发

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
source .venv/bin/activate
pip install -e ".[dev]"
ruff check .
mypy src
pytest --cov
```

运行本地服务：

```bash
alembic upgrade head
uvicorn lark_ledger.main:app --reload
```

## 配置项

所有环境变量均以 `LARK_LEDGER_` 开头。完整示例见 [`.env.example`](.env.example)。
生产部署与凭据安全检查见 [`docs/environment.md`](docs/environment.md)。

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DATABASE_URL` | PostgreSQL Compose 地址 | SQLAlchemy async 数据库 URL |
| `TIMEZONE` | `Asia/Shanghai` | 解析“昨天”“这个月”等相对时间 |
| `CURRENCY` | `CNY` | 新增账目的默认币种 |
| `LARK_BASE_URL` | `https://open.feishu.cn` | 国内飞书 API；Lark 国际版可改域名 |
| `LARK_ENCRYPT_KEY` | 空 | 飞书事件加密与签名密钥，生产推荐配置 |
| `AI_BASE_URL` | `https://api.openai.com/v1` | OpenAI 兼容 API 根地址 |
| `AI_MODEL` | `gpt-4.1-mini` | 支持视觉与 JSON Schema 的解析模型 |
| `TRANSCRIPTION_MODEL` | `gpt-4o-mini-transcribe` | 语音转写模型 |

## 项目结构

```text
src/lark_ledger/
├── api.py                 # 健康检查与飞书 Webhook
├── config.py              # 环境配置和校验
├── db.py                  # 异步数据库会话
├── models.py              # PostgreSQL 账本与幂等事件模型
├── schemas.py             # AI 可输出的受限业务动作
└── services/
    ├── ai.py              # 文字、视觉、语音的 AI 适配
    ├── feishu.py          # Token、资源、回复、验签和事件处理
    └── ledger.py          # 固定的记账业务逻辑
```

## 当前限制与路线图

这是 `0.1.0` 的可运行 MVP：

- 一张图片按一笔账处理；多商品拆分将在后续版本加入。
- 后台任务目前运行在 Web 进程内；高可用部署应接入 Redis / RabbitMQ 等持久队列。
- 用户级时区、币种、自定义分类和数据导出尚未提供。
- 当前为 Webhook 模式，未来可增加飞书官方 SDK 长连接模式。

欢迎通过 Issue 和 Pull Request 参与。提交前请阅读 [CONTRIBUTING.md](CONTRIBUTING.md) 与 [SECURITY.md](SECURITY.md)。

## License

LarkLedger 使用 [Apache License 2.0](LICENSE) 开源。
