import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { AlertTriangle, ArrowDownRight, ArrowUpRight, BellRing, CalendarClock, Users, WalletCards } from "lucide-react";
import { api, localTime, money, type HouseholdOverview, type Insight, type InsightList } from "../api";

export function OverviewPage() {
  const query = useQuery({ queryKey: ["overview"], queryFn: () => api<HouseholdOverview>("/overview") });
  const insights = useQuery({
    queryKey: ["insights"],
    queryFn: () => api<InsightList>("/insights?limit=5"),
    enabled: query.isSuccess,
  });
  if (query.isLoading) return <div className="page-skeleton"><div /><div /><div /></div>;
  if (query.isError) return <section className="state-panel"><h2>概览暂时不可用</h2><button onClick={() => query.refetch()}>重新加载</button></section>;
  const data = query.data!;
  const isHousehold = data.ledger_kind === "household_shared";
  const budgetPct = data.budget.usage_rate === null ? null : Number(data.budget.usage_rate);
  const insightItems: Insight[] = insights.data?.insights ?? [];

  return (
    <div className="dashboard-page">
      <div className="page-heading">
        <div><p className="eyebrow">账本概览</p><h2>{data.ledger_name} · {data.period}</h2></div>
        <div className="heading-actions">
          <Link className="quiet-button" to="/entries">查看全部账目</Link>
          <Link className="quiet-button" to="/recurring">周期账单</Link>
          {isHousehold && <Link className="quiet-button" to="/households">家庭成员</Link>}
        </div>
      </div>

      <section className="panel insight-panel">
        <div className="panel-title"><h3>值得关注</h3><BellRing size={16} /></div>
        {insightItems.length ? (
          <div className="insight-list">
            {insightItems.map((item) => (
              <div key={item.key} className={`insight-row ${item.severity}`}>
                {item.severity === "warning" && <AlertTriangle size={15} />}
                <p>{item.summary}</p>
                {item.related_goal_name && <Link className="quiet-link" to="/goals">查看目标</Link>}
              </div>
            ))}
          </div>
        ) : (
          <p className="muted-empty">目前没有需要特别关注的变化</p>
        )}
      </section>

      <section className="metric-grid">
        <article><span><ArrowDownRight size={17} /> 本月支出</span><strong>{money(data.expense_total)}</strong></article>
        <article><span><ArrowUpRight size={17} /> 本月收入</span><strong>{money(data.income_total)}</strong></article>
        <article><span><WalletCards size={17} /> 本月结余</span><strong className={Number(data.net_total) >= 0 ? "positive" : ""}>{money(data.net_total)}</strong></article>
        <article><span><CalendarClock size={17} /> 预算使用率</span><strong>{budgetPct === null ? "未设置" : `${budgetPct.toFixed(1)}%`}</strong></article>
      </section>

      <section className="metric-grid asset-metrics">
        <article><span>总资产</span><strong className="positive">{money(data.account_balance_summary.total_assets)}</strong></article>
        <article><span>总负债</span><strong>{money(data.account_balance_summary.total_liabilities)}</strong></article>
        <article><span>净资产</span><strong className={Number(data.account_balance_summary.net_assets) >= 0 ? "positive" : ""}>{money(data.account_balance_summary.net_assets)}</strong></article>
      </section>

      <div className="dashboard-grid">
        {data.budget.total_budget !== null && (
          <section className="panel">
            <div className="panel-title"><h3>预算进度</h3><span>{money(data.budget.total_spent)} / {money(data.budget.total_budget)}</span></div>
            <div className="budget-track" aria-label="预算使用率">
              <i className={budgetPct !== null && budgetPct > 100 ? "over" : ""} style={{ width: `${Math.min(budgetPct ?? 0, 100)}%` }} />
            </div>
            <p className="muted-empty">{data.budget.total_remaining !== null ? `剩余 ${money(data.budget.total_remaining)}` : "无剩余额度"}</p>
          </section>
        )}

        {isHousehold && (
          <section className="panel">
            <div className="panel-title"><h3>成员支出</h3><Users size={16} /></div>
            <div className="category-list">
              {data.member_contributions.map((member) => (
                <div key={member.user_id}>
                  <span>{member.alias || member.display_name || "成员"}</span>
                  <b>{money(member.expense_total)}</b>
                  <em>{member.role === "owner" ? "所有者" : "成员"}</em>
                </div>
              ))}
            </div>
          </section>
        )}

        <section className="panel">
          <div className="panel-title"><h3>主要分类</h3><span>支出占比</span></div>
          {data.top_categories.length ? <div className="category-list">{data.top_categories.map((item) => <Link key={item.category} to={`/entries?category=${encodeURIComponent(item.category)}`}><span>{item.category}</span><b>{money(item.amount)}</b><em>{Number(item.ratio).toFixed(1)}%</em></Link>)}</div> : <p className="muted-empty">本月还没有支出</p>}
        </section>

        <section className="panel">
          <div className="panel-title"><h3>未来周期支出</h3><span>最近 {data.upcoming_recurring.length} 项</span></div>
          {data.upcoming_recurring.length ? <div className="category-list">{data.upcoming_recurring.map((item) => <div key={item.rule_id}><span>{item.description || item.category}</span><b>{money(item.amount)}</b><em>{item.next_occurrence}</em></div>)}</div> : <p className="muted-empty">没有即将到期的周期账单</p>}
        </section>
      </div>

      <section className="panel recent-panel">
        <div className="panel-title"><h3>最近交易</h3><span>{data.recent_transactions.length} 笔</span></div>
        {data.recent_transactions.length ? <div className="recent-list">{data.recent_transactions.map((entry) => <Link key={entry.id} to={`/entries?entry=${entry.short_id}`}><code>#{entry.short_id}</code><span><b>{entry.category}{entry.payer_name ? ` · ${entry.payer_name}` : ""}</b><small>{entry.note || "无备注"} · {localTime(entry.occurred_at)}</small></span><strong className={entry.direction === "INCOME" ? "positive" : ""}>{entry.direction === "INCOME" ? "+" : "-"}{money(entry.amount)}</strong></Link>)}</div> : <div className="empty-ledger"><h3>还没有账目</h3><p>去飞书对飞账说：“午饭32元”</p></div>}
      </section>
    </div>
  );
}
