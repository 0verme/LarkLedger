# 环境配置与部署指南

LarkLedger 从环境变量读取运行配置，所有变量都以 `LARK_LEDGER_` 开头。仓库只保留 [`.env.example`](../.env.example)；不要提交包含真实凭据的 `.env`。

## 外部依赖

运行前需要准备：

1. 一个开启机器人能力并订阅 `im.message.receive_v1` 的飞书 / Lark 企业自建应用。
2. 一个应用可访问的 PostgreSQL 数据库、独立数据库用户和高强度密码。
3. 一个用于文字解析和消费建议的 AI 服务。图片和语音功能分别使用独立服务配置；推荐文字使用 DeepSeek、图片与语音使用阿里云百炼。

当前 `compose.yaml` 只启动应用容器，不创建 PostgreSQL。`LARK_LEDGER_DATABASE_URL` 中的数据库主机必须能从容器内部访问；不要把容器内的 `localhost` 当作宿主机。

## 完整配置项

| 环境变量 | 默认值 | 必需条件 | 说明 |
| --- | --- | --- | --- |
| `LARK_LEDGER_EVENT_MODE` | `webhook` | 始终 | `websocket` 或 `webhook`，不区分大小写 |
| `LARK_LEDGER_DATABASE_URL` | 示例 Compose 地址 | 始终 | SQLAlchemy async PostgreSQL URL，驱动应为 `asyncpg` |
| `LARK_LEDGER_TIMEZONE` | `Asia/Shanghai` | 始终 | IANA 时区，用于解析“昨天”“这个月”和预算自然月 |
| `LARK_LEDGER_CURRENCY` | `CNY` | 始终 | 三字母 ISO 4217 币种代码，启动时转为大写 |
| `LARK_LEDGER_EXCHANGE_RATE_API_URL` | `https://api.frankfurter.dev` | 使用外币金额 | Frankfurter v2 或兼容服务的 API 根地址，无需填写密钥 |
| `LARK_LEDGER_EXCHANGE_RATE_CACHE_TTL_SECONDS` | `3600` | 使用外币金额 | 最新汇率的进程内缓存秒数，允许范围 60～86400 |
| `LARK_LEDGER_LARK_APP_ID` | 空 | 始终 | 飞书 / Lark 应用 App ID |
| `LARK_LEDGER_LARK_APP_SECRET` | 空 | 始终 | 应用 App Secret |
| `LARK_LEDGER_LARK_BASE_URL` | `https://open.feishu.cn` | 始终 | 开放平台 API 根地址；Lark 国际版按平台文档调整 |
| `LARK_LEDGER_LARK_VERIFICATION_TOKEN` | 空 | Webhook 应配置 | 校验回调来源；长连接不使用 |
| `LARK_LEDGER_LARK_ENCRYPT_KEY` | 空 | Webhook 推荐 | Webhook 签名校验和加密事件解密；长连接不使用 |
| `LARK_LEDGER_AI_API_KEY` | 空 | 始终 | 文字解析和消费建议服务的 API Key |
| `LARK_LEDGER_AI_BASE_URL` | `https://api.openai.com/v1` | 始终 | 文字服务的 OpenAI 兼容 API 根地址 |
| `LARK_LEDGER_AI_MODEL` | `gpt-4.1-mini` | 始终 | 文字消息解析和消费建议模型 |
| `LARK_LEDGER_VISION_API_KEY` | 空 | 图片记账 | 图片理解服务 API Key；未配置时明确提示图片功能不可用 |
| `LARK_LEDGER_VISION_BASE_URL` | 百炼北京兼容地址 | 图片记账 | 图片服务的 OpenAI 兼容 API 根地址 |
| `LARK_LEDGER_VISION_MODEL` | `qwen3.7-plus` | 图片记账 | 支持图片输入和 JSON Object 输出的视觉模型 |
| `LARK_LEDGER_TRANSCRIPTION_API_KEY` | 空 | 语音记账 | 千问 ASR 服务 API Key；可以与图片服务使用同一个百炼 Key |
| `LARK_LEDGER_TRANSCRIPTION_BASE_URL` | 百炼北京兼容地址 | 语音记账 | 千问 ASR 的 OpenAI 兼容 API 根地址 |
| `LARK_LEDGER_TRANSCRIPTION_MODEL` | `qwen3-asr-flash` | 语音记账 | 通过 Chat Completions `input_audio` 调用的转写模型 |
| `LARK_LEDGER_TRANSCRIPTION_LANGUAGE` | `zh` | 语音记账 | ASR 语言代码；留空时自动识别 |
| `LARK_LEDGER_TRANSCRIPTION_ENABLE_ITN` | `true` | 语音记账 | 将口语数字、日期等归一化为书面形式 |
| `LARK_LEDGER_AI_TIMEOUT_SECONDS` | `45` | 始终 | 单次 AI HTTP 请求超时，必须大于 0 且不超过 180 秒 |
| `LARK_LEDGER_REPORT_FONT_PATH` | 空 | 可选 | 中文报告字体文件；Docker 镜像已包含 Noto CJK |

示例值只是占位符。生产部署前必须替换 `replace-me`、`change-me` 和所有示例账号密码。

### 多模型配置

推荐让不同能力使用独立模型，避免把图片或语音发送给不支持对应输入格式的文字模型：

```dotenv
LARK_LEDGER_AI_API_KEY=replace-with-deepseek-key
LARK_LEDGER_AI_BASE_URL=https://api.deepseek.com
LARK_LEDGER_AI_MODEL=deepseek-v4-flash

LARK_LEDGER_VISION_API_KEY=replace-with-dashscope-key
LARK_LEDGER_VISION_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
LARK_LEDGER_VISION_MODEL=qwen3.7-plus

LARK_LEDGER_TRANSCRIPTION_API_KEY=replace-with-dashscope-key
LARK_LEDGER_TRANSCRIPTION_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
LARK_LEDGER_TRANSCRIPTION_MODEL=qwen3-asr-flash
LARK_LEDGER_TRANSCRIPTION_LANGUAGE=zh
LARK_LEDGER_TRANSCRIPTION_ENABLE_ITN=true
```

视觉模型接收 JPEG、PNG 或 WebP，并使用图片真实格式构造 Base64 Data URL。语音模型接收飞书下载的 OPUS/OGG 等音频，通过千问 ASR 的 Chat Completions 接口转写。图片或语音 Key 为空时，对应功能会返回未配置提示，不会回退到文字模型。

## 长连接模式（推荐）

长连接仅需出站访问飞书和 AI 服务，不需要公网回调地址：

```dotenv
LARK_LEDGER_EVENT_MODE=websocket
LARK_LEDGER_LARK_APP_ID=cli_xxxxxxxxxxxxx
LARK_LEDGER_LARK_APP_SECRET=replace-me
```

在飞书开放平台的「事件与回调」中选择「使用长连接接收事件」，验证连接后订阅 `im.message.receive_v1` 并发布版本。不要填写请求地址，也不需要 Verification Token 或 Encrypt Key。

应用启动时会建立连接，断线后自动重连；退出时会关闭连接。`GET /healthz` 的 `long_connection` 可能为：

- `connected`：连接可用
- `connecting`：正在建立连接
- `reconnecting`：断线重连中
- `error`：连接线程异常停止
- `stopped` 或 `stopping`：应用正在停止

使用 Uvicorn `--reload` 时，由实际 worker 的生命周期管理连接。不要同时运行多个长期连接实例，除非已经确认事件分发和处理能力符合部署预期。

## Webhook 模式

Webhook 适合已有公网 HTTPS、反向代理或平台化入口的部署：

```dotenv
LARK_LEDGER_EVENT_MODE=webhook
LARK_LEDGER_LARK_VERIFICATION_TOKEN=replace-me-for-webhook
LARK_LEDGER_LARK_ENCRYPT_KEY=replace-with-encrypt-key
```

在开放平台选择将事件发送至开发者服务器，并配置：

```text
https://你的域名/webhooks/feishu
```

服务支持 URL verification、Verification Token 校验、`X-Lark-Signature` 验签和加密事件解密。配置 Encrypt Key 后，飞书后台与 `.env` 中的值必须一致。

Webhook 在验证来源和请求格式后立即返回，把实际消息处理加入 FastAPI 后台任务。该任务与 Web 进程同生共死，不具备持久队列的重试和故障恢复能力。

## Docker Compose 部署

复制并填写配置：

```bash
cp .env.example .env
docker compose up -d --build
docker compose logs -f app
```

应用容器启动时先执行 `alembic upgrade head`，再启动 Uvicorn。数据库迁移失败时应用不会启动；请先检查数据库地址、网络、权限和 TLS 参数。

健康检查：

```bash
curl http://localhost:8000/healthz
```

Webhook 模式返回 `long_connection: disabled`；长连接模式返回当前连接状态。健康检查不会回显任何凭据。

## 本地开发

```bash
python -m venv .venv
# Linux / macOS
source .venv/bin/activate
# Windows PowerShell
.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
cp .env.example .env
alembic upgrade head
uvicorn lark_ledger.main:app --reload
```

本地运行同样需要可访问的 PostgreSQL。若使用长连接，先启动应用，再到开放平台验证连接。

## 生产安全

- 将 `.env` 限制为运行 LarkLedger 的操作系统账号可读；Linux 可执行 `chmod 600 .env`。
- Webhook 必须位于 HTTPS 反向代理之后；长连接模式的 `8000` 端口可只在私网开放用于健康检查。
- PostgreSQL 仅允许应用所在私网或 VPN 访问，不要暴露到公网；跨越不受信任网络时启用数据库 TLS。
- AI Key、App Secret、Verification Token 和 Encrypt Key 只通过环境变量或密钥管理服务注入。
- 日志不得记录环境变量快照、数据库 URL、Authorization 头、完整消息正文、媒体内容或未脱敏的 AI 请求与响应。
- 定期备份 PostgreSQL，并实际演练恢复流程。备份的访问控制和保留策略应与生产账本一致。
- 轮换任何曾经出现在聊天、工单、源码或日志中的凭据，不能只从文件中删除。

## 部署验收清单

- [ ] PostgreSQL 使用独立账号和非示例密码，应用能执行 Alembic 迁移。
- [ ] `.env` 不在 Git 跟踪或暂存列表中，权限仅允许服务账号读取。
- [ ] 飞书应用只授予消息收发和媒体资源所需的最小权限。
- [ ] 长连接显示 `connected`，或 Webhook URL verification 与验签通过。
- [ ] `GET /healthz` 返回 `status: ok`，且事件模式与预期一致。
- [ ] 文本消息可以记账，重复投递同一 `event_id` 不会生成第二条记录。
- [ ] `午饭1300日元` 能换算成默认币种保存；汇率服务不可用且没有缓存时不会写入账目。
- [ ] 图片、语音和报告功能使用的 AI 模型及飞书资源权限均已验证。
- [ ] 使用包含多笔独立交易的支付流水截图验证批量图片记账、部分失败汇总和 20 笔上限提示。
- [ ] 图片和语音专项 API Key 已分别配置；如复用同一个百炼 Key，两项均已显式填写。
- [ ] PostgreSQL 无法从公网访问，Webhook（如启用）只通过 HTTPS 暴露。
- [ ] 已建立数据库备份、恢复和凭据轮换流程。
