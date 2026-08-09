import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { ArrowDownRight, ArrowUpRight, Clock3, WalletCards } from "lucide-react";
import { api, localTime, money, type AssetSummary, type DashboardData } from "../api";

export function DashboardPage() {
  const query = useQuery({ queryKey: ["dashboard"], queryFn: () => api<DashboardData>("/dashboard") });
  const assets = useQuery({ queryKey: ["assets"], queryFn: () => api<AssetSummary>("/assets") });
  if (query.isLoading) return <div className="page-skeleton"><div /><div /><div /></div>;
  if (query.isError) return <section className="state-panel"><h2>总览暂时不可用</h2><button onClick={() => query.refetch()}>重新加载</button></section>;
  const data = query.data!;
  const peak = Math.max(...data.trend.flatMap((point) => [Number(point.income), Number(point.expense)]), 1);
  return (
    <div className="dashboard-page">
      <div className="page-heading"><div><p className="eyebrow">财务总览</p><h2>本月，保持清晰。</h2></div><div className="heading-actions"><Link className="quiet-button" to="/transfers">转账记录</Link><Link className="quiet-button" to="/accounts">管理账户</Link><Link className="quiet-button" to="/entries">查看全部账目</Link></div></div>
      <section className="metric-grid">
        <article><span><ArrowDownRight size={17} /> 本月支出</span><strong>{money(data.month_expense)}</strong></article>
        <article><span><ArrowUpRight size={17} /> 本月收入</span><strong>{money(data.month_income)}</strong></article>
        <article><span><WalletCards size={17} /> 本月结余</span><strong>{money(data.month_balance)}</strong></article>
        <article><span><WalletCards size={17} /> 预算使用率</span><strong>{data.budget_usage_rate === null ? "未设置" : `${Number(data.budget_usage_rate).toFixed(1)}%`}</strong></article>
        <article><span><Clock3 size={17} /> 待确认</span><strong>{data.pending_count} 笔</strong></article>
      </section>
      {assets.data?.accounts && <>
        <section className="metric-grid asset-metrics">
          <article><span>总资产</span><strong className="positive">{money(assets.data.total_assets)}</strong></article>
          <article><span>总负债</span><strong>{money(assets.data.total_liabilities)}</strong></article>
          <article><span>净资产</span><strong className={Number(assets.data.net_assets) >= 0 ? "positive" : ""}>{money(assets.data.net_assets)}</strong></article>
        </section>
        <section className="panel account-balances"><div className="panel-title"><h3>账户余额</h3><span>{assets.data.currency}</span></div><div className="category-list">{assets.data.accounts.map((account) => <div key={account.account_id}><span>{account.account_name}{account.archived ? "（已归档）" : ""}</span><b>{money(account.current_balance)}</b><em>{account.account_type === "liability" ? "负债" : "资产"}</em></div>)}</div></section>
      </>}
      <div className="dashboard-grid">
        <section className="panel trend-panel"><div className="panel-title"><h3>近 30 天趋势</h3><span>收入 / 支出</span></div><div className="mini-chart" aria-label="近 30 天收支趋势">{data.trend.map((point) => <div className="chart-day" key={point.period} title={`${point.period} 收入 ${money(point.income)} 支出 ${money(point.expense)}`}><i className="income" style={{ height: `${Math.max(Number(point.income) / peak * 100, 2)}%` }} /><i className="expense" style={{ height: `${Math.max(Number(point.expense) / peak * 100, 2)}%` }} /></div>)}</div></section>
        <section className="panel"><div className="panel-title"><h3>本月分类</h3><span>支出占比</span></div>{data.categories.length ? <div className="category-list">{data.categories.map((item) => <Link key={item.category} to={`/entries?category=${encodeURIComponent(item.category)}`}><span>{item.category}</span><b>{money(item.amount)}</b><em>{Number(item.ratio).toFixed(1)}%</em></Link>)}</div> : <p className="muted-empty">本月还没有支出</p>}</section>
      </div>
      <section className="panel recent-panel"><div className="panel-title"><h3>最近账目</h3><span>{data.recent_entries.length} 笔</span></div>{data.recent_entries.length ? <div className="recent-list">{data.recent_entries.map((entry) => <Link key={entry.id} to={`/entries?entry=${entry.short_id}`}><code>#{entry.short_id}</code><span><b>{entry.category}</b><small>{entry.note || "无备注"} · {localTime(entry.occurred_at)}</small></span><strong className={entry.direction === "INCOME" ? "positive" : ""}>{entry.direction === "INCOME" ? "+" : "-"}{money(entry.amount)}</strong></Link>)}</div> : <div className="empty-ledger"><h3>还没有账目</h3><p>去飞书对飞账说：“午饭32元”</p></div>}</section>
    </div>
  );
}
