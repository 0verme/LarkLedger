import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
	CheckCircle2,
	Laptop,
	LogOut,
	MonitorSmartphone,
	ShieldCheck,
	Smartphone,
	XCircle,
} from "lucide-react";
import {
	sessionApi,
	type CurrentSession,
	type WebSession,
} from "../api";

function DeviceIcon({ device }: { device: string }) {
	if (/移动端/.test(device)) return <Smartphone size={18} />;
	return <Laptop size={18} />;
}

function SessionRow({
	session,
	onRevoke,
	revoking,
}: {
	session: WebSession;
	onRevoke: () => void;
	revoking: boolean;
}) {
	const active = session.revoked_at === null;
	return (
		<li className={`session-row ${active ? "" : "revoked"}`}>
			<div className="session-device">
				<DeviceIcon device={session.device} />
			</div>
			<div className="session-meta">
				<strong>
					{session.device}
					{session.current && <span className="tag current">当前会话</span>}
					{!active && <span className="tag revoked">已注销</span>}
				</strong>
				<span>
					登录于 {new Date(session.created_at).toLocaleString("zh-CN")}
					{" · "}
					最近活跃 {new Date(session.last_seen_at).toLocaleString("zh-CN")}
				</span>
				<span className="session-expiry">
					有效期至 {new Date(session.expires_at).toLocaleString("zh-CN")}
					{session.user_agent ? ` · ${session.user_agent.slice(0, 80)}` : ""}
				</span>
			</div>
			<div className="session-actions">
				{active && !session.current && (
					<button
						type="button"
						className="danger-small"
						disabled={revoking}
						onClick={onRevoke}
					>
						<XCircle size={15} /> 注销
					</button>
				)}
				{active && session.current && (
					<span className="muted-note">
						<CheckCircle2 size={14} /> 本设备
					</span>
				)}
			</div>
		</li>
	);
}

export function SessionsPage() {
	const queryClient = useQueryClient();
	const [confirmOthers, setConfirmOthers] = useState(false);
	const [revokingId, setRevokingId] = useState<string | null>(null);

	const me = useQuery({
		queryKey: ["current-session"],
		queryFn: () => sessionApi.me(),
	});
	const sessions = useQuery({
		queryKey: ["sessions"],
		queryFn: () => sessionApi.list(),
	});
	const revoke = useMutation({
		mutationFn: (sessionId: string) => sessionApi.revoke(sessionId),
		onSuccess: async () => {
			await queryClient.invalidateQueries({ queryKey: ["sessions"] });
		},
	});
	const revokeOthers = useMutation({
		mutationFn: () => sessionApi.revokeOthers(),
		onSuccess: async () => {
			setConfirmOthers(false);
			await queryClient.invalidateQueries({ queryKey: ["sessions"] });
		},
	});
	const logout = useMutation({
		mutationFn: () => sessionApi.logout(),
		onSuccess: () => {
			queryClient.setQueryData(["me"], null);
			window.dispatchEvent(new Event("larkledger:auth-expired"));
		},
	});

	const items = sessions.data?.items ?? [];
	const activeCount = items.filter((item) => item.revoked_at === null).length;

	return (
		<div className="page">
			<div className="page-head">
				<div>
					<p className="eyebrow">SECURITY</p>
					<h1>登录会话</h1>
				</div>
				<div className="page-actions">
					<button
						type="button"
						className="danger"
						disabled={revokeOthers.isPending || activeCount <= 1}
						onClick={() => setConfirmOthers(true)}
					>
						<MonitorSmartphone size={15} /> 注销其他设备
					</button>
					<button
						type="button"
						className="primary"
						aria-label="退出当前会话"
						disabled={logout.isPending}
						onClick={() => logout.mutate()}
					>
						<LogOut size={15} /> 退出登录
					</button>
				</div>
			</div>

			{me.data && (
				<section className="session-summary">
					<div className="avatar large">{me.data.name.slice(0, 1)}</div>
					<div>
						<strong>{me.data.name}</strong>
						<span>
							{me.data.role === "ADMIN" ? "管理员" : "用户"} · 已登录
						</span>
						<span className="session-expiry">
							会话有效期至{" "}
							{new Date(me.data.expires_at).toLocaleString("zh-CN")}
						</span>
					</div>
				</section>
			)}

			<section className="panel">
				<div className="panel-head">
					<h2>
						已登录设备{" "}
						<span className="count-badge">{activeCount}</span>
					</h2>
					<p>
						<ShieldCheck size={14} /> 服务器仅保存会话摘要，浏览器持有
						HttpOnly Cookie 明文
					</p>
				</div>
				{sessions.isLoading ? (
					<p className="muted-note">正在加载会话列表…</p>
				) : items.length === 0 ? (
					<p className="muted-note">暂无会话</p>
				) : (
					<ul className="session-list">
						{items.map((session) => (
							<SessionRow
								key={session.id}
								session={session}
								revoking={revokingId === session.id}
								onRevoke={() => {
									setRevokingId(session.id);
									revoke.mutate(session.id, {
										onSettled: () => setRevokingId(null),
									});
								}}
							/>
						))}
					</ul>
				)}
			</section>

			{confirmOthers && (
				<div className="dialog-overlay" role="dialog" aria-modal="true">
					<div className="edit-dialog">
						<p className="eyebrow">确认操作</p>
						<h3>注销其他所有设备？</h3>
						<p className="dialog-copy">
							当前设备之外的所有登录会话将立即失效，它们需要重新登录。
						</p>
						<div className="dialog-actions">
							<button
								type="button"
								className="ghost"
								onClick={() => setConfirmOthers(false)}
							>
								取消
							</button>
							<button
								type="button"
								className="danger"
								disabled={revokeOthers.isPending}
								onClick={() => revokeOthers.mutate()}
							>
								{revokeOthers.isPending ? "注销中…" : "确认注销"}
							</button>
						</div>
					</div>
				</div>
			)}
		</div>
	);
}

export type { CurrentSession };
