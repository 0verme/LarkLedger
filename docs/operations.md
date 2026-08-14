# 运维与可观测性（Operations）

本文档是生产实例的运维手册：健康检查契约、运行身份、日志关联、故障定位。
备份 / 恢复见 [backup-restore.md](backup-restore.md)，发布与回滚见 [release-sop.md](release-sop.md)。

## 状态语义（alive / ready / degraded / unhealthy）

P42 建立四层稳定语义，运维告警与自动恢复必须区分它们：

| 状态 | HTTP | 含义 | 自动动作 |
| --- | --- | --- | --- |
| `alive` | `/healthz` 200 | HTTP 进程存活 | 无（容器 healthcheck 依据） |
| `ready` | `/readyz` 200 | 可以承接流量 | 开始分发流量 |
| `degraded` | `/readyz` 200 + `degraded: true` | 可用但有非关键异常 | 告警观察，**不要重启** |
| `not ready` | `/readyz` 503 | 关键依赖不可用 | 停止流量，修复后自愈 |

原则：**业务积压（如 dead-letter = 1）永远不应该让 `/readyz` 变成 503**
（否则容器会进入「重启 → 重启」循环，反而延长恢复）。积压类异常在
`/ops/status` 中体现为 `backlog.dead > 0`，属于 degraded，不是 unhealthy。

## `/healthz`

- 含义：**HTTP application process alive**。
- 特点：快、不访问外部网络、不执行昂贵 SQL、不因 worker backlog 等业务状态失败。
- 响应（200）：

```json
{"status": "ok", "event_mode": "webhook", "long_connection": "disabled"}
```

Docker HEALTHCHECK 使用 `/healthz`（而不是 `/readyz`）：503 表示「还没准备好承接流量」，
不应触发容器重启。

## `/readyz`

- 含义：**当前实例是否具备正常提供服务的条件**。
- 检查项（真实组件，非虚构）：

```text
application / database / migration / event_worker / reply_worker /
cleanup_worker / recurring_worker / receiver
```

- 关键依赖（database / migration / 启用的 worker / receiver）失败 → **503**，并在
  `checks` 中明确失败组件与原因；`cleanup_worker` 失败只降级为 `warning`。
- 响应（200）：

```json
{
  "status": "ready",
  "degraded": false,
  "checks": {
    "application": {"status": "ok"},
    "database": {"status": "ok"},
    "migration": {"status": "ok", "current": "20260814_0027", "expected": "20260814_0027"},
    "event_worker": {"status": "ok", "started": true, "running": true, "last_sweep_at": "..."},
    "reply_worker": {"status": "ok"},
    "cleanup_worker": {"status": "ok"},
    "recurring_worker": {"status": "disabled"},
    "receiver": {"status": "disabled"}
  }
}
```

- migration 检查：读取 `alembic_version` 与代码解析的 Alembic head 对比；
  落后时 `readyz = 503`，`migration.reason = "migration_revision_mismatch"`。
  **readiness 从不自动执行 migration**。
- worker stale：worker 任务存活但循环心跳（`last_sweep_at`）超过
  `LARK_LEDGER_READINESS_STALE_AFTER_SECONDS`（默认 30s）未推进 → `warning`
  - `reason: "worker_stale"`（degraded，不是 503）。事件/reply worker
  高频轮询（1s），直接使用该阈值；低频 worker（recurring 300s、
  cleanup 3600s）的 stale 窗口按自身 sweep 周期缩放（至少两倍周期），
  避免健康低频 worker 被永久误报。

## `/version`（运行身份）

生产实例必须能回答「我现在跑的是哪一版」：

```bash
curl -s http://127.0.0.1:8000/version
```

```json
{"version": "0.11.0", "git_sha": "abc123def...", "build_time": "2026-08-20T12:00:00Z"}
```

- 来源：镜像构建时由 release pipeline 以 Docker build args 注入
  `LARK_LEDGER_VERSION` / `LARK_LEDGER_GIT_SHA` / `LARK_LEDGER_BUILD_TIME`，
  运行时**不调用 git**、不依赖容器内的 `.git`。
- 回退：version 回退到包内 `__version__`；git_sha 回退到 `"unknown"`。
- 只暴露这三个字段，不暴露任何环境变量 / 数据库 URL / secret。

## `/ops/status`（聚合状态）

受限聚合视图：backlog 计数 + worker 心跳 + build 身份，全部脱敏：

```bash
curl -s http://127.0.0.1:8000/ops/status
```

```json
{
  "status": "ok",
  "build": {"version": "0.11.0", "git_sha": "...", "build_time": "..."},
  "backlog": {
    "status": "ok",
    "events":  {"received": 0, "processing": 0, "failed": 0, "dead": 0, "pending": 0, "retry": 0, "total": 0},
    "outbox":  {"pending": 0, "sending": 0, "failed": 0, "dead": 0, "pending": 0, "retry": 0, "total": 0},
    "pending_commands": {"pending": 0, "executing": 0, "total": 0}
  },
  "workers": {
    "event_worker": {"started": true, "running": true, "last_sweep_at": "...", "last_success_at": "...", "last_error_at": null, "sweeps": 42, "processed": 17},
    "reply_worker": {"started": true, "running": true, "last_sweep_at": "..."},
    "cleanup_worker": {"status": "disabled"},
    "recurring_worker": {"status": "disabled"},
    "receiver": {"status": "disabled"}
  }
}
```

- `backlog.events.pending` = received；`retry` = failed；`dead` = dead。
- 所有计数来自 DB `GROUP BY status` 聚合（走 `(status, ...)` 索引），
  不把行读进 Python，不返回任何 payload / owner / ledger / user 维度。
- observability 自身失败时该 section 显示 `status: "unavailable"`，**不会**导致 HTTP 500。

## 日志与请求关联（request_id）

- 每个请求（含 `/healthz`、`/readyz`、webhook、全部 API）都会获得 `request_id`：
  - 客户端传入合法 `X-Request-ID`（≤128 字符，仅 `[A-Za-z0-9._-]`）时**复用**；
  - 非法或缺失时服务端生成；
  - 响应头 `X-Request-ID` 回显（所有路径）。
- 请求期间产生的所有日志行自动携带 `request_id=...`（`contextvars` + logging Filter），
  因此可以用一条日志串起 应用 → service → worker 的完整处理链路。
- 日志格式（应用启动后由 `setup_logging` 安装）：

```text
2026-08-20 12:00:01 INFO lark_ledger.services.worker request_id=ab12cd34ef56 event succeeded event_id=e_1 attempt=1
```

> 注意：`setup_logging` 幂等；单元测试直接驱动各 logger，不受影响。

## 隐私红线

禁止出现在日志 / `/ops/status` / `/readyz` / `/version` 中：

- API token、`Authorization` header、Cookie
- 飞书 App Secret、verification token、encrypt key
- 数据库密码 / 连接串
- credential digest、完整 prompt 中的敏感内容、用户私密财务原文

`/ops/status` 只暴露计数与时间戳；worker owner id（含主机名）不会出现在任何
observability 响应中。`logging_config.redact_sensitive()` 提供兜底脱敏。

## Troubleshooting

### 数据库不可用

```text
/readyz → 503, checks.database.reason = "database_unavailable"
```

排查：`docker compose logs app` 中最近的 `readiness database probe failed`；
确认 PostgreSQL 容器健康（`docker compose ps`、`pg_isready`）；恢复后 `/readyz`
自动回到 200。

### Migration mismatch

```text
/readyz → 503, checks.migration.reason = "migration_revision_mismatch"
checks.migration.current != checks.migration.expected
```

含义：数据库 schema 落后于代码期望的 Alembic head。处理：先备份
（见 [backup-restore.md](backup-restore.md)），再 `alembic upgrade head`，重启后复检。
**绝不**在 readiness 请求中自动迁移。

### Worker stale

```text
/readyz → 200, checks.event_worker.status = "warning", reason = "worker_stale"
```

含义：worker 任务进程还活着，但循环心跳超过阈值未推进（可能卡在 DB 调用）。
查看 `/ops/status` 的 `last_sweep_at` 距今多久。通常数据库连接恢复后自愈；
若持续 stale，重启应用容器（`docker compose restart app`）。

### Dead queue 增长

```text
/ops/status → backlog.events.dead > 0 或 backlog.outbox.dead > 0
```

dead 表示永久失败（payload / contract / 超出重试预算）。用 Web 后台的
事件重放工具（`docs/environment.md` 的管理员人工事件重放）或检查
`processed_events.last_error_code` 定位根因。dead 本身是 degraded 而非 not ready。

## Dead-letter 语义与处理（P44）

### 状态语义

- **pending** — 已持久化，等待 worker / 用户（`events.received`、`outbox.pending`、
  `pending_commands.pending`）。
- **retry** — 失败过一次，已按指数退避安排下次重试（`failed` 且
  `next_attempt_at` 在未来）。
- **dead** — 重试耗尽或永久失败，需要运维判断（`events.dead`、`outbox.dead`）。
- **resolved** — 运维人员确认不重放（`dead_letter_actions` 中的 `resolve` 审计标记；
  源行保留，不删除、不改状态）。

### 统一查询模型

受保护的运维 API（管理员登录后可访问）：

```text
GET  /api/web/v1/admin/dead-letters?source=&state=&reason=&retryable=&replay_safe=&created_from=&created_to=&page=&page_size=&sort=
GET  /api/web/v1/admin/dead-letters/{source}/{id}
POST /api/web/v1/admin/dead-letters/{source}/{id}/replay   {reason}
POST /api/web/v1/admin/dead-letters/{source}/{id}/resolve  {reason}
```

列表与详情**只返回脱敏摘要**：来源、状态、尝试次数、原因分类
（network / timeout / rate_limited / authentication / permission /
remote_not_found / remote_rejected / invalid_payload / serialization /
database / business_conflict / expired / unknown）、可重放性评估、payload 类型
摘要、脱敏后的单行错误摘要。不返回业务 payload、财务文本、token、cookie、
DB URL 或完整异常。

### Replay（什么时候可以重放）

- 原因分类属于 **network / timeout / rate_limited**（transient），且
  `replay_safe=true`（无 `remote_message_id`、无已提交业务结果）时：可以重放。
- replay 只把 `dead`/`failed` 重新入队（outbox → `pending`，事件 → `received`），
  由现有 worker 按正常租约路径投递；API 本身不执行任何远端副作用。
- 每个 replay 都写入 `dead_letter_actions` 审计（operator、reason、前后状态、
  request_id），并受 PostgreSQL 行锁保护：两个管理员同时重放同一记录只有一次
  有效状态迁移，另一方得到 409。

### Do not replay（什么时候不能重放）

- 原因分类为 **remote_rejected / remote_not_found / invalid_payload /
  serialization / expired**：terminal，重放必然再次失败。
- **authentication / permission / database / business_conflict / unknown**：
  需要人工审查，不允许一键重放。
- `replay_safe=false`（存在已记录的 `remote_message_id` 或业务结果已提交）默认
  禁止重放，避免重复副作用。

### 生产处理流程（inspect → classify → assess → replay/resolve → verify）

1. 查看 `/ops/status` 的 `backlog` 计数与 `oldest_*_at` 时间。
2. 打开 Dead Letters 页面（`/admin/dead-letters`），按 source / reason 过滤。
3. 打开单条详情，看原因分类与重放评估。
4. 判断：transient 且安全 → `replay`（填写原因）；terminal → `resolve`
   （记录审计，不删除源行）。
5. 重放后确认 `/ops/status` 中对应 pending 计数变化、worker 心跳正常。
6. 检查业务副作用（账本是否出现预期记录）。

### 历史清理

**绝不直接 DELETE dead 行让计数归零。** dead-letter 是有审计价值的运维资产；
清理交给 Cleanup Worker 的 retention 窗口（`outbox_dead_retention_days`，默认 90 天），
人工只通过 `resolve` 记录处理结论。

### Outbox 积压

```text
/ops/status → backlog.outbox.pending / failed 持续增长
```

reply worker 可能停止或卡住。对照 `/ops/status` 的 `workers.reply_worker`
心跳；若 worker 正常但积压，通常是飞书侧限流（429），retry 会在指数退避后消化。

## Graceful Shutdown（P42）

应用收到 `SIGTERM`（`docker compose stop`、`docker stop`、`docker compose down`）时按固定顺序收尾：

1. 标记 `shutting_down`（`/readyz` 立即返回 503，不再承接新流量）；
2. 停止 WebSocket receiver（不再接受新事件）；
3. 停止 Event / Reply / Cleanup / Recurring worker（`stop()` 先设停止标志、再 cancel 循环任务并等待其退出——in-flight 事件的 lease 保留，崩溃窗口由其它实例在 lease 过期后接管，绝不丢事件）；
4. 关闭数据库连接池（`engine.dispose()`）。

Compose 已配置 `stop_grace_period: 60s`，给在途业务提交与 lease 清理留足时间；超过宽限期 Docker 才发 `SIGKILL`。停止期间留下的 `processing` 行带有效 lease，重启/其它实例会在 lease 过期后继续处理（尊重现有 lease/retry 设计，不额外重构）。

## 未来指标兼容

内部状态模型已按「未来 Prometheus 化」约束命名：

```text
worker_last_success_timestamp / worker_last_error_timestamp
outbox_pending / events_retry / events_dead
```

所有计数为**有界基数**：无 `user_id` / `ledger_id` / `request_id` label，
未来接入 Prometheus 不会发生 label cardinality 爆炸。
