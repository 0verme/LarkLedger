import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Check, ChevronLeft, ChevronRight, Clock3, Loader2, X } from "lucide-react";
import { useSearchParams } from "react-router-dom";
import { api, localTime, money, type PendingActionResponse, type PendingDetail, type PendingPage as PendingPageData } from "../api";
import { EmptyState, TableSkeleton } from "../components/States";

const tabs = [
  { value: "pending", label: "待处理" },
  { value: "completed", label: "已完成" },
  { value: "closed", label: "已取消 / 已过期" },
] as const;

const statusText: Record<string, string> = {
  pending: "待处理", executing: "处理中", executed: "已完成",
  cancelled: "已取消", expired: "已过期", failed: "处理失败",
};

export function PendingPage() {
  const [params, setParams] = useSearchParams();
  const client = useQueryClient();
  const group = tabs.some((tab) => tab.value === params.get("tab")) ? params.get("tab")! : "pending";
  const page = Number(params.get("page") ?? "1");
  const selected = params.get("confirmation");
  const [decision, setDecision] = useState<"confirm" | "cancel" | null>(null);
  const [notice, setNotice] = useState("");
  const list = useQuery({
    queryKey: ["pending", group, page],
    queryFn: () => api<PendingPageData>(`/pending?group=${group}&page=${page}&page_size=20`),
  });
  const detail = useQuery({
    queryKey: ["pending-detail", selected],
    queryFn: () => api<PendingDetail>(`/pending/${encodeURIComponent(selected!)}`),
    enabled: Boolean(selected),
  });
  const action = useMutation({
    mutationFn: (choice: "confirm" | "cancel") => api<PendingActionResponse>(`/pending/${encodeURIComponent(selected!)}/${choice}`, { method: "POST" }),
    onSuccess: async (result) => {
      setDecision(null);
      setNotice(result.message);
      client.setQueryData(["pending-detail", selected], result.pending);
      await Promise.all([client.invalidateQueries({ queryKey: ["pending"] }), client.invalidateQueries({ queryKey: ["dashboard"] })]);
      window.setTimeout(() => setNotice(""), 2800);
    },
  });
  const update = (key: string, value: string) => {
    const next = new URLSearchParams(params);
    if (value) next.set(key, value); else next.delete(key);
    if (key === "tab") next.set("page", "1");
    setParams(next);
  };
  const close = () => update("confirmation", "");
  const current = detail.data?.pending;
  const preview = detail.data?.preview;

  return <div className="pending-page">
    {notice && <div className="toast">{notice}</div>}
    <div className="page-heading"><div><p className="eyebrow">高风险确认</p><h2>先看清，再入账。</h2></div><span className="result-count">共 {list.data?.total ?? 0} 项</span></div>
    <div className="tab-list" role="tablist">{tabs.map((tab) => <button key={tab.value} role="tab" aria-selected={group === tab.value} onClick={() => update("tab", tab.value)}>{tab.label}</button>)}</div>
    <section className="pending-panel">
      {list.isLoading ? <TableSkeleton rows={4} /> : list.isError ? <div className="state-panel"><h3>确认单加载失败</h3><button onClick={() => list.refetch()}>重试</button></div> : !list.data?.items.length ? <EmptyState icon={<Clock3 size={28} />} title={group === "pending" ? "当前没有待确认事项" : "这里还没有记录"} description="图片、语音、批量或疑似重复记账会出现在这里。" /> : <div className="pending-list">{list.data.items.map((item) => <button key={item.confirmation_id} onClick={() => update("confirmation", item.confirmation_id)}><div><code>{item.confirmation_id}</code><span className={`pending-status ${item.status}`}>{statusText[item.status] ?? item.status}</span></div><strong>{item.risk_reason}</strong><p>{item.entries_total} 笔 · 支出 {money(item.expense_total, item.currency)} · 收入 {money(item.income_total, item.currency)}</p><small>{localTime(item.created_at)} · {item.source_type}</small></button>)}</div>}
    </section>
    {list.data && list.data.pages > 1 && <div className="pagination"><button disabled={page <= 1} onClick={() => update("page", String(page - 1))}><ChevronLeft size={16} /> 上一页</button><span>{page} / {list.data.pages}</span><button disabled={page >= list.data.pages} onClick={() => update("page", String(page + 1))}>下一页 <ChevronRight size={16} /></button></div>}
    {selected && <><button className="drawer-scrim" onClick={close} aria-label="关闭确认单详情" /><aside className="entry-drawer pending-drawer"><button className="drawer-close" onClick={close} aria-label="关闭"><X /></button>{detail.isLoading ? <div className="drawer-loading"><Loader2 className="spin" size={16} /> 加载确认单…</div> : detail.isError || !current || !preview ? <div className="state-panel"><h3>确认单不存在</h3></div> : <><div className="drawer-title"><code>{current.confirmation_id}</code><span className={`pending-status ${current.status}`}>{statusText[current.status] ?? current.status}</span><h2>{current.risk_reason}</h2><p>{current.entries_total} 笔 · {current.source_type} · {current.transport}</p></div><dl className="detail-grid"><div><dt>创建时间</dt><dd>{localTime(current.created_at)}</dd></div><div><dt>过期时间</dt><dd>{localTime(current.expires_at)}</dd></div><div><dt>支出合计</dt><dd>{money(current.expense_total, current.currency)}</dd></div><div><dt>收入合计</dt><dd>{money(current.income_total, current.currency)}</dd></div></dl>{preview.anomalies.length > 0 && <div className="anomaly-box"><AlertTriangle size={17} />{preview.anomalies.map((item) => <p key={item}>{item}</p>)}</div>}<section className="preview-section"><h3>冻结预览</h3>{preview.items.map((item, index) => <article key={`${item.index}-${index}`}><span>{index + 1}</span><div><strong>{item.direction === "expense" ? "支出" : "收入"} {money(item.amount, item.currency)}</strong><p>{item.category || "未分类"} · {item.occurred_at}</p><small>{item.note || "无备注"}{item.duplicate_of ? ` · 疑似重复 ${item.duplicate_of}` : ""}</small></div></article>)}{preview.budgets.map((budget) => <article key={budget.category}><span>预</span><div><strong>{budget.category} {money(budget.amount, budget.currency)}</strong><p>分类预算</p></div></article>)}</section>{current.status === "pending" && <div className="drawer-actions"><button className="primary-small" onClick={() => setDecision("confirm")}><Check size={16} /> 确认执行</button><button className="danger" onClick={() => setDecision("cancel")}><X size={16} /> 取消</button></div>}</>}</aside></>}
    {decision && current && <div className="modal-layer"><div className="confirm-dialog"><h3>{decision === "confirm" ? `确认执行 ${current.confirmation_id}？` : `取消 ${current.confirmation_id}？`}</h3><p>{decision === "confirm" ? "将严格执行已冻结的预览，不会重新调用 AI。" : "取消后不会写入账本，且无法再次确认。"}</p>{action.error && <p className="form-error">{action.error.message}</p>}<div><button onClick={() => setDecision(null)}>返回</button><button className={decision === "confirm" ? "primary-small" : "danger-solid"} disabled={action.isPending} onClick={() => action.mutate(decision)}>{decision === "confirm" ? "确认执行" : "确认取消"}</button></div></div></div>}
  </div>;
}
