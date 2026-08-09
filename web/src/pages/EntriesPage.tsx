import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ChevronLeft, ChevronRight, Pencil, Plus, RotateCcw, Search, Trash2, X } from "lucide-react";
import { useSearchParams } from "react-router-dom";
import { api, localTime, money, type AccountList, type EntryDetail, type EntryPage } from "../api";

function useDebounced(value: string, delay = 300) {
  const [result, setResult] = useState(value);
  useEffect(() => {
    const timer = window.setTimeout(() => setResult(value), delay);
    return () => window.clearTimeout(timer);
  }, [value, delay]);
  return result;
}

function dateQuery(value: string, end = false) {
  if (!value) return "";
  const date = new Date(`${value}T00:00:00`);
  if (end) date.setDate(date.getDate() + 1);
  return date.toISOString();
}

export function EntriesPage() {
  const [params, setParams] = useSearchParams();
  const client = useQueryClient();
  const [search, setSearch] = useState(params.get("search") ?? "");
  const [editing, setEditing] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [creating, setCreating] = useState(false);
  const [notice, setNotice] = useState("");
  const debounced = useDebounced(search);
  const accounts = useQuery({ queryKey: ["accounts"], queryFn: () => api<AccountList>("/accounts") });

  useEffect(() => {
    if ((params.get("search") ?? "") === debounced) return;
    setParams((current) => {
      const next = new URLSearchParams(current);
      if (debounced) next.set("search", debounced);
      else next.delete("search");
      next.set("page", "1");
      return next;
    }, { replace: true });
  }, [debounced, params, setParams]);

  const selected = params.get("entry");
  const queryString = useMemo(() => {
    const next = new URLSearchParams(params);
    next.delete("entry");
    if (!next.has("page_size")) next.set("page_size", "25");
    return next.toString();
  }, [params]);
  const entries = useQuery({
    queryKey: ["entries", queryString],
    queryFn: () => api<EntryPage>(`/entries?${queryString}`),
  });
  const detail = useQuery({
    queryKey: ["entry", selected],
    queryFn: () => api<EntryDetail>(`/entries/${selected}`),
    enabled: Boolean(selected),
  });
  const refresh = async () => {
    await Promise.all([
      client.invalidateQueries({ queryKey: ["entries"] }),
      client.invalidateQueries({ queryKey: ["entry", selected] }),
      client.invalidateQueries({ queryKey: ["dashboard"] }),
    ]);
  };
  const action = useMutation({
    mutationFn: ({ path, method, body }: { path: string; method: string; body: object }) =>
      api<EntryDetail>(path, { method, body: JSON.stringify(body) }),
    onSuccess: async (result) => {
      await refresh();
      client.setQueryData(["entry", selected], result);
      setEditing(false);
      setDeleting(false);
      setCreating(false);
      setNotice("操作已保存");
      window.setTimeout(() => setNotice(""), 2400);
    },
  });
  const updateParam = (key: string, value: string) => {
    const next = new URLSearchParams(params);
    if (value) next.set(key, value);
    else next.delete(key);
    if (key !== "page" && key !== "entry") next.set("page", "1");
    setParams(next);
  };
  const closeDrawer = () => {
    const next = new URLSearchParams(params);
    next.delete("entry");
    setParams(next);
  };
  const current = detail.data?.entry;
  const revisions = detail.data?.revisions ?? [];

  return (
    <div className="entries-page">
      {notice && <div className="toast">{notice}</div>}
      <div className="page-heading">
        <div><p className="eyebrow">账目管理</p><h2>每一笔，都可追溯。</h2></div>
        <div className="heading-actions"><span className="result-count">共 {entries.data?.total ?? 0} 笔</span><button className="primary-small" onClick={() => setCreating(true)}><Plus size={16} /> 新建账目</button></div>
      </div>
      <section className="filter-bar">
        <label className="search-box"><Search size={17} /><input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="搜索备注、分类或短 ID" /></label>
        <select aria-label="收支方向" value={params.get("direction") ?? ""} onChange={(event) => updateParam("direction", event.target.value)}><option value="">全部收支</option><option value="EXPENSE">支出</option><option value="INCOME">收入</option></select>
        <input aria-label="分类" value={params.get("category") ?? ""} onChange={(event) => updateParam("category", event.target.value)} placeholder="分类" />
        <select aria-label="来源" value={params.get("source_type") ?? ""} onChange={(event) => updateParam("source_type", event.target.value)}><option value="">全部来源</option><option value="text">文字</option><option value="image">图片</option><option value="post">图文</option><option value="audio">语音</option></select>
        <input aria-label="最低金额" type="number" min="0" step="0.01" value={params.get("amount_min") ?? ""} onChange={(event) => updateParam("amount_min", event.target.value)} placeholder="最低金额" />
        <input aria-label="最高金额" type="number" min="0" step="0.01" value={params.get("amount_max") ?? ""} onChange={(event) => updateParam("amount_max", event.target.value)} placeholder="最高金额" />
        <label className="date-filter">开始<input type="date" value={(params.get("start") ?? "").slice(0, 10)} onChange={(event) => updateParam("start", dateQuery(event.target.value))} /></label>
        <label className="date-filter">结束<input type="date" value={(params.get("end") ?? "").slice(0, 10)} onChange={(event) => updateParam("end", dateQuery(event.target.value, true))} /></label>
        <select aria-label="删除状态" value={params.get("deleted") ?? "active"} onChange={(event) => updateParam("deleted", event.target.value)}><option value="active">有效账目</option><option value="deleted">已删除</option><option value="all">全部状态</option></select>
        <select aria-label="排序" value={`${params.get("sort") ?? "occurred_at"}:${params.get("order") ?? "desc"}`} onChange={(event) => { const [sort, order] = event.target.value.split(":"); const next = new URLSearchParams(params); next.set("sort", sort); next.set("order", order); next.set("page", "1"); setParams(next); }}><option value="occurred_at:desc">时间：新到旧</option><option value="occurred_at:asc">时间：旧到新</option><option value="amount:desc">金额：高到低</option><option value="amount:asc">金额：低到高</option><option value="updated_at:desc">最近更新</option></select>
      </section>
      <section className="table-panel">
        {entries.isLoading ? <div className="table-skeleton">正在加载账目…</div> : entries.isError ? <div className="state-panel"><h3>账目加载失败</h3><button onClick={() => entries.refetch()}>重试</button></div> : !entries.data?.items.length ? <div className="empty-ledger"><h3>还没有账目</h3><p>去飞书对飞账说：“午饭32元”</p></div> : <div className="table-scroll"><table><thead><tr><th>Short ID</th><th>时间</th><th>收支</th><th>金额</th><th>分类</th><th>账户</th><th>备注</th><th>来源</th><th>更新时间</th><th>状态</th></tr></thead><tbody>{entries.data.items.map((entry) => <tr key={entry.id} onClick={() => updateParam("entry", entry.short_id)}><td><button className="entry-link">#{entry.short_id}</button></td><td>{localTime(entry.occurred_at)}</td><td>{entry.direction === "EXPENSE" ? "支出" : "收入"}</td><td className={entry.direction === "INCOME" ? "positive" : ""}>{money(entry.amount)}</td><td><span className="category-pill">{entry.category}</span></td><td>{entry.account_name || "—"}</td><td className="note-cell">{entry.note || "—"}</td><td>{entry.source_type}</td><td>{localTime(entry.updated_at)}</td><td><span className={`status-dot ${entry.deleted_at ? "deleted" : ""}`}>{entry.deleted_at ? "已删除" : "有效"}</span></td></tr>)}</tbody></table></div>}
      </section>
      {entries.data && entries.data.pages > 1 && <div className="pagination"><button disabled={entries.data.page <= 1} onClick={() => updateParam("page", String(entries.data!.page - 1))}><ChevronLeft size={16} /> 上一页</button><span>{entries.data.page} / {entries.data.pages}</span><button disabled={entries.data.page >= entries.data.pages} onClick={() => updateParam("page", String(entries.data!.page + 1))}>下一页 <ChevronRight size={16} /></button></div>}
      {selected && <><button className="drawer-scrim" onClick={closeDrawer} aria-label="关闭详情" /><aside className="entry-drawer"><button className="drawer-close" onClick={closeDrawer} aria-label="关闭"><X /></button>{detail.isLoading ? <div className="drawer-loading">加载详情…</div> : detail.isError || !current ? <div className="state-panel"><h3>账目不存在</h3></div> : <><div className="drawer-title"><code>#{current.short_id}</code><span className={`status-dot ${current.deleted_at ? "deleted" : ""}`}>{current.deleted_at ? "已删除" : "有效"}</span><h2>{money(current.amount)}</h2><p>{current.direction === "EXPENSE" ? "支出" : "收入"} · {current.category}</p></div><dl className="detail-grid"><div><dt>发生时间</dt><dd>{localTime(current.occurred_at)}</dd></div><div><dt>账户</dt><dd>{current.account_name || "（默认账户）"}</dd></div><div><dt>币种</dt><dd>{current.currency}</dd></div><div><dt>备注</dt><dd>{current.note || "无"}</dd></div><div><dt>来源</dt><dd>{current.source_type}</dd></div><div><dt>创建时间</dt><dd>{localTime(current.created_at)}</dd></div><div><dt>更新时间</dt><dd>{localTime(current.updated_at)}</dd></div></dl><div className="drawer-actions">{current.deleted_at ? <button className="primary-small" onClick={() => action.mutate({ path: `/entries/${current.short_id}/restore`, method: "POST", body: { expected_updated_at: current.updated_at } })}><RotateCcw size={16} /> 恢复</button> : <><button onClick={() => setEditing(true)}><Pencil size={16} /> 修改</button><button className="danger" onClick={() => setDeleting(true)}><Trash2 size={16} /> 删除</button></>}</div><section className="revision-section"><h3>Revision Timeline</h3>{revisions.length ? revisions.map((revision) => <article key={revision.id}><i /><div><strong>{{ update: "修改", delete: "删除", restore: "恢复" }[revision.change_type]}</strong><time>{localTime(revision.created_at)}</time>{revision.change_type === "update" && <p>{revision.before.amount !== revision.after.amount ? `金额 ${String(revision.before.amount)} → ${String(revision.after.amount)}` : revision.before.account_id !== revision.after.account_id ? "交易账户已变更" : "账目信息已更新"}</p>}</div></article>) : <p className="muted-empty">暂无修改记录</p>}</section></>}</aside></>}
      {editing && current && <EditDialog entry={current} accounts={accounts.data?.items ?? []} busy={action.isPending} error={action.error?.message} onClose={() => setEditing(false)} onSave={(body) => action.mutate({ path: `/entries/${current.short_id}`, method: "PATCH", body })} />}
      {creating && <CreateEntryDialog accounts={accounts.data?.items ?? []} busy={action.isPending} error={action.error?.message} onClose={() => setCreating(false)} onSave={(body) => action.mutate({ path: "/entries", method: "POST", body })} />}
      {deleting && current && <div className="modal-layer"><div className="confirm-dialog"><h3>确认删除 #{current.short_id}？</h3><p>金额：{money(current.amount)}<br />分类：{current.category}</p><div><button onClick={() => setDeleting(false)}>取消</button><button className="danger-solid" disabled={action.isPending} onClick={() => action.mutate({ path: `/entries/${current.short_id}`, method: "DELETE", body: { expected_updated_at: current.updated_at } })}>确认删除</button></div></div></div>}
    </div>
  );
}

function EditDialog({ entry, accounts, busy, error, onClose, onSave }: { entry: EntryDetail["entry"]; accounts: Array<{ id: string; name: string; status: string }>; busy: boolean; error?: string; onClose: () => void; onSave: (body: object) => void }) {
  const [amount, setAmount] = useState(entry.amount);
  const [category, setCategory] = useState(entry.category);
  const [note, setNote] = useState(entry.note);
  const [direction, setDirection] = useState(entry.direction);
  const [accountId, setAccountId] = useState(entry.account_id);
  const [occurred, setOccurred] = useState(entry.occurred_at.slice(0, 16));
  return <div className="modal-layer"><form className="edit-dialog" onSubmit={(event) => { event.preventDefault(); onSave({ expected_updated_at: entry.updated_at, amount, category, note, direction, account_id: accountId || null, occurred_at: new Date(occurred).toISOString() }); }}><h3>修改 #{entry.short_id}</h3><label>金额<input type="number" min="0.01" step="0.01" value={amount} onChange={(event) => setAmount(event.target.value)} /></label><label>方向<select value={direction} onChange={(event) => setDirection(event.target.value as "EXPENSE" | "INCOME")}><option value="EXPENSE">支出</option><option value="INCOME">收入</option></select></label><label>账户<select value={accountId} onChange={(event) => setAccountId(event.target.value)}><option value="">（保持默认账户）</option>{accounts.filter((account) => account.status !== "archived").map((account) => <option key={account.id} value={account.id}>{account.name}</option>)}</select></label><label>分类<input maxLength={64} value={category} onChange={(event) => setCategory(event.target.value)} /></label><label>备注<textarea maxLength={500} value={note} onChange={(event) => setNote(event.target.value)} /></label><label>发生时间<input type="datetime-local" value={occurred} onChange={(event) => setOccurred(event.target.value)} /></label>{error && <p className="form-error">{error}</p>}<div><button type="button" onClick={onClose}>取消</button><button className="primary-small" disabled={busy}>保存修改</button></div></form></div>;
}

function CreateEntryDialog({ accounts, busy, error, onClose, onSave }: { accounts: Array<{ id: string; name: string; status: string }>; busy: boolean; error?: string; onClose: () => void; onSave: (body: object) => void }) {
  const [amount, setAmount] = useState("");
  const [category, setCategory] = useState("");
  const [note, setNote] = useState("");
  const [direction, setDirection] = useState<"EXPENSE" | "INCOME">("EXPENSE");
  const [accountId, setAccountId] = useState("");
  const [occurred, setOccurred] = useState(() => new Date().toISOString().slice(0, 16));
  return <div className="modal-layer"><form className="edit-dialog" onSubmit={(event) => { event.preventDefault(); onSave({ amount, direction, category, note, account_id: accountId || null, occurred_at: new Date(occurred).toISOString() }); }}><h3>新建账目</h3><label>金额<input type="number" min="0.01" step="0.01" value={amount} onChange={(event) => setAmount(event.target.value)} /></label><label>方向<select value={direction} onChange={(event) => setDirection(event.target.value as "EXPENSE" | "INCOME")}><option value="EXPENSE">支出</option><option value="INCOME">收入</option></select></label><label>账户<select value={accountId} onChange={(event) => setAccountId(event.target.value)}><option value="">（默认账户）</option>{accounts.filter((account) => account.status !== "archived").map((account) => <option key={account.id} value={account.id}>{account.name}</option>)}</select></label><label>分类<input maxLength={64} value={category} onChange={(event) => setCategory(event.target.value)} /></label><label>备注<textarea maxLength={500} value={note} onChange={(event) => setNote(event.target.value)} /></label><label>发生时间<input type="datetime-local" value={occurred} onChange={(event) => setOccurred(event.target.value)} /></label>{error && <p className="form-error">{error}</p>}<div><button type="button" onClick={onClose}>取消</button><button className="primary-small" disabled={busy || Number(amount) <= 0 || !category.trim()}>保存</button></div></form></div>;
}
