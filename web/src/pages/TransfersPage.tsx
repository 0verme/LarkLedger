import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeftRight, Plus, RotateCcw, X } from "lucide-react";
import { api, localTime, money, type AccountList, type TransferDetail, type TransferPage } from "../api";

export function TransfersPage() {
  const client = useQueryClient();
  const [creating, setCreating] = useState(false);
  const [selected, setSelected] = useState<string | null>(null);
  const [notice, setNotice] = useState("");
  const [page, setPage] = useState(1);
  const transfers = useQuery({
    queryKey: ["transfers", page],
    queryFn: () => api<TransferPage>(`/transfers?page=${page}&page_size=20`),
  });
  const detail = useQuery({
    queryKey: ["transfer", selected],
    queryFn: () => api<TransferDetail>(`/transfers/${selected}`),
    enabled: Boolean(selected),
  });
  const accounts = useQuery({ queryKey: ["accounts"], queryFn: () => api<AccountList>("/accounts") });
  const accountNames = new Map((accounts.data?.items ?? []).map((item) => [item.id, item.name]));
  const refresh = async () => {
    await Promise.all([
      client.invalidateQueries({ queryKey: ["transfers"] }),
      client.invalidateQueries({ queryKey: ["transfer", selected] }),
      client.invalidateQueries({ queryKey: ["assets"] }),
      client.invalidateQueries({ queryKey: ["accounts"] }),
      client.invalidateQueries({ queryKey: ["dashboard"] }),
    ]);
  };
  const reverse = useMutation({
    mutationFn: (transferId: string) => api<TransferDetail["transfer"]>(`/transfers/${transferId}/reverse`, { method: "POST" }),
    onSuccess: async () => {
      await refresh();
      setNotice("转账已撤销");
      window.setTimeout(() => setNotice(""), 2400);
    },
  });
  if (transfers.isLoading) return <div className="page-skeleton"><div /><div /></div>;
  if (transfers.isError || !transfers.data) {
    return <div className="state-panel"><h3>转账加载失败</h3><button onClick={() => transfers.refetch()}>重试</button></div>;
  }
  const rows = transfers.data.items;
  const current = detail.data?.transfer;
  return (
    <section className="transfers-page">
      {notice && <div className="toast">{notice}</div>}
      <div className="page-heading">
        <div><p className="eyebrow">账户间转账</p><h2>转账，不改变你的总账。</h2></div>
        <button className="primary-small" onClick={() => setCreating(true)} disabled={!accounts.data?.items.length}><Plus size={16} /> 新建转账</button>
      </div>
      {rows.length === 0 ? (
        <div className="empty-ledger"><ArrowLeftRight size={30} /><h3>还没有转账</h3><p>在两个账户之间转移资金，不会计入收入或支出</p></div>
      ) : (
        <section className="table-panel">
          <div className="table-scroll"><table><thead><tr><th>时间</th><th>转出</th><th>转入</th><th>金额</th><th>备注</th><th>状态</th></tr></thead><tbody>
            {rows.map((transfer) => (
              <tr key={transfer.id} onClick={() => setSelected(transfer.id)}>
                <td>{localTime(transfer.occurred_at)}</td>
                <td>{accountNames.get(transfer.from_account_id) ?? "已归档账户"}</td>
                <td>{accountNames.get(transfer.to_account_id) ?? "已归档账户"}</td>
                <td className="positive">{money(transfer.amount)}</td>
                <td className="note-cell">{transfer.note || "—"}</td>
                <td><span className={`status-dot ${transfer.reversed_at ? "deleted" : ""}`}>{transfer.reversed_at ? "已撤销" : "有效"}</span></td>
              </tr>
            ))}
          </tbody></table></div>
        </section>
      )}
      {transfers.data.pages > 1 && (
        <div className="pagination">
          <button disabled={page <= 1} onClick={() => setPage((value) => value - 1)}>上一页</button>
          <span>{page} / {transfers.data.pages}</span>
          <button disabled={page >= transfers.data.pages} onClick={() => setPage((value) => value + 1)}>下一页</button>
        </div>
      )}
      {creating && <CreateTransferDialog accounts={accounts.data?.items ?? []} onClose={() => setCreating(false)} onCreated={async () => { await refresh(); setCreating(false); setNotice("转账已创建"); window.setTimeout(() => setNotice(""), 2400); }} />}
      {selected && (
        <>
          <button className="drawer-scrim" onClick={() => setSelected(null)} aria-label="关闭转账详情" />
          <aside className="entry-drawer pending-drawer">
            <button className="drawer-close" onClick={() => setSelected(null)} aria-label="关闭"><X /></button>
            {detail.isLoading ? <div className="drawer-loading">加载详情…</div> : detail.isError || !current ? <div className="state-panel"><h3>转账不存在</h3></div> : (
              <>
                <div className="drawer-title">
                  <span className={`status-dot ${current.reversed_at ? "deleted" : ""}`}>{current.reversed_at ? "已撤销" : "有效"}</span>
                  <h2>{money(current.amount)}</h2>
                  <p>{accountNames.get(current.from_account_id) ?? "已归档账户"} → {accountNames.get(current.to_account_id) ?? "已归档账户"}</p>
                </div>
                <dl className="detail-grid">
                  <div><dt>发生时间</dt><dd>{localTime(current.occurred_at)}</dd></div>
                  <div><dt>币种</dt><dd>{current.currency}</dd></div>
                  <div><dt>备注</dt><dd>{current.note || "无"}</dd></div>
                  <div><dt>创建时间</dt><dd>{localTime(current.created_at)}</dd></div>
                  <div><dt>更新时间</dt><dd>{localTime(current.updated_at)}</dd></div>
                </dl>
                {!current.reversed_at && <div className="drawer-actions"><button className="danger" disabled={reverse.isPending} onClick={() => reverse.mutate(current.id)}><RotateCcw size={16} /> 撤销转账</button></div>}
                {reverse.error && <p className="form-error">{reverse.error.message}</p>}
                <section className="revision-section"><h3>操作记录</h3>{detail.data?.revisions.length ? detail.data.revisions.map((revision) => <article key={revision.id}><i /><div><strong>{{ create: "创建", update: "修改", reverse: "撤销" }[revision.change_type]}</strong><time>{localTime(revision.created_at)}</time>{revision.change_type === "update" && <p>金额 {String(revision.before.amount)} → {String(revision.after.amount)}</p>}</div></article>) : <p className="muted-empty">暂无操作记录</p>}</section>
              </>
            )}
          </aside>
        </>
      )}
    </section>
  );
}

function CreateTransferDialog({ accounts, onClose, onCreated }: { accounts: Array<{ id: string; name: string; status: string }>; onClose: () => void; onCreated: () => void }) {
  const [fromAccountId, setFromAccountId] = useState("");
  const [toAccountId, setToAccountId] = useState("");
  const [amount, setAmount] = useState("");
  const [note, setNote] = useState("");
  const [occurredAt, setOccurredAt] = useState(() => new Date().toISOString().slice(0, 16));
  const mutation = useMutation({
    mutationFn: () =>
      api<TransferDetail["transfer"]>("/transfers", {
        method: "POST",
        body: JSON.stringify({ from_account_id: fromAccountId, to_account_id: toAccountId, amount, occurred_at: new Date(occurredAt).toISOString(), note }),
      }),
    onSuccess: onCreated,
  });
  const active = accounts.filter((account) => account.status !== "archived");
  return (
    <div className="modal-layer">
      <form className="edit-dialog" onSubmit={(event) => { event.preventDefault(); mutation.mutate(); }}>
        <h3>新建转账</h3>
        <label>转出账户<select value={fromAccountId} onChange={(event) => setFromAccountId(event.target.value)}><option value="">请选择</option>{active.map((account) => <option key={account.id} value={account.id}>{account.name}</option>)}</select></label>
        <label>转入账户<select value={toAccountId} onChange={(event) => setToAccountId(event.target.value)}><option value="">请选择</option>{active.map((account) => <option key={account.id} value={account.id}>{account.name}</option>)}</select></label>
        <label>金额<input type="number" min="0.01" step="0.01" value={amount} onChange={(event) => setAmount(event.target.value)} /></label>
        <label>发生时间<input type="datetime-local" value={occurredAt} onChange={(event) => setOccurredAt(event.target.value)} /></label>
        <label>备注<textarea maxLength={500} value={note} onChange={(event) => setNote(event.target.value)} /></label>
        {mutation.error && <p className="form-error">{mutation.error.message}</p>}
        <div><button type="button" onClick={onClose}>取消</button><button className="primary-small" disabled={mutation.isPending || !fromAccountId || !toAccountId || fromAccountId === toAccountId || Number(amount) <= 0}>创建转账</button></div>
      </form>
    </div>
  );
}
