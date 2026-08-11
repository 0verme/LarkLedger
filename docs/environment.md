# 环境配置与部署指南

> Documentation is Chinese-first. For an English project overview, see the [English README](../README.en.md).

LarkLedger 从环境变量读取运行配置，所有运行时变量都以 `LARK_LEDGER_` 开头（见 `src/lark_ledger/config.py` 中的 `Settings`）。仓库只保留 [`.env.example`](../.env.example)；不要提交包含真实凭据的 `.env`（已在 `.gitignore` 中忽略）。

## 推荐快速路径（文字-only）

目标：技术用户在**无公网回调**的情况下，用 WebSocket + PostgreSQL + Docker Compose 完成**第一笔纯文字记账**。

```text
已有 Docker
→ 创建飞书自建应用（机器人 + 长连接 + im.message.receive_v1）
→ 准备 PostgreSQL（推荐 compose.dev 体验库）
→ 填写最小 .env
→ docker compose 启动
→ 检查 healthz 与日志
→ 飞书发送「午饭32元」→ 收到含 #XXXXX 的回复
→ 「最近10笔」核对
```

主路径**只承诺**文字记账、五位短 ID、最近账目/详情、按短 ID 改删恢复、CSV 导出（导出另需文件权限）。图片、语音、Webhook、公网域名放在本文后续扩展章节。

### 最小必填环境变量

| 环境变量 | 说明 |
| --- | --- |
| `LARK_LEDGER_EVENT_MODE` | 快速路径请设为 `websocket` |
| `LARK_LEDGER_DATABASE_URL` | `postgresql+asyncpg://...` 异步连接串 |
| `LARK_LEDGER_LARK_APP_ID` | 飞书应用 App ID |
| `LARK_LEDGER_LARK_APP_SECRET` | 飞书应用 App Secret |
| `LARK_LEDGER_AI_API_KEY` | 文字 AI API Key |
| `LARK_LEDGER_AI_BASE_URL` | 文字 AI 的 OpenAI 兼容根地址（示例常用 DeepSeek） |
| `LARK_LEDGER_AI_MODEL` | 文字模型名 |

**不要**为了纯文字记账去填写图片模型、语音识别或 Webhook 验签相关占位值。Pydantic `Settings` 对图片/语音 Key 允许为空；未配置时仅禁用对应功能。

**有明确安全默认、通常不必改：**

| 环境变量 | 代码默认 |
| --- | --- |
| `LARK_LEDGER_TIMEZONE` | `Asia/Shanghai` |
| `LARK_LEDGER_CURRENCY` | `CNY` |
| `LARK_LEDGER_AI_TIMEOUT_SECONDS` | `45` |
| `LARK_LEDGER_EXCHANGE_RATE_API_URL` | `https://api.frankfurter.dev` |
| `LARK_LEDGER_EXCHANGE_RATE_CACHE_TTL_SECONDS` | `3600` |

**代码中不存在** `LOG_LEVEL` / 日志级别环境变量；当前依赖标准库与应用日志默认行为。

### 一键本地体验（推荐）

`compose.dev.yaml` 提供 PostgreSQL 16、健康检查、命名卷，并覆盖应用的数据库地址：

```bash
cp .env.example .env
# 编辑 .env：填写飞书 App ID/Secret 与文字 AI Key；保持 EVENT_MODE=websocket
docker compose -f compose.yaml -f compose.dev.yaml up -d --build
docker compose -f compose.yaml -f compose.dev.yaml ps
docker compose -f compose.yaml -f compose.dev.yaml logs -f app
curl http://127.0.0.1:8000/healthz
```

Windows PowerShell 复制：`Copy-Item .env.example .env`。

| 项 | 事实 |
| --- | --- |
| 应用服务名 | `app` |
| 数据库服务名 | `db`（仅 dev 叠加） |
| 健康检查 | `GET http://127.0.0.1:8000/healthz` |
| 开发库账号 | 用户/库 `lark_ledger`，密码 `dev-only-password`（**仅本地**） |
| 数据卷 | `lark_ledger_dev_pgdata` |
| 库端口映射 | `127.0.0.1:${LARK_LEDGER_DEV_POSTGRES_PORT:-5432}:5432` |
| 启动迁移 | `compose.yaml` 中 `alembic upgrade head` 后启动 Uvicorn |

常用运维：

```bash
# 停止但保留数据
docker compose -f compose.yaml -f compose.dev.yaml down

# 停止并删除开发库数据卷（不可恢复）
docker compose -f compose.yaml -f compose.dev.yaml down -v
```

### 第一笔账验收

在飞书中发送（`#XXXXX` 换成机器人实际返回的短 ID）：

1. `午饭32元` → 成功回复含 `#XXXXX`
2. `最近10笔` → 能看到该笔
3. `查看 #XXXXX` → 详情
4. `把 #XXXXX 改成35元` → 修改成功
5. `删除 #XXXXX` / `恢复 #XXXXX` → 软删除与恢复
6. `导出最近90天账单` → CSV 文件消息（**需额外确认飞书文件上传与文件消息权限**；本仓库不宣称已在真实飞书租户完成该项验收）

---

## Web Dashboard（可选）

Dashboard 默认关闭。关闭时不会挂载静态页面或 `/api/web/v1/*`，也不会改变飞书事件、Event Worker、Reply Worker、Cleanup Worker 或 migration 的启动条件。

### 飞书 OAuth 与会话配置

```dotenv
LARK_LEDGER_DASHBOARD_ENABLED=true
LARK_LEDGER_DASHBOARD_BASE_URL=https://ledger.example.com
LARK_LEDGER_DASHBOARD_SESSION_SECRET=replace-with-at-least-32-high-entropy-characters
LARK_LEDGER_DASHBOARD_ADMIN_OPEN_IDS=ou_admin_1,ou_admin_2
LARK_LEDGER_DASHBOARD_SESSION_TTL_SECONDS=28800
LARK_LEDGER_DASHBOARD_OAUTH_STATE_TTL_SECONDS=600
LARK_LEDGER_DASHBOARD_COOKIE_SECURE=true
```

在飞书开放平台为同一个企业自建应用配置：

1. 添加重定向 URL：`https://ledger.example.com/api/web/v1/auth/callback`，必须与 `DASHBOARD_BASE_URL` 的 origin 完全一致。
2. 开通 `auth:user.id:read`，使登录流程取得当前用户的 `open_id`。
3. 发布应用版本，再访问 `https://ledger.example.com/` 登录。
4. 将需要运维权限的 `open_id` 以逗号分隔写入 `DASHBOARD_ADMIN_OPEN_IDS`；未列入者都是普通用户。

`SESSION_SECRET` 需由密码学安全随机源生成，至少 32 个非平凡字符。Dashboard 开启但密钥弱、App ID/Secret 缺失、Base URL 非绝对 origin，或 Secure Cookie 搭配 HTTP 时，应用会拒绝启动。飞书 access token 不返回浏览器；浏览器仅保存 HttpOnly 会话 Cookie、HttpOnly OAuth state Cookie 与供双提交校验的 CSRF Cookie。会话可撤销、有明确 TTL，退出后立即失效。

### HTTPS 与可信代理

生产必须在 HTTPS 反向代理后运行。代理应保留 `Host` 并发送 `X-Forwarded-Proto: https`。Uvicorn 只能信任实际代理 IP/CIDR，不要在互联网暴露的实例上使用无边界的 `--forwarded-allow-ips='*'`。代理不在 Uvicorn 默认信任的 `127.0.0.1` 时，可覆盖 Compose command，例如：

```yaml
services:
  app:
    command: >-
      sh -c "alembic upgrade head &&
      uvicorn lark_ledger.main:app --host 0.0.0.0 --port 8000
      --proxy-headers --forwarded-allow-ips=172.20.0.10"
```

把示例地址替换为固定代理地址或经过审查的最小 CIDR。`DASHBOARD_BASE_URL` 是 OAuth 回调的权威外部 origin，不能包含路径、query、凭据或 fragment。应用为 Dashboard 响应添加 CSP、禁止 iframe、MIME sniff 防护、Referrer/Permissions Policy；Secure Cookie 模式还返回 HSTS。不要让代理覆盖或放宽这些响应头。

管理员配置页只返回时区、币种、Worker 开关、模型名与“是否已配置”状态；不会返回 API Key、App Secret、数据库 URL、密码、Webhook Token 或 Authorization 值。本版本不支持网页修改 Secret。

## 外部依赖

1. 开启机器人能力并订阅 `im.message.receive_v1` 的飞书 / Lark 企业自建应用。
2. 应用可访问的 PostgreSQL（开发叠加提供 16；生产请使用受支持的 PostgreSQL 16 或与项目测试一致的版本）。
3. 文字解析 AI 服务。图片与语音为可选独立配置。

当前 `compose.yaml` **只**启动应用容器，不创建 PostgreSQL。`LARK_LEDGER_DATABASE_URL` 中的主机必须从**容器内部**可达；容器内的 `localhost` 不是宿主机。

代码内置默认值面向通用 OpenAI 兼容服务；[`.env.example`](../.env.example) 展示推荐的文字模型示例与可选图片/语音分组。示例会覆盖部分代码默认（例如文字 `AI_BASE_URL` / `AI_MODEL`），**不是**两套互相打架的权威源：以你部署时 `.env` 的最终值为准。

## 配置项对照（Settings 事实）

以下变量名与默认值来自当前 `Settings`（`env_prefix=LARK_LEDGER_`），**不是**印象值。

| 配置项 | 代码默认 | `.env.example` 推荐 | 快速路径 |
| --- | --- | --- | --- |
| 事件模式 `EVENT_MODE` | `webhook` | `websocket` | **必填设为 websocket** |
| PostgreSQL `DATABASE_URL` | `postgresql+asyncpg://lark_ledger:change-me@db:5432/lark_ledger` | 同左占位 | 必填（dev 可被 Compose 覆盖） |
| 飞书 App ID | 空字符串 | 占位 `cli_…` | 必填 |
| 飞书 App Secret | 空字符串 | `replace-me` | 必填 |
| 飞书 Base URL | `https://open.feishu.cn` | 同左 | 可选 |
| Verification Token | 空 | 空（Webhook 时再填） | 文字 WebSocket **不需要** |
| Encrypt Key | 空 | 空 | 文字 WebSocket **不需要** |
| 文字 AI API Key | 空 | `replace-me` | 必填 |
| 文字 AI Base URL | `https://api.openai.com/v1` | `https://api.deepseek.com` | 必填（或接受代码默认并确保 Key 匹配） |
| 文字 AI Model | `gpt-4.1-mini` | `deepseek-v4-flash` | 必填（或接受代码默认） |
| 时区 | `Asia/Shanghai` | 同左 | 有默认 |
| 默认币种 | `CNY` | 同左 | 有默认 |
| 图片 Vision Key / URL / Model | 空 / 百炼兼容地址 / `qwen3.7-plus` | Key 空 | 文字-only **不需要** |
| 语音 Transcription 配置 | Key 空；模型 `qwen3-asr-flash` 等 | Key 空 | 文字-only **不需要** |
| AI 超时秒 | `45`（0–180） | `45` | 有默认 |
| 汇率 API / 缓存 TTL | frankfurter / `3600` | 同左 | 外币时相关 |
| 报告字体路径 | `None` | 空 | 可选 |
| 事件 Worker 开关 `WORKER_ENABLED` | `true` | `true` | 有默认（生产默认开启；关闭则回到进程内同步路径） |
| Worker 轮询间隔秒 `WORKER_POLL_INTERVAL_SECONDS` | `1.0` | `1.0` | 有默认 |
| Worker 批量大小 `WORKER_BATCH_SIZE` | `10` | `10` | 有默认 |
| 事件最大尝试 `EVENT_MAX_ATTEMPTS` | `3` | `3` | 有默认（首次处理计 1 次） |
| 事件租约秒 `EVENT_LEASE_SECONDS` | `300` | `300` | 有默认（崩溃事件在租约过期后被接管） |
| 重试退避基数秒 `EVENT_RETRY_BASE_SECONDS` | `2.0` | `2.0` | 有默认 |
| 重试退避上限秒 `EVENT_RETRY_MAX_SECONDS` | `3600` | `3600` | 有默认 |
| 回复 Worker 开关 `REPLY_WORKER_ENABLED` | `true` | `true` | 有默认（生产默认开启；关闭则回到兼容同步发送路径） |
| 回复 Worker 轮询间隔秒 `REPLY_WORKER_POLL_INTERVAL_SECONDS` | `1.0` | `1.0` | 有默认 |
| 回复 Worker 批量大小 `REPLY_WORKER_BATCH_SIZE` | `10` | `10` | 有默认 |
| 回复最大尝试 `REPLY_MAX_ATTEMPTS` | `3` | `3` | 有默认（首次发送计 1 次） |
| 回复租约秒 `REPLY_LEASE_SECONDS` | `300` | `300` | 有默认（崩溃/慢 Worker 的回复在租约过期后被接管） |
| 回复退避基数秒 `REPLY_RETRY_BASE_SECONDS` | `2.0` | `2.0` | 有默认 |
| 回复退避上限秒 `REPLY_RETRY_MAX_SECONDS` | `3600` | `3600` | 有默认 |
| Cleanup Worker 开关 `CLEANUP_ENABLED` | `true` | `true` | 显式设 `false` 才关闭 |
| Cleanup 间隔秒 `CLEANUP_INTERVAL_SECONDS` | `3600` | `3600` | 最小 60 秒 |
| Cleanup 单类批量 `CLEANUP_BATCH_SIZE` | `500` | `500` | 每个短事务上限 |
| 成功 Event 保留天数 `EVENT_SUCCEEDED_RETENTION_DAYS` | `30` | `30` | 同时适用于 legacy_succeeded；最小 1 天 |
| dead Event 保留天数 `EVENT_DEAD_RETENTION_DAYS` | `90` | `90` | 默认长于成功记录 |
| sent Outbox 保留天数 `OUTBOX_SENT_RETENTION_DAYS` | `30` | `30` | 按 sent_at |
| dead Outbox 保留天数 `OUTBOX_DEAD_RETENTION_DAYS` | `90` | `90` | 按 updated_at；默认更长 |
| 高风险确认开关 `PENDING_ENABLED` | `true` | `true` | 图片/语音/批量/疑似重复先进待确认；显式设 `false` 恢复直写 |
| 确认单有效期秒 `PENDING_EXPIRES_SECONDS` | `86400` | `86400` | 24 小时；过期后无法确认/取消 |
| 终态确认单保留天数 `PENDING_RETENTION_DAYS` | `7` | `7` | executed/cancelled/expired/failed 按 updated_at 清理 |
| 疑似重复时间窗分钟 `PENDING_DUPLICATE_WINDOW_MINUTES` | `60` | `60` | 相同方向/金额/币种在此窗口内检查分类与来源 |
| 待确认列表上限 `PENDING_MAX_LIST` | `10` | `10` | `查看待确认` 最多展示条数 |
| Dashboard 开关 `DASHBOARD_ENABLED` | `false` | `false` | 关闭时不暴露页面或 Web API |
| Dashboard 外部 origin `DASHBOARD_BASE_URL` | 空 | `https://ledger.example.com` | 开启时必填，仅允许绝对 origin |
| Dashboard Session Secret | 空 | 空 | 开启时必填，至少 32 位高熵随机值，不得提交仓库 |
| Dashboard 管理员 open_id | 空 | 空 | 逗号分隔；未列入者为 USER |
| Dashboard Session TTL 秒 | `28800` | `28800` | 300～604800 |
| OAuth state TTL 秒 | `600` | `600` | 60～1800 |
| Dashboard Secure Cookie | `true` | `true` | 生产保持开启并使用 HTTPS |
| 日志级别 | **无此配置项** | — | — |
| Webhook 监听 | 进程内 `0.0.0.0:8000`（Compose 映射 `8000:8000`） | — | WebSocket 仅用于 healthz 时可内网访问 |
| Compose 应用服务名 | `app` | — | — |
| 数据库迁移 | 源码 Compose：`alembic upgrade head` 再 uvicorn | — | 自动 |
| healthz | `GET /healthz` | — | 验收 |
| readyz | `GET /readyz` | — | 数据库、migration 与后台任务验收 |

Compose 默认读取 `.env`；可用 `LARK_LEDGER_ENV_FILE` 指定其他文件。生产**不得**直接把 `.env.example` 当运行配置。

### 多模型配置（扩展）

需要图片或语音时再填写：

```dotenv
LARK_LEDGER_VISION_API_KEY=replace-with-provider-key
LARK_LEDGER_VISION_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
LARK_LEDGER_VISION_MODEL=qwen3.7-plus

LARK_LEDGER_TRANSCRIPTION_API_KEY=replace-with-provider-key
LARK_LEDGER_TRANSCRIPTION_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
LARK_LEDGER_TRANSCRIPTION_MODEL=qwen3-asr-flash
LARK_LEDGER_TRANSCRIPTION_LANGUAGE=zh
LARK_LEDGER_TRANSCRIPTION_ENABLE_ITN=true
```

图片或语音 Key 为空时，对应功能返回未配置提示，**不会**回退到文字模型，也**不会**阻止纯文字记账。

---

## PostgreSQL

### 方式 A：开发叠加（最简单）

见上文「一键本地体验」。适合本机试用与个人验证，不适合把 `dev-only-password` 暴露到公网。

### 方式 B：已有 PostgreSQL

由 DBA 或你自己创建独立用户与数据库（**示例密码请替换**；不要把真实密码写进 README 或提交到 Git）：

```sql
CREATE USER lark_ledger WITH PASSWORD 'replace-with-strong-password';
CREATE DATABASE lark_ledger OWNER lark_ledger;
```

连接串形态：

```text
postgresql+asyncpg://用户名:密码@主机:5432/数据库名
```

注意：

- 密码或用户名含 `@`、`:`、`/`、`#` 等特殊字符时，必须在 URL 中**百分号编码**。
- 数据库在宿主机时，容器内不能写容器自己的 `localhost`。Windows / macOS Docker Desktop 常用 `host.docker.internal`；Linux 或远程库请用容器可达的主机名或私网地址。
- 应用启动（源码 `compose.yaml`）会执行 `alembic upgrade head`；迁移失败则进程退出。
- **升级生产库前请备份**。本项目不自动创建数据库用户或库。
- 当前集成测试与 dev 镜像面向 **PostgreSQL 16**；其他大版本请自行验证。

仅启动应用（外部库已就绪）：

```bash
docker compose up -d --build
```

---

## 飞书权限

权限**标识名称以飞书开放平台控制台为准**；下列为按代码能力整理的用途说明。若无法与控制台逐字对齐，请在真实控制台核对后再授予。

### 文字-only 必需（用途）

- 接收用户消息（订阅 `im.message.receive_v1`）
- 以机器人身份回复文本消息
- 机器人能力开启，并将机器人加入测试会话

### CSV 导出额外需要

- 上传文件资源（代码调用 `POST /open-apis/im/v1/files`）
- 以**文件消息**回复当前会话（`msg_type=file`）

导出失败时用户会看到发送失败类提示；**v0.2.1（P06b）起文件投递由后台 Reply Worker 按指数退避自动重试**，重试复用已上传的 `file_key`。需在真实飞书租户确认文件上传与文件消息权限是否齐全。

### 图片 / 语音扩展额外需要

- 下载消息中的图片 / 文件资源（代码经 `messages/{message_id}/resources/{file_key}`）
- 消费报告若发送图片卡片，还需要上传图片类能力

### 配置步骤摘要（WebSocket）

1. 创建企业自建应用
2. 启用机器人能力
3. 事件与回调选择**长连接**，不填请求地址
4. 订阅 `im.message.receive_v1`
5. 添加上文「文字-only」权限
6. 发布应用版本
7. 将机器人加入可测试会话
8. 应用已连接后发送第一条消息

长连接验证通常要求进程已在线。Webhook 步骤见下文。

---

## 长连接模式（WebSocket，推荐首次部署）

- **推荐首次部署**；不需要公网回调 URL
- 适合 NAS、家庭服务器和内网
- 仍需服务器**主动出站**访问飞书与 AI API

```dotenv
LARK_LEDGER_EVENT_MODE=websocket
LARK_LEDGER_LARK_APP_ID=cli_xxxxxxxxxxxxx
LARK_LEDGER_LARK_APP_SECRET=replace-me
```

不需要 Verification Token 或 Encrypt Key。

`GET /healthz` 中 `long_connection` 可能为：

| 值 | 含义 |
| --- | --- |
| `connected` | 连接可用 |
| `connecting` | 正在建立 |
| `reconnecting` | 断线重连中 |
| `error` | 连接线程异常停止 |
| `stopped` / `stopping` | 应用正在停止 |
| `disabled` | 当前为 Webhook 模式 |

使用 Uvicorn `--reload` 时，由实际 worker 生命周期管理连接。不要同时运行多个长期连接实例，除非已确认事件分发符合预期。

---

## Webhook 模式（高级 / 生产替代）

Webhook **仍然支持**，适合已有公网 HTTPS、反向代理或平台化入口的部署。它不是废弃路径，只是**不作为**首次快速路径的默认推荐。

```dotenv
LARK_LEDGER_EVENT_MODE=webhook
LARK_LEDGER_LARK_VERIFICATION_TOKEN=replace-me-for-webhook
LARK_LEDGER_LARK_ENCRYPT_KEY=replace-with-encrypt-key
```

开放平台事件发送地址：

```text
https://你的域名/webhooks/feishu
```

服务支持 URL verification、Verification Token 校验、`X-Lark-Signature` 验签和加密事件解密。配置 Encrypt Key 后，飞书后台与 `.env` 必须一致。

Webhook 在验证来源后尽快返回，实际处理在 FastAPI 后台任务中执行；任务与 Web 进程同生共死，**不具备**持久队列的重试与故障恢复（当前版本整体仍为 claim-first）。

长连接模式下 Webhook 端点返回 404；Webhook 模式下不会启动长连接。

---

## Docker Compose 部署

### 源码构建（推荐，在正式 GHCR 发布前）

```bash
cp .env.example .env
docker compose up -d --build
# 或叠加开发库：
docker compose -f compose.yaml -f compose.dev.yaml up -d --build
docker compose logs -f app
curl http://127.0.0.1:8000/healthz
```

应用容器启动时先执行 `alembic upgrade head`，再启动 Uvicorn。迁移失败时应用不会启动。

### 使用 GHCR 预构建镜像（可选）

`compose.image.yaml` 使用镜像 `ghcr.io/0verme/larkledger:${LARK_LEDGER_IMAGE_TAG:-latest}`。当前正式版本为 **0.7.0**：

```bash
export LARK_LEDGER_IMAGE_TAG=0.7.0
# PowerShell: $env:LARK_LEDGER_IMAGE_TAG = "0.7.0"
docker compose -f compose.image.yaml pull
docker compose -f compose.image.yaml run --rm app alembic upgrade head
docker compose -f compose.image.yaml up -d
curl http://127.0.0.1:8000/healthz
```

镜像启动**不会**像源码 `compose.yaml` 那样自动跑迁移；升级请显式 `alembic upgrade head`。生产应固定版本标签，避免长期跟随未固定的 `latest`。详见[升级指南](upgrading.md)。

### 飞牛 NAS 可选脚本

`scripts/deploy-fnos.sh` 是面向飞牛 NAS 的可选 Git 更新入口，不是通用安装器。默认目录与 `REPO_URL` 可能需按 fork 修改。

---

## 健康检查与日志

```bash
docker compose ps
# 使用 dev 叠加时：
docker compose -f compose.yaml -f compose.dev.yaml ps

docker compose logs -f app
curl http://127.0.0.1:8000/healthz
curl -f http://127.0.0.1:8000/readyz
```

使用 `docker compose logs --timestamps` 时，Docker 附加的时间戳以 `Z` 结尾，表示
UTC，例如 `2026-08-07T07:28:09Z` 对应北京时间 `2026-08-07 15:28:09`。这是
Docker 日志元数据的正常格式，不表示飞牛 NAS 或容器系统时区配置错误；
`LARK_LEDGER_TIMEZONE` 仍用于机器人消息、账目和报告中的业务时间展示。

`/healthz` 是 liveness，只确认 HTTP 进程能够响应，不访问数据库、飞书或 AI；即使
PostgreSQL 不可用也仍返回 200。`/readyz` 是 readiness，会执行 `SELECT 1`、核对数据库
revision 与代码唯一 Alembic head，并读取已启用 Event / Reply Worker 和 WebSocket receiver
的任务状态。Webhook 模式不要求 receiver，显式关闭的 Worker 是合法兼容模式。未就绪返回
HTTP 503；探针不会自动迁移数据库，也不会访问飞书、AI、DNS 或其他外部网络。

Cleanup Worker 默认每小时执行终态小批量清理。成功 Event / sent Outbox 默认保留 30 天，
dead Event / dead Outbox 默认保留 90 天；非终态、有有效 lease、仍有关联 Outbox 的 Event
不会删除，账本与 revision 永不删除。清理日志不记录 payload、回复、用户或消息标识。清理
不等于数据库备份；缩短保留期前应先确认审计需求。

### 管理员人工事件重放

人工事件重放是服务器管理员命令，不是飞书聊天命令，也不同于只重发 Outbox 的结果回放：

```bash
# 默认 dry-run：只输出脱敏预检，不修改数据库
python -m lark_ledger.admin replay-event \
  --event-id <event_id> \
  --operator <operator> \
  --reason "temporary upstream outage"

# 只有显式 --execute 才会锁定、重新预检并排回 received
python -m lark_ledger.admin replay-event \
  --event-id <event_id> \
  --operator <operator> \
  --reason "temporary upstream outage" \
  --execute
```

`operator` 与 `reason` 必填并有长度上限；完整值只写入审计表，不回显到 CLI JSON 或日志。
有 Outbox 时应走结果回放，不能重新执行业务；已有来源账目、payload 不完整、active lease、
状态不合法或历史原子性无法证明时默认拒绝。执行成功会在同一事务写审计并将自动尝试计数
开启一个新的有限窗口。该命令不能替代数据库备份，也不能替代对模糊事件的人工取证。

健康检查**不会**回显凭据、完整异常、事件或回复内容。排查时不要在工单中粘贴 App
Secret、AI Key、数据库密码、完整消息正文。

### 常见问题区分

| 现象 | 优先检查 |
| --- | --- |
| 容器未启动 / 反复退出 | `docker compose ps`、`logs`；构建错误或启动命令失败 |
| PostgreSQL 无法连接 | URL 主机是否容器可达、账号密码、库是否存在、dev 库是否 healthy |
| Alembic 迁移失败 | 权限、网络、是否连错库、日志中的 migration 错误（升级前应有备份） |
| WebSocket 未建立 | `EVENT_MODE=websocket`、App ID/Secret、出站网络、healthz 的 `long_connection`、readyz 的 `receiver` |
| 飞书权限不足 | 是否发布版本、机器人是否在会话中、是否订阅消息事件 |
| AI API 鉴权失败 | Key、Base URL、模型名是否匹配供应商 |
| AI 返回格式错误 | 模型是否支持 JSON / 结构化输出；超时是否过短 |
| CSV 上传失败 | 文件上传与文件消息权限；行数/体积是否超限 |

---

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

本地同样需要可访问的 PostgreSQL。长连接模式下先启动应用，再到开放平台验证连接。

---

## 生产安全

- 将 `.env` 限制为运行账号可读；Linux 可 `chmod 600 .env`
- Webhook 必须位于 HTTPS 反向代理之后；长连接的 `8000` 可仅内网开放用于 healthz
- PostgreSQL 不要暴露到公网；跨不可信网络时启用数据库 TLS
- AI Key、App Secret、Verification Token、Encrypt Key 只通过环境变量或密钥管理注入
- 日志不得记录环境变量快照、数据库 URL、Authorization 头、完整消息正文、媒体内容或未脱敏 AI 响应
- 定期备份 PostgreSQL 并演练恢复；升级前备份
- 轮换曾出现在聊天、工单、源码或日志中的凭据

---

## 已知限制（部署视角）

与产品手册一致，部署文档也不使用「可靠投递」「永不丢消息」等措辞：

- 事件处理失败会由事件 Worker 自动重试（指数退避）并最终进入 `dead`；业务变更与回复意图通过 **Transactional Outbox**（P06a）同一事务提交，崩溃重试不会重复执行业务，但仍不宣称"绝不重复记账"，来源唯一约束为兜底
- 回复发送失败（P06b）由后台 Reply Worker 自动重试（指数退避）并最终进入回复 `dead`；发送失败**绝不**重新执行业务。进程重启后继续投递 `pending` / `failed` 回复
- Dashboard 管理员可以重发现有 `dead` / `failed` Outbox 结果，也可对 `dead` / `failed` Event 先 dry-run 再二次确认重放；普通用户无权访问，且没有批量重放
- 极端窗口下（飞书已发送但本地未标记 `sent` 后崩溃，且重发间隔超过飞书 `uuid` 幂等的 1 小时窗口）用户可能收到重复回复；该窗口**不会**导致重复执行业务或重复记账
- 图片 / 语音 / 批量 / 疑似重复使用冻结结果进行写入前确认；没有多级审批或多人共享确认
- Dashboard 只有 `USER` / `ADMIN`，无企业多租户、组织树、复杂 RBAC 或共享账本
- JSON 导出不是正式能力

路线主题（**无发布日期承诺**）：

```text
v0.2.1：可靠投递（事件 Worker / 租约 / 重试 / dead 已完成；Transactional Outbox 已完成；
        后台回复 Worker / 回复租约 / 回复重试 / 回复 dead 已完成；结果回放为内部能力）
v0.3.0：高风险确认
v0.4.0：Web Dashboard（账目、确认、分析与可靠性运维）
v0.5.0：账本级账户与转账（P26/P27）
v0.6.0：预算 2.0 与周期账单（P28/P29）
```

---

## 部署验收清单

- [ ] PostgreSQL 使用独立账号（生产非示例密码），应用能完成 Alembic 迁移
- [ ] `.env` 不在 Git 跟踪列表，权限仅服务账号可读
- [ ] `LARK_LEDGER_EVENT_MODE=websocket`（或你明确选择的 webhook）
- [ ] 飞书文字-only 权限与 `im.message.receive_v1` 已配置并发布版本
- [ ] `GET /healthz` 为 `status: ok`；`GET /readyz` 返回 200，长连接为 `connected`（WebSocket）
- [ ] `午饭32元` 可记账且回复含 `#XXXXX`；`最近10笔` 可核对
- [ ] （可选）CSV 导出在真实飞书确认文件权限后可用
- [ ] （可选）图片 / 语音 Key 与资源权限仅在需要时配置并验证
- [ ] （可选）Dashboard 使用 HTTPS，OAuth 回调、强 Session Secret、管理员 open_id 与 Secure Cookie 已配置
- [ ] （启用 Dashboard 时）OAuth 登录、用户隔离、账目 revision、Pending、CSV 与管理员健康页面已验证
- [ ] 已建立备份、恢复与凭据轮换流程
