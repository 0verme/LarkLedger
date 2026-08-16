import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
	CalendarClock,
	Check,
	Copy,
	KeyRound,
	Plus,
	ShieldAlert,
	Trash2,
} from "lucide-react";
import {
	api,
	localTime,
	type ClientCredential,
	type ClientCredentialCreated,
	type ClientCredentialList,
	type ClientCredentialScope,
} from "../api";
import { EmptyState, PageSkeleton } from "../components/States";

const SCOPE_LABELS: Record<ClientCredentialScope, string> = {
	"ledger:read": "只读（查询账本）",
	"ledger:write": "读写（记账 / 修改 / 删除）",
	"pending:write": "待确认（确认 / 取消）",
};

function TokenSecret({
	token,
	onClose,
}: {
	token: string;
	onClose: () => void;
}) {
	const [copied, setCopied] = useState(false);
	const copy = async () => {
		await navigator.clipboard.writeText(token).catch(() => undefined);
		setCopied(true);
		window.setTimeout(() => setCopied(false), 1600);
	};
	return (
		<div className="modal-layer">
			<div className="edit-dialog">
				<p className="eyebrow">一次性的令牌明文</p>
				<h3>请立即保存</h3>
				<p className="token-warning">
					<ShieldAlert size={15} />{" "}
					明文只会显示这一次，关闭后无法再次查看。服务端仅保存哈希。
				</p>
				<div className="token-secret-row">
					<code className="token-secret">{token}</code>
					<button className="primary-small" type="button" onClick={copy}>
						{copied ? <Check size={15} /> : <Copy size={15} />}{" "}
						{copied ? "已复制" : "复制"}
					</button>
				</div>
				<div className="dialog-actions">
					<button type="button" className="primary-small" onClick={onClose}>
						我已保存
					</button>
				</div>
			</div>
		</div>
	);
}

function CreateTokenDialog({ onClose }: { onClose: () => void }) {
	const queryClient = useQueryClient();
	const [name, setName] = useState("");
	const [expires, setExpires] = useState("");
	const [scopes, setScopes] = useState<ClientCredentialScope[]>([
		"ledger:read",
		"ledger:write",
	]);
	const [error, setError] = useState<string | null>(null);
	const [created, setCreated] = useState<ClientCredentialCreated | null>(null);
	const create = useMutation({
		mutationFn: () =>
			api<ClientCredentialCreated>("/client-credentials", {
				method: "POST",
				body: JSON.stringify({
					name,
					scopes,
					expires_at: expires ? `${expires}T00:00:00` : null,
				}),
			}),
		onSuccess: (result) => {
			setCreated(result);
			void queryClient.invalidateQueries({ queryKey: ["client-credentials"] });
		},
		onError: (exc: Error) => setError(exc.message),
	});
	const toggleScope = (scope: ClientCredentialScope) => {
		setScopes((current) =>
			current.includes(scope)
				? current.filter((item) => item !== scope)
				: [...current, scope],
		);
	};
	if (created) return <TokenSecret token={created.token} onClose={onClose} />;
	return (
		<div className="modal-layer">
			<form
				className="edit-dialog"
				onSubmit={(event) => {
					event.preventDefault();
					setError(null);
					create.mutate();
				}}
			>
				<p className="eyebrow">创建 API 令牌</p>
				<h3>给设备一个身份</h3>
				<label className="field-label">名称</label>
				<input
					className="text-input"
					value={name}
					onChange={(event) => setName(event.target.value)}
					placeholder="例如：ESP32 记账按钮 / CLI"
					maxLength={128}
					required
				/>
				<label className="field-label">过期时间（可选）</label>
				<input
					className="text-input"
					type="date"
					value={expires}
					onChange={(event) => setExpires(event.target.value)}
				/>
				<label className="field-label">权限范围</label>
				<div className="scope-options">
					{(Object.keys(SCOPE_LABELS) as ClientCredentialScope[]).map(
						(scope) => (
							<label className="check-label" key={scope}>
								<input
									type="checkbox"
									checked={scopes.includes(scope)}
									onChange={() => toggleScope(scope)}
								/>
								<span>
									<strong>{scope}</strong>
									<small>{SCOPE_LABELS[scope]}</small>
								</span>
							</label>
						),
					)}
				</div>
				{error && <p className="form-error">{error}</p>}
				<div className="dialog-actions">
					<button type="button" onClick={onClose}>
						取消
					</button>
					<button
						className="primary-small"
						disabled={create.isPending || !name.trim() || scopes.length === 0}
					>
						创建令牌
					</button>
				</div>
			</form>
		</div>
	);
}

export function ApiTokensPage() {
	const queryClient = useQueryClient();
	const [creating, setCreating] = useState(false);
	const [notice, setNotice] = useState<string | null>(null);
	const [revoking, setRevoking] = useState<ClientCredential | null>(null);
	const tokens = useQuery({
		queryKey: ["client-credentials"],
		queryFn: () => api<ClientCredentialList>("/client-credentials"),
	});
	const revoke = useMutation({
		mutationFn: (id: string) =>
			api<void>(`/client-credentials/${id}`, { method: "DELETE" }),
		onSuccess: async () => {
			await queryClient.invalidateQueries({ queryKey: ["client-credentials"] });
			setNotice("令牌已撤销，立即失效。");
		},
	});
	const scopeText = (scopes: string[]) =>
		scopes
			.map((scope) => SCOPE_LABELS[scope as ClientCredentialScope] ?? scope)
			.join("、");
	const expired = (row: ClientCredential) =>
		!!row.expires_at &&
		!row.revoked_at &&
		new Date(row.expires_at) <= new Date();
	return (
		<section className="tokens-page">
			{notice && <div className="toast">{notice}</div>}
			<div className="page-heading">
				<div>
					<p className="eyebrow">CLIENT API</p>
					<h2>API 令牌</h2>
					<p className="page-subtitle">
						给 CLI / 硬件 / 未来客户端一个独立的 Bearer 身份（
						<code>/api/v1</code>）。
					</p>
				</div>
				<button className="primary-small" onClick={() => setCreating(true)}>
					<Plus size={16} /> 创建令牌
				</button>
			</div>
			<div className="config-notice">
				<KeyRound size={20} />
				<p>
					令牌明文只在创建时显示一次；服务端只保存哈希摘要。令牌会过期（若设置了日期）且可随时撤销，撤销立即生效。请勿把令牌提交到
					Git 或写入公开固件仓库。
				</p>
			</div>
			{tokens.isLoading ? (
				<PageSkeleton rows={2} />
			) : tokens.isError || !tokens.data ? (
				<div className="state-panel">
					<h3>令牌列表加载失败</h3>
					<button onClick={() => tokens.refetch()}>重试</button>
				</div>
			) : tokens.data.items.length === 0 ? (
				<EmptyState
					icon={<KeyRound size={30} />}
					title="还没有 API 令牌"
					description="创建一个令牌给 CLI 或硬件客户端使用。"
				/>
			) : (
				<section className="table-panel">
					<div className="table-scroll">
						<table>
							<thead>
								<tr>
									<th>名称</th>
									<th>前缀</th>
									<th>权限</th>
									<th>最近使用</th>
									<th>过期</th>
									<th>状态</th>
									<th>操作</th>
								</tr>
							</thead>
							<tbody>
								{tokens.data.items.map((row) => (
									<tr key={row.id}>
										<td>
											<strong>{row.name}</strong>
										</td>
										<td>
											<code className="token-prefix">{row.token_prefix}…</code>
										</td>
										<td className="token-scopes">{scopeText(row.scopes)}</td>
										<td>
											{row.last_used_at
												? localTime(row.last_used_at)
												: "从未使用"}
										</td>
										<td>
											{row.expires_at ? localTime(row.expires_at) : "永不过期"}
										</td>
										<td>
											{row.revoked_at ? (
												<span className="status-dot deleted">已撤销</span>
											) : expired(row) ? (
												<span className="status-dot deleted">已过期</span>
											) : (
												<span className="status-dot">有效</span>
											)}
										</td>
										<td>
											{row.revoked_at ? (
												<span className="muted-text">—</span>
											) : (
												<div className="row-actions">
													<button
														className="danger"
														disabled={revoke.isPending}
														onClick={() => setRevoking(row)}
													>
														<Trash2 size={15} /> 撤销
													</button>
												</div>
											)}
										</td>
									</tr>
								))}
							</tbody>
						</table>
					</div>
				</section>
			)}
			{creating && <CreateTokenDialog onClose={() => setCreating(false)} />}
			{revoking && (
				<div className="modal-layer">
					<div className="confirm-dialog">
						<h3>撤销令牌「{revoking.name}」？</h3>
						<p>撤销后立即失效，使用该令牌的程序将无法再访问账本。</p>
						<div>
							<button onClick={() => setRevoking(null)} disabled={revoke.isPending}>
								取消
							</button>
							<button
								className="danger-solid"
								disabled={revoke.isPending}
								onClick={() => {
									revoke.mutate(revoking.id);
									setRevoking(null);
								}}
							>
								确认撤销
							</button>
						</div>
					</div>
				</div>
			)}
			<div className="tokens-tip">
				<CalendarClock size={15} /> 令牌仅用于 <code>/api/v1</code> 与{" "}
				<code>/api/client/v1</code>；Web 会话（Cookie + CSRF）不受影响。
			</div>
		</section>
	);
}
