import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Archive, Eye, EyeOff, Landmark, Pencil, Plus, Star } from "lucide-react";
import { api, money, type Account, type AccountList, type AssetSummary } from "../api";

type AccountType = "cash" | "asset" | "liability";

const typeLabels: Record<AccountType, string> = {
  cash: "现金",
  asset: "资产",
  liability: "负债",
};

export function AccountsPage() {
  const client = useQueryClient();
  const [creating, setCreating] = useState(false);
  const [renaming, setRenaming] = useState<Account | null>(null);
  const [notice, setNotice] = useState("");
  const accounts = useQuery({
    queryKey: ["accounts"],
    queryFn: () => api<AccountList>("/accounts?include_archived=true"),
  });
  const assets = useQuery({
    queryKey: ["assets"],
    queryFn: () => api<AssetSummary>("/assets"),
  });
  const refresh = async () => {
    await Promise.all([
      client.invalidateQueries({ queryKey: ["accounts"] }),
      client.invalidateQueries({ queryKey: ["assets"] }),
      client.invalidateQueries({ queryKey: ["dashboard"] }),
    ]);
  };
  const action = useMutation({
    mutationFn: ({ path, method, body }: { path: string; method: string; body?: object }) =>
      api<Account>(path, { method, body: body ? JSON.stringify(body) : undefined }),
    onSuccess: async () => {
      await refresh();
      setRenaming(null);
      setCreating(false);
      setNotice("账户已更新");
      window.setTimeout(() => setNotice(""), 2400);
    },
  });
  if (accounts.isLoading) return <div className="page-skeleton"><div /><div /></div>;
  if (accounts.isError || !accounts.data) {
    return (
      <div className="state-panel">
        <h3>账户加载失败</h3>
        <button onClick={() => { accounts.refetch(); assets.refetch(); }}>重试</button>
      </div>
    );
  }
  const rows = accounts.data.items;
  const balances = new Map((assets.data?.accounts ?? []).map((item) => [item.account_id, item]));
  const sorted = [...rows].sort((a, b) => Number(b.is_default) - Number(a.is_default));
  return (
    <section className="accounts-page">
      {notice && <div className="toast">{notice}</div>}
      <div className="page-heading">
        <div><p className="eyebrow">账户管理</p><h2>每一笔钱，都在它该在的地方。</h2></div>
        <button className="primary-small" onClick={() => setCreating(true)}><Plus size={16} /> 创建账户</button>
      </div>
      {assets.data && (
        <section className="metric-grid asset-metrics">
          <article><span>总资产</span><strong className="positive">{money(assets.data.total_assets)}</strong></article>
          <article><span>总负债</span><strong>{money(assets.data.total_liabilities)}</strong></article>
          <article><span>净资产</span><strong className={Number(assets.data.net_assets) >= 0 ? "positive" : ""}>{money(assets.data.net_assets)}</strong></article>
        </section>
      )}
      {sorted.length === 0 ? (
        <div className="empty-ledger"><Landmark size={30} /><h3>还没有账户</h3><p>创建一个账户来管理你的资金余额</p></div>
      ) : (
        <section className="table-panel">
          <div className="table-scroll"><table><thead><tr><th>账户</th><th>类型</th><th>余额</th><th>期初余额</th><th>可见性</th><th>状态</th><th>操作</th></tr></thead><tbody>
            {sorted.map((account) => {
              const balance = balances.get(account.id);
              return (
                <tr key={account.id}>
                  <td><strong>{account.name}</strong>{account.is_default && <span className="status-dot" style={{ marginLeft: 8 }}>默认</span>}</td>
                  <td>{typeLabels[account.type]}</td>
                  <td className={Number(balance?.current_balance ?? account.opening_balance) >= 0 ? "positive" : ""}>{money(balance?.current_balance ?? account.opening_balance)}</td>
                  <td>{money(account.opening_balance)}</td>
                  <td><span className={`visibility-badge ${account.visibility === "private" ? "private" : ""}`}>{account.visibility === "private" ? "私人" : "共享"}</span></td>
                  <td><span className={`status-dot ${account.status === "archived" ? "deleted" : ""}`}>{account.status === "archived" ? "已归档" : "有效"}</span></td>
                  <td>
                    <div className="row-actions">
                      <button aria-label={`修改 ${account.name}`} disabled={action.isPending} onClick={() => setRenaming(account)}><Pencil size={15} /> 改名</button>
                      {account.status !== "archived" && (
                        <button aria-label={`切换 ${account.name} 可见性`} disabled={action.isPending} onClick={() => action.mutate({ path: `/accounts/${account.id}/visibility`, method: "POST", body: { visibility: account.visibility === "private" ? "shared" : "private" } })}>
                          {account.visibility === "private" ? <><Eye size={15} /> 共享</> : <><EyeOff size={15} /> 私人</>}
                        </button>
                      )}
                      {account.status !== "archived" && !account.is_default && (
                        <>
                          <button aria-label={`设为默认 ${account.name}`} disabled={action.isPending} onClick={() => action.mutate({ path: `/accounts/${account.id}/default`, method: "POST" })}><Star size={15} /> 默认</button>
                          <button className="danger" aria-label={`归档 ${account.name}`} disabled={action.isPending} onClick={() => action.mutate({ path: `/accounts/${account.id}/archive`, method: "POST" })}><Archive size={15} /> 归档</button>
                        </>
                      )}
                    </div>
                  </td>
                </tr>
              );
            })}
          </tbody></table></div>
        </section>
      )}
      {creating && <CreateDialog busy={action.isPending} error={action.error?.message} onClose={() => setCreating(false)} onCreate={(body) => action.mutate({ path: "/accounts", method: "POST", body })} />}
      {renaming && <RenameDialog account={renaming} busy={action.isPending} error={action.error?.message} onClose={() => setRenaming(null)} onSave={(name) => action.mutate({ path: `/accounts/${renaming.id}`, method: "PATCH", body: { name } })} />}
    </section>
  );
}

function CreateDialog({ busy, error, onClose, onCreate }: { busy: boolean; error?: string; onClose: () => void; onCreate: (body: object) => void }) {
  const [name, setName] = useState("");
  const [type, setType] = useState<AccountType>("asset");
  const [openingBalance, setOpeningBalance] = useState("0");
  const [isDefault, setIsDefault] = useState(false);
  const [visibility, setVisibility] = useState<"shared" | "private">("shared");
  return (
    <div className="modal-layer">
      <form className="edit-dialog" onSubmit={(event) => { event.preventDefault(); onCreate({ name, type, opening_balance: openingBalance, is_default: isDefault, visibility }); }}>
        <h3>创建账户</h3>
        <label>账户名称<input autoFocus maxLength={64} value={name} onChange={(event) => setName(event.target.value)} placeholder="例如：招商银行" /></label>
        <label>账户类型<select value={type} onChange={(event) => setType(event.target.value as AccountType)}><option value="asset">资产</option><option value="cash">现金</option><option value="liability">负债</option></select></label>
        <label>期初余额<input type="number" min="0" step="0.01" value={openingBalance} onChange={(event) => setOpeningBalance(event.target.value)} /></label>
        <label>可见性<select value={visibility} onChange={(event) => setVisibility(event.target.value as "shared" | "private")}><option value="shared">共享（家庭成员可见）</option><option value="private">私人（仅自己可见）</option></select></label>
        <label className="check-label"><input type="checkbox" checked={isDefault} onChange={(event) => setIsDefault(event.target.checked)} /> 设为默认账户</label>
        {error && <p className="form-error">{error}</p>}
        <div><button type="button" onClick={onClose}>取消</button><button className="primary-small" disabled={busy || !name.trim()}>创建</button></div>
      </form>
    </div>
  );
}

function RenameDialog({ account, busy, error, onClose, onSave }: { account: Account; busy: boolean; error?: string; onClose: () => void; onSave: (name: string) => void }) {
  const [name, setName] = useState(account.name);
  return (
    <div className="modal-layer">
      <form className="edit-dialog" onSubmit={(event) => { event.preventDefault(); onSave(name); }}>
        <h3>修改账户名称</h3>
        <label>账户名称<input autoFocus maxLength={64} value={name} onChange={(event) => setName(event.target.value)} /></label>
        {error && <p className="form-error">{error}</p>}
        <div><button type="button" onClick={onClose}>取消</button><button className="primary-small" disabled={busy || !name.trim()}>保存</button></div>
      </form>
    </div>
  );
}
