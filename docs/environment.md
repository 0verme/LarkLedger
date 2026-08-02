# 环境配置与部署安全

LarkLedger 从环境变量读取运行配置。仓库只保留 `.env.example`，不要提交填入真实凭据的 `.env` 文件。

## 必要的外部配置

启动应用前，请完成以下准备：

1. 在飞书开放平台创建企业自建应用，开启机器人能力，并配置消息接收、发送和资源读取权限。
2. 订阅 `im.message.receive_v1`，使用 Webhook 模式将事件发送到 `/webhooks/feishu`。
3. 创建仅供 LarkLedger 使用的 PostgreSQL 用户和数据库，并使用唯一的高强度密码。
4. 准备兼容 OpenAI Chat Completions、JSON Schema structured output 和音频转写接口的模型服务凭据。

如果任何 App Secret、API Key 或数据库密码曾经出现在聊天记录、工单、源码或日志中，请先撤销并重新生成，再继续部署。

## 本地开发

复制示例配置并在本地填写：

```bash
cp .env.example .env
```

所有应用变量均以 `LARK_LEDGER_` 开头。至少需要配置：

- `LARK_LEDGER_DATABASE_URL`
- `LARK_LEDGER_LARK_APP_ID`
- `LARK_LEDGER_LARK_APP_SECRET`
- `LARK_LEDGER_LARK_VERIFICATION_TOKEN`
- `LARK_LEDGER_AI_API_KEY`

推荐同时配置 `LARK_LEDGER_LARK_ENCRYPT_KEY`，以便校验飞书签名并解密加密事件。完整字段和默认值见 [`.env.example`](../.env.example)。

## 生产环境

在服务器上直接创建生产 `.env`，并限制为运行 LarkLedger 的操作系统账号可读。在 Linux 上可执行：

```bash
chmod 600 .env
```

- 将应用放在 HTTPS 反向代理之后，不要直接将应用端口暴露到公网。
- 应用与 PostgreSQL 应通过同一私有网络或 VPN 通信，不要向公网开放 PostgreSQL。
- 数据库连接跨越不受信任网络时，使用数据库支持的 TLS 参数。
- 日志中不得记录环境变量快照、数据库连接串、Authorization 请求头、包含财务信息的完整消息正文或未经脱敏的模型请求与响应。
- `.env`、私钥和证书文件均已加入 `.gitignore`；提交前仍需检查暂存内容。

## 部署前检查

- 曾经暴露的飞书 App Secret、模型 API Key 和数据库密码均已撤销或轮换。
- 生产环境中不再包含示例占位符或默认密码。
- 飞书事件 URL 使用 HTTPS，验证 Token 与 Encrypt Key 和开放平台配置一致。
- 应用服务器可通过私网地址访问 PostgreSQL，且 PostgreSQL 无法从公网访问。
- 健康检查 `GET /healthz` 返回成功。
- 同一个飞书 `event_id` 重复投递时只生成一条账目记录。
- 模型响应经过结构化校验后才会触发账本操作。
- `git status` 不显示 `.env`、私钥、证书或其他真实凭据文件。
