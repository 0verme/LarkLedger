# LarkLedger 产品演进路线

路线遵循“先建设共同地基，再用家庭场景验证，最后按领域和客户端分叉”的原则。
远期阶段是方向和决策门槛，不是发布日期承诺。

## 共同主干

1. **Identity & Ledger Foundation（已完成）**：内部 User、ChannelIdentity、Ledger、
   RequestContext 和无损旧数据迁移。
2. **个人多账本（已完成）**：创建、列出、切换默认账本；短 ID、预算、统计、Pending 和 revision
   全部按 Ledger 隔离。
3. **家庭空间 MVP（已完成）**：Household、成员、邀请与家庭公共账本；个人账本默认不挂载、不共享，公共账本按查询无复制汇总。
4. **统一 Client API（已完成）**：飞书与 Web 共用 `ClientApplicationService` 命令/查询边界；提供 `/api/client/v1`、可撤销 Bearer、持久化幂等快照与稳定错误契约。具体 ESP32、Telegram、微信客户端仍按后续验证顺序实施。
5. **P26 Account Domain（已完成）**：建立账本范围内的现金、资产和负债账户；历史账目无损绑定默认账户，Web / Client API 提供账户生命周期能力，旧记账入口保持默认账户兼容。
6. **P27 Transfer & Balance（已完成）**：独立 Transfer 事实与 append-only audit 支持同账本账户转账、撤销和 revision；余额由 opening balance、有效收支与有效转账统一派生，提供账户余额、总资产、总负债和净资产视图。转账不进入收入、支出、预算或分类消费统计。
7. **P28 Budget 2.0（已完成）**：账本范围的月度总预算与分类预算以显式月份为周期，计划 vs 实际、剩余、使用率与 `normal / warning / exceeded / none` 状态由统一 `BudgetService` 从实时账目事实派生；`category_budgets` 保留为月度默认预算，周期预算在当月覆盖它。预算只统计支出，转账永不进入预算，删除 / 恢复 / 金额与分类修订均按当前有效事实重算。Web 按月查看与设置总 / 分类预算，飞书支持设置本月总预算与查看预算进度。
8. **P29 Recurring Rules（已完成）**：`recurring_rules` + `recurring_occurrences` 表达已知未来周期性收支，`monthly / yearly / weekly` 调度带锚定日，月末钳制不漂移；到期由 Recurring Worker 幂等生成一个确认 Pending（冻结账本 / 账户 / 金额 / 分类 / 计划日期）与飞书提醒卡片，只有确认后才正式入账。`(rule_id, occurrence_date)` 唯一约束是幂等权威，支持暂停 / 恢复 / 跳过 / 停用，修改只影响未来周期；已确认交易幂等且只计入确认后预算。Web 周期账单页与飞书确定性命令均可管理规则。
9. **P30 Household Contribution（已完成）**：账目区分「谁记账」与「谁付钱」（`created_by_user_id` / `paid_by_user_id`，后向兼容 `user_open_id`）；付款人按别名 > 显示名 > open_id > UUID 确定性解析；周期规则冻结付款人、家庭成员可跨确认；成员支出按付款人聚合且排除转账；Web 提供成员别名管理与成员统计。
10. **P31 Household Overview（已完成）**：一个确定性的「家庭首页」视图（`HouseholdOverviewService.overview`）：本月收支 / 预算进度 / 成员支出 / 主要分类 / 未来周期支出 / 最近交易 / 账户余额，全部由后端一次性确定性计算，前端不拼接多端点；`GET /api/web/v1/overview` 与飞书 `概览 / 家庭概览 / 家庭开销` 同源；为 P32 预留 `privacy_filter` 钩子。
11. **P32 Account Privacy（已完成）**：账户级 `visibility`（`shared` / `private`）+ `owner_user_id`；`PrivacyService` 在账户、账目、预算、转账、周期、待确认、总览与成员统计全线落实统一可见性谓词（个人账本为零操作）；Web 账户页显示「共享 / 私人」徽标并可切换；迁移 0025 带 CHECK 约束，降级拒绝存在 private 账户。
12. **P33 Goals & Insights（已完成）**：`financial_goals` + `goal_account_bindings`（迁移 0026）表达储蓄目标；进度由 `GoalProgressService` 在查询时从绑定账户实时余额确定性派生，目标不保存 `current_amount`、不维护第二套余额，记账 / 删账 / 恢复 / 转账自动重算；目标可见性继承绑定账户（引用 private 账户的目标对他人不可见）；确定性 `InsightService` 只输出四类洞察（支出变化 / 预算风险 / 未来周期支出 / 目标进度），全部规则集中在 `InsightPolicy` 阈值，数据不足返回 `[]` 不制造噪音；AI 仅可选改写结构化洞察文案并自动回退确定性摘要；Web `/goals` 与 `/insights`、飞书 `我的目标 / 目标 / 查看目标` 与 `洞察 / 财务洞察 / 本月洞察` 同源，私人数据不通过任何洞察侧信道泄漏。

## 分叉路线

- **个人与家庭主线**：预算、周期账单、账户与资产、家庭目标、**财务目标与确定性洞察（已完成）**；账户级隐私已完成（更细粒度如字段级 ACL 不在承诺内）。
- **一人公司领域**：独立的科目、期间、凭证和复式分录模型；只复用身份、权限、
  Worker、Outbox、幂等和审计基础设施，不把会计字段加入个人收支表。
- **客户端支线**：统一 API 后依次验证 ESP32、Telegram 等入口，微信放在多客户端
  边界已经稳定之后。
- **发行方式**：当前维持单仓库、单主分支和单版本线；只有企业域形成独立用户、依赖、
  团队或许可证需求时才考虑 Edition、插件或拆仓。

## 决策门槛

- 个人多账本完成且现有功能无回归，才启动家庭空间。
- Client API 与设备认证稳定，才让 ESP32 接触正式账务数据。
- 家庭主线稳定（v0.7.0 已含共享记账 / 付款人归属 / 家庭总览 / 账户级隐私），并有真实企业样本和会计复核者，才启动一人公司复式记账。
- 实际出现独立发布节奏或工程成本，才讨论模块发行或拆仓。

任何阶段都必须保持现有数据无损、确定性权限、AI 不决定授权，以及 Event/Outbox
可靠性语义不倒退。
