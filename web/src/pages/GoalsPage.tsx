import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Archive, CheckCircle2, Flag, Pencil, Plus, Target, Trash2, TrendingUp } from "lucide-react";
import { api, money, type Account, type AccountList, type Goal, type GoalCreateInput, type GoalList, type GoalUpdateInput } from "../api";

function progressTone(percent: number, reached: boolean): string {
  if (reached) return "reached";
  if (percent >= 100) return "over";
  if (percent >= 80) return "warning";
  return "";
}

function statusMeta(status: Goal["status"]): { label: string; tone: string } {
  switch (status) {
    case "completed": return { label: "已完成", tone: "normal" };
    case "archived": return { label: "已归档", tone: "none" };
    default: return { label: "进行中", tone: "warning" };
  }
}

type GoalForm = {
  name: string;
  description: string;
  target_amount: string;
  currency: string;
  target_date: string;
  account_ids: string[];
};

export function GoalsPage() {
  const client = useQueryClient();
  const [editing, setEditing] = useState<{ goalId: string | null; form: GoalForm } | null>(null);
  const [deleting, setDeleting] = useState<Goal | null>(null);
  const [notice, setNotice] = useState("");
  const goals = useQuery({ queryKey: ["goals"], queryFn: () => api<GoalList>("/goals") });
  const accounts = useQuery({
    queryKey: ["accounts"],
    queryFn: () => api<AccountList>("/accounts?include_archived=true"),
  });
  const action = useMutation({
    mutationFn: ({ path, method, body }: { path: string; method: string; body?: object }) =>
      api<Goal>(path, { method, body: body ? JSON.stringify(body) : undefined }),
    onSuccess: async (_, vars) => {
      await client.invalidateQueries({ queryKey: ["goals"] });
      await client.invalidateQueries({ queryKey: ["insights"] });
      if (vars.path.endsWith("/complete")) setNotice("目标已标记完成");
      else if (vars.path.endsWith("/archive")) setNotice("目标已归档");
      else if (vars.method === "DELETE") setNotice("目标已删除");
      else if (vars.method === "PATCH") setNotice("目标已更新");
      else setNotice("目标已创建");
      setEditing(null);
      window.setTimeout(() => setNotice(""), 2400);
    },
  });
  const save = (body: GoalCreateInput) => {
    if (editing?.goalId) {
      action.mutate({ path: `/goals/${editing.goalId}`, method: "PATCH", body: body as GoalUpdateInput });
    } else {
      action.mutate({ path: "/goals", method: "POST", body });
    }
  };
  if (goals.isLoading) return <div className="page-skeleton"><div /><div /></div>;
  if (goals.isError || !goals.data) {
    return <div className="state-panel"><h3>目标加载失败</h3><button onClick={() => goals.refetch()}>重试</button></div>;
  }
  const items = goals.data.items.filter((item) => item.status !== "archived");
  const archived = goals.data.items.filter((item) => item.status === "archived");
  const accountOptions = accounts.data?.items ?? [];
  return (
    <section className="recurring-page">
      {notice && <div className="toast">{notice}</div>}
      <div className="page-heading">
        <div><p className="eyebrow">FINANCIAL GOALS</p><h2>把想存到的钱，变成看得见的进度。</h2></div>
        <button className="primary-small" onClick={() => setEditing({ goalId: null, form: emptyForm(accountOptions) })}><Plus size={16} /> 创建目标</button>
      </div>
      {items.length === 0 ? (
        <div className="empty-ledger"><Flag size={30} /><h3>还没有财务目标</h3><p>在 Web 端创建“应急储备 60000”，进度来自真实账户余额，不用手动记账。</p><button className="primary-small" onClick={() => setEditing({ goalId: null, form: emptyForm(accountOptions) })}><Plus size={15} /> 创建目标</button></div>
      ) : (
        <div className="goal-grid">
          {items.map((goal) => {
            const meta = statusMeta(goal.status);
            const percent = Number(goal.progress_percent ?? 0);
            const tone = progressTone(percent, goal.is_target_reached);
            return (
              <article className="goal-card" key={goal.id}>
                <div className="goal-card-head">
                  <div><h3>{goal.name}</h3><span className="status-chip warning">{meta.label}</span></div>
                  <div className="row-actions">
                    <button aria-label={`修改 ${goal.name}`} disabled={action.isPending} onClick={() => setEditing({ goalId: goal.id, form: formFromGoal(goal) })}><Pencil size={15} /></button>
                    {goal.status === "active" && (
                      <>
                        <button aria-label={`完成 ${goal.name}`} disabled={action.isPending} onClick={() => action.mutate({ path: `/goals/${goal.id}/complete`, method: "POST" })}><CheckCircle2 size={15} /></button>
                        <button aria-label={`归档 ${goal.name}`} disabled={action.isPending} onClick={() => action.mutate({ path: `/goals/${goal.id}/archive`, method: "POST" })}><Archive size={15} /></button>
                      </>
                    )}
                    <button className="danger" aria-label={`删除 ${goal.name}`} disabled={action.isPending} onClick={() => setDeleting(goal)}><Trash2 size={15} /></button>
                  </div>
                </div>
                {goal.description && <p className="goal-desc">{goal.description}</p>}
                <div className="goal-amount">
                  <strong>{money(goal.current_amount)}</strong><span> / {money(goal.target_amount)}</span>
                </div>
                <div className="budget-track" aria-label="目标进度">
                  <i className={tone} style={{ width: `${Math.min(percent, 100)}%` }} />
                </div>
                <p className="goal-meta">
                  {goal.is_target_reached
                    ? <b className="positive">✅ 目标已达成</b>
                    : <b>{Number(goal.progress_percent ?? 0).toFixed(1)}%</b>}
                  {goal.target_date && <span>目标日期 {goal.target_date}</span>}
                </p>
                <p className="goal-meta muted-empty">
                  {goal.account_bindings.length > 0
                    ? `账户：${goal.account_bindings.map((b) => b.account_name ?? "未命名").join("、")}`
                    : "未绑定账户"}
                  {goal.remaining_amount !== null && !goal.is_target_reached && ` · 剩余 ${money(goal.remaining_amount)}`}
                </p>
                {goal.is_target_reached && <p className="goal-meta muted-empty">余额可能随后变化，状态由你管理。</p>}
              </article>
            );
          })}
        </div>
      )}
      {archived.length > 0 && (
        <details className="panel" style={{ marginTop: 22 }}>
          <summary className="panel-title"><h3>已归档目标</h3><span>{archived.length} 个</span></summary>
          <div className="category-list">
            {archived.map((goal) => (
              <div key={goal.id}><span>{goal.name}</span><b>{money(goal.target_amount)}</b><em>{Number(goal.progress_percent ?? 0).toFixed(0)}%</em></div>
            ))}
          </div>
        </details>
      )}
      {editing && <GoalDialog form={editing.form} accounts={accountOptions} busy={action.isPending} error={action.error?.message} isEdit={editing.goalId !== null} onClose={() => setEditing(null)} onSave={save} />}
      {deleting && (
        <div className="modal-layer">
          <div className="confirm-dialog">
            <h3>删除目标「{deleting.name}」？</h3>
            <p>不会影响任何账户或账目。</p>
            <div>
              <button onClick={() => setDeleting(null)} disabled={action.isPending}>取消</button>
              <button className="danger-solid" disabled={action.isPending} onClick={() => { action.mutate({ path: `/goals/${deleting.id}`, method: "DELETE" }); setDeleting(null); }}>确认删除</button>
            </div>
          </div>
        </div>
      )}
    </section>
  );
}

function emptyForm(accounts: Account[]): GoalForm {
  const asset = accounts.find((item) => item.type !== "liability");
  return {
    name: "",
    description: "",
    target_amount: "",
    currency: "",
    target_date: "",
    account_ids: asset ? [asset.id] : [],
  };
}

function formFromGoal(goal: Goal): GoalForm {
  return {
    name: goal.name,
    description: goal.description,
    target_amount: goal.target_amount,
    currency: goal.currency === "CNY" ? "" : goal.currency,
    target_date: goal.target_date ?? "",
    account_ids: goal.account_bindings.map((b) => b.account_id),
  };
}

function GoalDialog({ form, accounts, busy, error, isEdit, onClose, onSave }: {
  form: GoalForm;
  accounts: Account[];
  busy: boolean;
  error?: string;
  isEdit: boolean;
  onClose: () => void;
  onSave: (body: GoalCreateInput) => void;
}) {
  const savings = useMemo(() => accounts.filter((item) => item.type !== "liability"), [accounts]);
  const [value, setValue] = useState<GoalForm>(form);
  const valid = value.name.trim() && Number(value.target_amount) > 0 && value.account_ids.length > 0;
  const toggleAccount = (id: string) => {
    setValue({
      ...value,
      account_ids: value.account_ids.includes(id)
        ? value.account_ids.filter((item) => item !== id)
        : [...value.account_ids, id],
    });
  };
  const submit = () => {
    onSave({
      name: value.name,
      description: value.description,
      target_amount: value.target_amount,
      currency: value.currency.trim() ? value.currency.trim().toUpperCase() : null,
      target_date: value.target_date || null,
      account_ids: value.account_ids,
    });
  };
  return (
    <div className="modal-layer">
      <form className="edit-dialog" onSubmit={(event) => { event.preventDefault(); if (valid) submit(); }}>
        <h3>{isEdit ? "修改目标" : "创建目标"}</h3>
        <label>名称<input autoFocus maxLength={64} value={value.name} onChange={(event) => setValue({ ...value, name: event.target.value })} placeholder="例如：应急储备" /></label>
        <label>描述<input maxLength={200} value={value.description} onChange={(event) => setValue({ ...value, description: event.target.value })} placeholder="可选" /></label>
        <label>目标金额<input type="number" min="0.01" step="0.01" value={value.target_amount} onChange={(event) => setValue({ ...value, target_amount: event.target.value })} placeholder="60000" /></label>
        <label>币种<input maxLength={3} value={value.currency} onChange={(event) => setValue({ ...value, currency: event.target.value })} placeholder="CNY（留空）" /></label>
        <label>目标日期<input type="date" value={value.target_date} onChange={(event) => setValue({ ...value, target_date: event.target.value })} /></label>
        <label>计算进度用的账户（至少 1 个，币种需一致）</label>
        <div className="goal-account-picker">
          {savings.length === 0 && <p className="muted-empty">当前账本还没有现金 / 资产账户，请先在「账户」页创建。</p>}
          {savings.map((item) => (
            <label key={item.id} className="check-label">
              <input type="checkbox" checked={value.account_ids.includes(item.id)} onChange={() => toggleAccount(item.id)} />
              <span>{item.name}{item.is_default ? "（默认）" : ""} · {item.currency}</span>
            </label>
          ))}
        </div>
        {error && <p className="form-error">{error}</p>}
        <div><button type="button" onClick={onClose}>取消</button><button className="primary-small" disabled={busy || !valid}>{busy ? "保存中…" : "保存"}</button></div>
      </form>
    </div>
  );
}

export function GoalProgressIcon() {
  return <TrendingUp size={15} />;
}

export function GoalTargetIcon() {
  return <Target size={15} />;
}
