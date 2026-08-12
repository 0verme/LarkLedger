# LarkLedger（飞账）

[English](README.en.md) | 简体中文

> 自托管的飞书 / Lark AI 记账机器人。账本保存在你自己的 PostgreSQL 中；大模型只负责把消息变成经过严格校验的业务动作。

[![CI](https://github.com/0verme/LarkLedger/actions/workflows/ci.yml/badge.svg)](https://github.com/0verme/LarkLedger/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)

## 当前能力（v0.9.0 主线）

- **平台 / 通道无关 Core（v0.9.0）**：Feishu / Web / Client API 三套入口共享同一个 `ClientApplicationService` Application Layer——对于相同业务事实产生一致 Domain Result，Core 不依赖任何渠道 transport（架构守护见 `tests/architecture/`）。正式通道无关 Client API：**`/api/v1`**（`/api/client/v1` 为同一组 handler 的兼容别名）；Bearer API Token（`llv1_*`，明文只显示一次、DB 只存 SHA-256 digest、可 revoke / expiry、scope 只缩权），headless client 不需要 Feishu、浏览器 cookie 或 OAuth session 即可独立完成认证 / 选账本 / 记账 / 查询 / Overview / Goals / Insights；写请求强制 `Idempotency-Key`（同 key 重试 replay、不同 body 409、PostgreSQL 并发 exactly-once）；稳定 error envelope 与 OpenAPI 契约（见 [Client API 文档](docs/client-api.md)）
- **财务目标（Goals，v0.8.0）**：把“想存到多少钱”变成可跟踪的目标（`应急储备 60000`）；进度来自**真实账本**——目标绑定现金 / 资产账户，`current_amount` 始终等于绑定账户实时余额之和，目标不保存、不手工维护余额，记账 / 删账 / 恢复 / 转账变化会自动重算。支持目标日期与确定性 forecast；可见性继承绑定账户（引用任何私人账户的目标对他人完全不可见，防止通过目标显示泄漏私人余额）；目标不是虚拟账户 / 资金池，创建 / 修改 / 删除从不触碰账户、账目或转账。飞书 `我的目标 / 目标 / 查看目标` 与 Web `/goals` 同源
- **确定性洞察（Insights，v0.8.0）**：从真实账本自动发现值得注意的事实——支出变化（本月 vs 近 3 个月平均）、预算风险（使用率快于时间进度）、未来 30 天周期支出（按币种分组）、目标进度 / 预计缺口。全部由确定性规则计算，AI 不参与计算、不访问数据库，只可选改写解释文案；AI 不可用时自动回退确定性摘要。私人数据不会通过任何洞察侧信道泄漏。飞书 `洞察 / 财务洞察 / 本月洞察` 与 Web `/insights` 同源。**洞察是财务数据解释与提醒，不是金融顾问**——不提供投资、股票、理财、贷款、税务建议，不做任何自动资金操作

- **家庭共享账本（v0.7.0）**：一个家庭 = 一个内部用户群 + 一个独立公共账本，多个真实成员可共同记账；
  - **付款人归属**：区分「谁记账」与「谁付钱」（`created_by ≠ paid_by`），支持按别名 / 显示名 / open_id / UUID 确定性解析付款人（`B 买菜120` → B 付款）；成员别名由户主维护，收款/支出按付款人聚合
  - **家庭总览**：一个确定性的“家庭首页”视图（本月收支 / 预算进度 / 成员支出 / 主要分类 / 未来周期支出 / 最近交易 / 账户余额），飞书 `概览 / 家庭概览 / 家庭开销` 与 Web `/overview` 同源
  - **账户级隐私**：账户可设为 `共享`（成员可见）或 `私人`（仅本人可见）；私人账户的余额、账目、周期账单、待确认、预算消耗与统计对他人完全不可见，个人账本行为不受影响
- **周期账单（Recurring Rules）**：把已知未来周期性收支建成确定性规则（`每月8号房租3500` / `每年6月15日保险2000` / `每周健身房100`）；到期时 Recurring Worker 生成一个冻结的待确认单并主动发飞书提醒卡片，**确认后才正式入账**——规则与 Pending 永不消耗预算，只有确认后的支出计入预算。支持暂停 / 恢复 / 跳过 / 停用，修改只影响未来周期，同一规则同一期在任何并发 / 重试下只会产生一个 Pending 与一条交易
- **预算 2.0**：月度总预算与分类预算以显式月份为周期，计划 vs 实际、剩余、使用率与超支状态实时派生；转账永不进入预算，删除 / 恢复 / 修订按当前有效事实重算
- **账本级账户与转账**：每笔收支可绑定账户（现金 / 资产 / 负债）；账户支持期初余额、改名、设默认与归档；`转账` 独立于收支统计；飞书与 Web 均可按当前账本查看单账户余额、总资产、总负债与净资产
- **个人多账本**：飞书确定性命令与 Web 选择器可创建、列出、切换、设默认和重命名个人账本；账目、预算、短 ID、统计、报告、导出、revision、判重与 Pending 全部按当前账本隔离
- **家庭空间 MVP**：创建家庭、邀请已有内部用户、处理邀请与成员；每个家庭自动获得独立公共账本，个人账本不会因加入家庭而共享或复制
- **文字记账**，成功回复含当前账本内唯一五位短 ID（`#XXXXX`）；简单明确的单笔文字仍直接入账
- **最近账目 / 单笔详情**（例如 `最近10笔`、`查看 #XXXXX`）
- **按短 ID 修改、软删除与恢复**（另保留「上一笔」快捷方式）
- **CSV 导出**本人账目（飞书文件消息；需额外文件权限）
- 汇总、分类月预算、消费报告
- **高风险确认**：图片 / 语音 / 批量 / 疑似重复先进入待确认（`#C-XXXXX`），可文本 `确认`/`取消` 或点卡片按钮，确认时才用冻结结果写账
- 多用户隔离（`open_id`）、事件 `event_id` 幂等 claim
- **可靠投递**：事件 / 回复后台 Worker、事务性回复 Outbox、PostgreSQL 租约与指数退避重试、readiness、终态清理与受控人工事件重放
- **Web Dashboard**：飞书 OAuth、财务总览、账目与 revision、Pending、分析、预算、**财务目标（/goals，创建 / 编辑 / 进度 / 归档 / 删除）**、**洞察卡片（/overview「值得关注」）**、周期账单、报表、CSV 下载及管理员可靠性控制台
- **通道无关 Client API（v0.9.0）**：正式契约 `/api/v1`（`/api/client/v1` 为兼容别名）为 CLI / 硬件 / 未来客户端提供结构化命令/查询边界；Bearer 个人令牌（`llv1_`，只保存 SHA-256 摘要、可撤销、可过期、scope 只缩权）与持久化 `Idempotency-Key`。飞书与 Web 只是 Adapter，与 API 共用同一个 `ClientApplicationService`，同一业务事实产生一致 Domain Result
- 自托管：FastAPI、React / TypeScript / Vite、PostgreSQL、Docker Compose

完整消息示例见[用户手册](docs/help.md)。

## 文字直写与高风险确认

简单明确的单笔文字保持直写：`午饭32元` → 直接入账并返回账目短 ID。图片、语音、批量或疑似重复则先确认：

```text
发送小票图片 → 飞账识别 → 返回确认卡片 → 用户确认 → 才写入账本
```

卡片不可用时可发送 `确认 #C-A83F2`、`取消 #C-A83F2`；发送 `查看待确认`（或 `确认列表`）可列出当前待确认单。确认使用创建 pending 时冻结的结构化命令，不会重新调用 AI。

## Web Dashboard

v0.7.0 提供可选的中文 Web Dashboard，生产镜像已内置前端静态资源，无需额外 Node.js 服务：

- 财务总览、服务端分页账目管理、revision 时间线、软删除与恢复；账目列表显示账户、创建/编辑时可选择账户
- **账户管理**：列表、创建、改名、设默认、归档、单账户余额与总资产/负债/净资产
- **转账管理**：创建、详情（含操作记录）与撤销
- **周期账单**：列表（名称 / 金额 / 账户 / 周期 / 下次日期 / 状态 / 待确认）、创建、修改、暂停、恢复、跳过与停用
- 待确认查看、确认与取消（复用冻结预览，不重新调用 AI）
- 趋势、分类、月度分析、预算、报告和受限 CSV 下载
- 管理员 Event / Outbox / Dead / Replay、健康状态与只读脱敏配置

Web 与飞书共享同一套 `LedgerService`、`PendingCommandService`、revision、Outbox 与 PostgreSQL 用户隔离。启用后访问 `https://你的域名/`，通过飞书 OAuth 登录：

```dotenv
LARK_LEDGER_DASHBOARD_ENABLED=true
LARK_LEDGER_DASHBOARD_BASE_URL=https://ledger.example.com
LARK_LEDGER_DASHBOARD_SESSION_SECRET=请生成至少32位的高熵随机值
LARK_LEDGER_DASHBOARD_ADMIN_OPEN_IDS=ou_xxx,ou_yyy
```

在飞书应用中登记回调地址 `https://ledger.example.com/api/web/v1/auth/callback`，并授予 `auth:user.id:read`。生产必须使用 HTTPS；反向代理需正确传递 `X-Forwarded-Proto`，应用服务器只应信任明确的代理地址。完整配置与安全说明见[环境与部署指南 · Web Dashboard](docs/environment.md#web-dashboard可选)。不开启时，Dashboard 页面与 `/api/web/v1/*` 均不暴露，机器人和 Worker 保持原行为。

## 适合谁

- 技术用户，能配置 Docker 与 PostgreSQL
- 重度飞书用户，希望**自托管**个人账本
- 首次部署只想尽快完成**一笔纯文字记账**

不适合：需要多级审批、儿童额度、银行卡同步、复杂 RBAC 或企业复式账套。可靠投递与高风险确认已实现，但**仍不**宣称「绝不重复记账」或「绝对零重复回复」（见下方已知限制）。

## 快速开始（推荐）

主路径：**WebSocket 长连接 + 文字-only + PostgreSQL + Docker Compose**。

不需要公网回调 URL；服务器仍需能**主动访问**飞书开放平台与文字 AI API。

```text
已有 Docker
→ 创建飞书自建应用（机器人 + 长连接 + 消息事件）
→ 准备 PostgreSQL（推荐 compose.dev 一键体验）
→ 填写最小 .env
→ docker compose 启动
→ 检查 healthz 与日志
→ 飞书发送「午饭32元」
→ 收到含 #XXXXX 的记账回复
→ 发送「最近10笔」核对
```

### 1. 复制环境变量

```bash
cp .env.example .env
```

Windows PowerShell：`Copy-Item .env.example .env`

### 2. 填写最小必填项

文字-only 快速路径只需：

```dotenv
LARK_LEDGER_EVENT_MODE=websocket
LARK_LEDGER_DATABASE_URL=postgresql+asyncpg://lark_ledger:change-me@db:5432/lark_ledger
LARK_LEDGER_LARK_APP_ID=cli_xxxxxxxxxxxxx
LARK_LEDGER_LARK_APP_SECRET=replace-me
LARK_LEDGER_AI_API_KEY=replace-me
LARK_LEDGER_AI_BASE_URL=https://api.deepseek.com
LARK_LEDGER_AI_MODEL=deepseek-v4-flash
```

说明：

| 项 | 说明 |
| --- | --- |
| `EVENT_MODE` | **请在 `.env` 显式设为 `websocket`**。代码运行时默认仍是 `webhook`，与推荐路径不同 |
| `DATABASE_URL` | 使用 `compose.dev.yaml` 时，Compose 会覆盖为开发库地址；自备库时改为容器可达的 URL |
| 文字 AI | 代码默认指向 OpenAI 兼容地址；示例推荐 DeepSeek，请按你的服务填写 |
| 图片 / 语音 Key | **文字-only 不需要**。留空只禁用对应能力 |
| Webhook 验签 | 长连接**不需要** Verification Token / Encrypt Key |

有安全默认值、通常不必改：`TIMEZONE=Asia/Shanghai`、`CURRENCY=CNY`。完整变量表见[环境与部署指南](docs/environment.md)。

### 3. 准备 PostgreSQL

**推荐（本地 / 个人试用）：** 使用开发叠加文件同时启动应用与 PostgreSQL 16：

```bash
docker compose -f compose.yaml -f compose.dev.yaml up -d --build
```

- 开发库账号密码仅用于本机或个人测试，**不要**当作互联网生产密码
- 数据在命名卷 `lark_ledger_dev_pgdata`；`down` 默认保留数据，彻底清理用 `down -v`
- 库端口默认映射到本机 `127.0.0.1:5432`，冲突时可设 `LARK_LEDGER_DEV_POSTGRES_PORT`

**已有 PostgreSQL：** 创建低权限用户与库（示例，请替换密码），再把 `LARK_LEDGER_DATABASE_URL` 指过去，并用：

```bash
docker compose up -d --build
```

SQL 示例与 URL 注意事项见[环境与部署指南 · PostgreSQL](docs/environment.md#postgresql)。

### 4. 配置飞书应用（文字-only）

1. 在[飞书开放平台](https://open.feishu.cn/app)创建**企业自建应用**，开启**机器人**能力
2. 事件与回调：选择**使用长连接接收事件**（不要填请求地址）
3. 订阅 `im.message.receive_v1`（接收消息）
4. 申请**接收消息 / 发送消息**等文字对话所需最小权限（权限标识请在控制台与[飞书文档](https://open.feishu.cn/document/)核对）
5. **发布应用版本**使配置生效
6. 将机器人加入可测试的单聊或群；群聊中需 `@机器人`
7. 应用进程已启动并建立长连接后，再在控制台点击连接验证（如需要）

CSV 导出、图片、语音所需权限见[环境与部署指南 · 飞书权限](docs/environment.md#飞书权限)。

### 5. 启动后检查

```bash
docker compose -f compose.yaml -f compose.dev.yaml ps
docker compose -f compose.yaml -f compose.dev.yaml logs -f app
curl http://127.0.0.1:8000/healthz
curl -f http://127.0.0.1:8000/readyz
```

仅使用 `compose.yaml` 时，去掉 `-f compose.dev.yaml` 即可。服务名是 `app`，宿主机端口默认 **8000**。

源码 Compose 启动命令会先执行 `alembic upgrade head` 再启动 Uvicorn；迁移失败则应用不会起来。

长连接正常时，健康检查类似：

```json
{"status":"ok","event_mode":"websocket","long_connection":"connected"}
```

`connecting` / `reconnecting` 表示尚未就绪或正在重连。

`/healthz` 只表示 HTTP 进程存活，不访问数据库。`/readyz` 还会轻量检查
PostgreSQL、当前 Alembic revision、已启用的 Event / Reply Worker，以及 WebSocket
模式下的接收器；不具备承接条件时返回 HTTP 503，且不会探测飞书或 AI。

### 6. 第一笔账验收

在飞书中依次发送（将 `#XXXXX` 换成机器人**实际返回**的短 ID）：

| 你发送 | 预期 |
| --- | --- |
| `午饭32元` | 成功记账回复，含五位短 ID，如 `#A83F2` |
| `最近10笔` | 列表中能看到刚才那笔 |
| `查看 #XXXXX` | 单笔详情（金额、分类、时间等） |
| `把 #XXXXX 改成35元` | 金额变为 35 |
| `删除 #XXXXX` | 软删除；列表默认不再显示 |
| `恢复 #XXXXX` | 恢复后可再次出现在列表中 |
| `导出最近90天账单` | 收到 CSV 文件消息（**需额外确认飞书文件上传与文件消息权限**；本仓库未宣称已在真实飞书完成该验收） |

## 事件接入方式

| 模式 | 定位 | 公网 HTTPS | 额外凭据 |
| --- | --- | --- | --- |
| **WebSocket 长连接（推荐首次部署）** | NAS、家庭服务器、内网 | 不需要 | App ID / Secret |
| **Webhook（高级 / 生产替代）** | 已有公网入口或反向代理 | 需要 | Verification Token；推荐 Encrypt Key |

Webhook 回调地址：`https://你的域名/webhooks/feishu`。详细配置见[环境与部署指南](docs/environment.md)。Webhook 仍是正式支持路径，并非废弃。

## 已知限制（诚实边界）

**不要**理解为"绝不丢消息 / 绝不重复记账"的可靠投递：

- 事件处理失败**会自动重试**（指数退避，默认最多 3 次）并在耗尽或永久错误时进入 `dead`；业务变更与回复意图通过 **Transactional Outbox** 在同一事务提交（P06a），崩溃重试不会重复执行业务，但仍**不**宣称"绝不重复记账"，来源唯一约束为兜底保障
- 回复发送失败**会自动重试**（P06b）：后台 Reply Worker 用 `FOR UPDATE SKIP LOCKED` 领取已提交的 Outbox、数据库租约、指数退避重试、永久错误或重试耗尽进入回复 `dead`；发送失败**绝不**重新执行业务，进程重启后继续投递 `pending` / `failed` 回复
- 每次回复携带稳定的飞书 `uuid` 幂等键（Outbox 行 ID）：1 小时内崩溃重发由飞书去重；极端情况（飞书已发送但本地未标记 `sent` 后崩溃，且重发间隔超过 1 小时）用户可能收到重复回复，但**绝不会**导致重复执行业务或重复记账
- 轻量 Cleanup Worker 默认按小批次清理终态投递记录：成功 Event / 已发送 Outbox 默认保留 30 天，dead 默认保留 90 天；不会删除账本或 revision。清理不是数据库备份，调整保留期前应评估审计要求
- 管理员可通过 Dashboard 或默认 dry-run 的 `python -m lark_ledger.admin replay-event` 受控重放安全的 `dead` / `failed` 事件；显式二次确认或 `--execute` 才会重新执行业务并写独立审计。存在 Outbox、已有账目结果或历史原子性无法证明时一律拒绝；结果重发只消费已有 Outbox，绝不重新执行业务
- **高风险确认（v0.3.0）**：图片 / 语音 / 批量 / 疑似重复先进入待确认 `#C-XXXXX`，确认或取消才结束。确认单 24 小时过期；确认使用冻结解析结果，绝不重新调用 AI。确认单只属于当前用户，无多级审批或多人共享确认
- **隐私是账户级的，不是字段级**：私人账户只隐藏「余额 / 账目 / 周期规则 / 待确认 / 预算消耗 / 成员统计」，不隐藏成员身份、别名或付款人聚合口径；没有基于金额阈值、分类或字段的 ACL
- Dashboard 仅提供 `USER` / `ADMIN` 两种角色；无企业多租户、组织树、复杂 RBAC 或共享账本
- **不是 AA / Splitwise**：没有分摊、结算、债务关系或人均拆账；**不是复式记账**：一人公司科目 / 凭证 / 借贷仍属远期领域，不把会计字段加入个人收支表；**不是企业财务**：无审计链路、审批流、多币种汇率结算或财务报告义务
- JSON 导出**不是**正式能力（当前仅 CSV）

后续路线不在本次发布承诺内；v0.9.0 不扩展为多租户财务 ERP / OAuth Authorization Server / SaaS API Gateway。

镜像与版本：当前正式版本为 **v0.9.0**（Platform / Channel-Neutral Core）。预构建镜像：`ghcr.io/0verme/larkledger:0.9.0`（亦有 `0.9` / `latest`；也可用源码 `docker compose ... --build`）。升级与迁移说明见[升级指南](docs/upgrading.md)。

## 效果展示

| 批量图片流水 | 语音批量记账 |
| --- | --- |
| ![从支付流水截图批量记账](docs/assets/batch-image-bookkeeping.png) | ![将语音中的多笔消费批量记账](docs/assets/voice-batch-bookkeeping.png) |
| 小票识别 | 复杂文字批量记账 |
| ![识别超市小票并记录消费](docs/assets/receipt-bookkeeping.png) | ![从一段自然语言中识别多笔收支](docs/assets/text-batch-bookkeeping.png) |

以上截图使用已获准公开的脱敏数据。识别结果以机器人确认回复为准。

## 安全边界

```text
飞书消息 → 来源校验 / 事件去重 → 媒体下载 → AI 结构化解析
                                              ↓
                              Pydantic 严格校验（禁止额外字段）
                                              ↓
                              固定业务动作 → SQLAlchemy → PostgreSQL
```

AI 不能访问数据库，也不能生成或执行 SQL。更多设计见[架构说明](docs/architecture.md)。

## 架构原则：飞书是 Adapter

```text
                ┌──────── Feishu Adapter（消息 / 卡片 / 事件 Worker）
                ├──────── Web Adapter（OAuth 会话路由）
Client Layer ───┼──────── Client API（/api/v1，Bearer 令牌）
                ├──────── CLI / Future
                └──────── Hardware / Future
                           │
                           ↓
                 Application Layer（ClientApplicationService）
                           │
                           ↓
                       Domain / Core（账本 / 预算 / 隐私 / 目标 / 洞察）
                           │
                           ↓
                       Repository → PostgreSQL
```

- **依赖方向只有一种**：Adapter → Application → Domain → Core。Core 从不
  import Feishu 客户端、FastAPI Request 或渠道事件 DTO（CI 有 AST 架构守护
  测试强制）。
- `RequestContext`（actor / ledger / source）平台无关；`source` 只是审计
  元数据，不决定业务结果。
- 移除飞书后核心业务完整成立；新增客户端只需实现 Adapter。见
  [Client API 文档](docs/client-api.md)。

## 本地开发

需要 Python 3.11+ 与可访问的 PostgreSQL：

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

提交前：

```bash
ruff check .
mypy src
pytest --cov
```

## 使用预构建镜像（可选）

```bash
export LARK_LEDGER_IMAGE_TAG=0.9.0
# PowerShell: $env:LARK_LEDGER_IMAGE_TAG = "0.9.0"
docker compose -f compose.image.yaml pull
docker compose -f compose.image.yaml run --rm app alembic upgrade head
docker compose -f compose.image.yaml up -d
curl http://127.0.0.1:8000/healthz
```

镜像不会在 `up` 时自动迁移；请先 `alembic upgrade head`。仍推荐首次用源码 + WebSocket 文字路径完成第一笔账。

## 文档

- [用户手册](docs/help.md)：消息示例、预算、报告、限制、FAQ
- [环境与部署指南](docs/environment.md)：完整变量、PostgreSQL、飞书权限、Webhook、排查
- [架构说明](docs/architecture.md)
- [Client API（`/api/v1`）](docs/client-api.md)
- [产品演进路线](docs/roadmap.md)
- [升级指南](docs/upgrading.md)
- [变更日志](CHANGELOG.md) · [v0.9.0 发布说明](.github/release-notes/v0.9.0.md) · [v0.8.0 发布说明](.github/release-notes/v0.8.0.md) · [v0.7.0 发布说明](.github/release-notes/v0.7.0.md) · [v0.6.0 发布说明](.github/release-notes/v0.6.0.md) · [v0.5.0 发布说明](.github/release-notes/v0.5.0.md)
- [贡献指南](CONTRIBUTING.md) · [安全策略](SECURITY.md)
- [English README](README.en.md)

## License

LarkLedger 使用 [Apache License 2.0](LICENSE) 开源。
