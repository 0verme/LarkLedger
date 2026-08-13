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
