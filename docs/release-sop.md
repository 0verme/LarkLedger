# LarkLedger 发布 SOP（Release SOP）

本文档定义标准版本发布流程。从 **v0.11.0 起**，GitHub Release 由 CI 自动创建，正常情况下**不再需要人工执行 `gh release create`**。

## 标准发布流程

1. **本地门禁全绿**：`ruff check .`、`mypy src`、`pytest --cov`（必要时含 PostgreSQL 集成测试）、前端 `npm run lint && npm run typecheck && npm test && npm run build`、文档链接检查。
2. **Release commit**：提交 `chore(release): prepare vX.Y.Z`，同步更新：
   - `pyproject.toml`、`src/lark_ledger/__init__.py`、`web/package.json` 的版本号（三处必须一致）；
   - `CHANGELOG.md` 顶部新增 `## [X.Y.Z] - YYYY-MM-DD` section（不得为空）；
   - `.github/release-notes/vX.Y.Z.md`（可选但推荐——自动化优先使用该文件作为 Release Notes，缺失时自动回退到 CHANGELOG section）。
3. **push main**：`git push origin main`。
4. **创建 annotated tag**：

   ```bash
   git tag -a vX.Y.Z -m "LarkLedger vX.Y.Z"
   git push origin vX.Y.Z
   ```

   > **必须使用 annotated tag**（`git tag -a`）。push lightweight tag 会让 Release workflow 的 annotated-tag guard 直接 FAIL。
5. **GitHub Actions 自动执行**（按依赖顺序，任一 guard 失败则后续 job 不运行）：

   ```text
   push tag
      ↓
   validate  → annotated tag guard / tag↔version 一致性 / CHANGELOG+Release Notes guard
      ↓
   image     → build multi-platform (linux/amd64, linux/arm64) + push GHCR（X.Y.Z / X.Y / latest）
      ↓
   release   → 生成 Release Notes → 创建 GitHub Release（非 draft、非 prerelease）
      ↓
   verify    → 输出发布摘要（tag / version / GHCR image / Release 状态）
   ```

6. **verify**：确认 GitHub Actions 全部 job 绿色；`gh release view vX.Y.Z` 与 `https://github.com/0verme/LarkLedger/releases/tag/vX.Y.Z` 可访问；GHCR 镜像 digest 与 Release 关联一致。
7. **NAS deployment**：按 `docs/environment.md` 部署并完成生产验收。
8. 未决问题（如 CHANGELOG section 缺失、版本不一致、轻量 tag）直接在 workflow 中失败，**不要绕过 guard 手工发布**。

## 自动化行为细节

- **Release 幂等**：workflow 可重跑。若 Release 已存在，会校验 `tag_name`、`draft=false`、`prerelease=false` 后视为 PASS，**不会**重复创建、删除或覆盖既有正式 Release。
- **Guards**（任一失败 → workflow FAIL）：
  - annotated tag：`git cat-file -e refs/tags/<tag>^{tag}`；
  - tag ↔ 版本一致性：`pyproject.toml` / `src/lark_ledger/__init__.py` / `web/package.json` 三处版本必须等于 tag 去掉 `v` 前缀；
  - CHANGELOG：必须存在 `## [X.Y.Z]` 非空 section；
  - Release Notes 非空：优先 `.github/release-notes/vX.Y.Z.md`，回退 CHANGELOG section，两者皆无 → FAIL（绝不发布空白 Release）。
- **执行顺序**：`validate → image → release`。镜像构建/推送失败时不会创建 GitHub Release，避免「Release 已发布但镜像缺失」的半发布状态。
- **并发**：同一 tag 的重复触发会串行化（`concurrency`），不会取消正在进行的镜像发布。

## Rollback（回滚）

### A. Code-only rollback（数据库 schema 未发生不兼容变化）

适用：新版本代码有问题，但数据库 schema 没有不兼容变更（通常是 bugfix 或
功能回退，migration 仍是同一 head）。

```bash
# 1. 确认当前库 revision 与目标旧镜像兼容（两版本 head 相同即 code-only）
LARK_LEDGER_DATABASE_URL='postgresql+asyncpg://...' alembic current

# 2. 切换到 previous GHCR image
docker compose -f compose.image.yaml down
LARK_LEDGER_IMAGE_TAG=<上一版本，如 0.10.0> docker compose -f compose.image.yaml up -d

# 3. 健康检查与 smoke test
curl -f http://127.0.0.1:8000/healthz
curl -f http://127.0.0.1:8000/readyz      # 必须 200，且 migration current
# smoke test：一笔账可写可查、/ops/status 正常
```

### B. Schema-changing rollback（数据库 schema 已变化）

> **镜像 rollback 和数据库 rollback 是两个不同操作。** `docker pull old image`
> 不能安全回滚数据库——旧镜像通常无法理解新 schema。

1. **判断 migration backward compatibility**：
   - 先确认当前 head 与回滚目标的 head。若回滚目标的代码版本**不认识**
     当前 schema 的新列/新表（例如旧的 `SELECT` 引用不存在的列），必须先回滚数据库。
   - 查看 `alembic/versions/` 中新增迁移是否提供了 `downgrade()`。
2. **Backup restore（推荐路径）**：按 [backup-restore.md](backup-restore.md) 的
   Restore 流程恢复到发布前的备份，再启动旧镜像。恢复前必须「备份现状」。
3. **Alembic downgrade 的适用边界**：
   - `alembic downgrade <旧head>` 只适用于**提供完整可逆 `downgrade()`** 的迁移
     （例如新增可空列、新索引、可删除的表）；
   - **destructive migration（删表 / 删列 / 改约束）没有安全的自动 downgrade**，
     此时唯一的恢复路径是 backup restore；
   - 升级后用户已写入的新数据在 downgrade 中**可能丢失**——回滚前先备份，
     并明确告知用户影响范围。
4. **验收**：恢复/降级后执行 启动 → `/healthz` → `/readyz`（migration current）
   → 关键业务 smoke test → 观察 `/ops/status` 无异常积压。

> 本仓库不宣传「自动无损 rollback」。任何 schema 变更都应在发布前做
> restore drill（见 backup-restore.md）验证可回退路径。

## Emergency Fallback（仅在自动化故障时）

正常情况下不需要人工干预。若 workflow 因平台故障无法创建 Release，可按以下命令手工兜底：

```bash
gh release create vX.Y.Z \
  --verify-tag \
  --title "LarkLedger vX.Y.Z" \
  --notes-file .github/release-notes/vX.Y.Z.md
```

兜底前必须确认：镜像已成功 push GHCR、版本与 CHANGELOG 一致、Release 尚不存在（`gh release view vX.Y.Z` 返回非零）。

## 禁止事项

- 不得修改、删除、重建或移动已发布的 tag / Release / GHCR 镜像（含 v0.10.0）。
- 不得仅为了测试 workflow 创建正式版本 tag（可用本地临时 tag 做脚本级测试，测完删除，禁止 push）。
- 发布必须是正式 Release：`draft=false`、`prerelease=false`。

## Regression：v0.10.0 为何出现 Release 404

v0.10.0 发布时曾出现：tag 与 GHCR 镜像均存在，但 `/releases/tag/v0.10.0` 返回 404。

**根因**：当时 `release.yml` 只执行 build/push GHCR 镜像，没有创建 GitHub Release 的步骤，GitHub Release 依赖人工创建，本次被遗漏。

**修复**：P41 为 release workflow 增加自动化 Release publishing（validate → image → release 三段式 + 全部 guard），未来版本 push annotated tag 后自动闭环，不再依赖人工创建。

## Dead-letter incident handling（P44）

当 `/ops/status` 显示 `backlog.*.dead > 0` 或收到 dead 增长告警时，按以下
runbook 处理。**先只读诊断，再判断，最后才执行动作。**

1. **查看聚合状态**：`curl http://<host>/ops/status`，记录 `backlog` 的
   `events` / `outbox` / `pending_commands` 计数与 `oldest_*_at` 时间。
2. **查看受保护详情**：以管理员登录 Web 后台，打开
   Operations → Dead Letters（`/admin/dead-letters`），或直接调用
   `GET /api/web/v1/admin/dead-letters?source=outbox&state=dead`。
3. **判断原因分类**：`reason_category` 是 bounded 分类
   （network / timeout / rate_limited / remote_rejected / …），不是 raw
   exception；错误摘要已脱敏。
4. **判断重放安全**：看 `retryable` / `replay_safe` / `requires_manual_review` /
   `terminal`。只有 transient 且 `replay_safe=true` 的项才能重放。
5. **replay / resolve**：transient 安全 → `POST .../replay {reason}`（API 只
   重新入队，worker 负责投递）；terminal 或不应重放 → `POST .../resolve
   {reason}`（审计标记，不删行）。
6. **检查 worker**：确认 `/ops/status` 的 `workers.event_worker` /
   `workers.reply_worker` 心跳正常、非 stale。
7. **检查 backlog**：重放后 pending 应短暂上升后回落；若持续积压，查
   `workers.reply_worker` 与飞书限流（429）。
8. **检查业务副作用**：确认账本出现预期记录且无重复（重放前先核对
   `replay_safe` 与审计历史；存在 `remote_message_id` 时禁止重放）。
9. **记录结果**：所有 replay / resolve 都自动写入 `dead_letter_actions`
   （operator、reason、前后状态、request_id），无需额外登记。

**禁止**：直接 `DELETE` / `UPDATE` dead 行、手工清 `attempts`、批量重放
所有 dead、为了计数归零而清数据。历史清理交给 Cleanup Worker 的 retention
窗口（`outbox_dead_retention_days` / `event_dead_retention_days`）。
