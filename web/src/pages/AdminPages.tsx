import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
	AlertTriangle,
	CheckCircle2,
	ChevronLeft,
	ChevronRight,
	RotateCcw,
	ServerCrash,
	X,
} from "lucide-react";
import {
	api,
	localTime,
	type AdminDeadSummary,
	type AdminEventPage,
	type AdminOutboxPage,
	type DeadLetterActionResponse,
	type DeadLetterDetail,
	type DeadLetterItem,
	type DeadLetterPage,
	type EventReplayResult,
	type HealthSnapshot,
} from "../api";

const eventStatuses = [
	"",
	"received",
	"processing",
	"failed",
	"succeeded",
	"dead",
	"legacy",
];
const replyStatuses = ["", "pending", "sending", "failed", "sent", "dead"];
const statusLabels: Record<string, string> = {
	received: "已接收",
	processing: "处理中",
	failed: "失败",
	succeeded: "成功",
	dead: "Dead",
	legacy: "旧版",
	legacy_succeeded: "旧版",
	pending: "待发送",
	sending: "发送中",
	sent: "已发送",
};

function Pager({
	page,
	pages,
	setPage,
}: {
	page: number;
	pages: number;
	setPage: (page: number) => void;
}) {
	if (pages <= 1) return null;
	return (
		<div className="pagination">
			<button disabled={page <= 1} onClick={() => setPage(page - 1)}>
				<ChevronLeft size={16} /> 上一页
			</button>
			<span>
				{page} / {pages}
			</span>
			<button disabled={page >= pages} onClick={() => setPage(page + 1)}>
				下一页 <ChevronRight size={16} />
			</button>
		</div>
	);
}

export function EventsPage() {
	const [status, setStatus] = useState("");
	const [page, setPage] = useState(1);
	const query = useQuery({
		queryKey: ["admin-events", status, page],
		queryFn: () =>
			api<AdminEventPage>(
				`/admin/events?page=${page}&page_size=25${status ? `&status=${status}` : ""}`,
			),
	});
	return (
		<section>
			<div className="page-heading">
				<div>
					<p className="eyebrow">RELIABLE DELIVERY</p>
					<h2>事件</h2>
				</div>
				<span className="result-count">共 {query.data?.total ?? 0} 项</span>
			</div>
			<div className="admin-filter">
				<label>
					状态
					<select
						value={status}
						onChange={(event) => {
							setStatus(event.target.value);
							setPage(1);
						}}
					>
						{eventStatuses.map((item) => (
							<option value={item} key={item}>
								{item ? statusLabels[item] : "全部状态"}
							</option>
						))}
					</select>
				</label>
			</div>
			<div className="table-panel">
				<div className="table-scroll">
					{query.isLoading ? (
						<div className="table-skeleton">正在加载事件…</div>
					) : query.isError ? (
						<div className="state-panel">
							<h3>事件加载失败</h3>
							<button onClick={() => query.refetch()}>重试</button>
						</div>
					) : !query.data?.items.length ? (
						<div className="empty-ledger">
							<ServerCrash size={28} />
							<h3>没有匹配的事件</h3>
						</div>
					) : (
						<table>
							<thead>
								<tr>
									<th>Event ID</th>
									<th>来源消息</th>
									<th>状态</th>
									<th>尝试</th>
									<th>通道</th>
									<th>接收时间</th>
									<th>处理时间</th>
									<th>错误码</th>
									<th>更新时间</th>
								</tr>
							</thead>
							<tbody>
								{query.data.items.map((item) => (
									<tr key={item.event_id}>
										<td>
											<code>{item.event_id}</code>
										</td>
										<td>{item.source_message_id ?? "—"}</td>
										<td>
											<span className={`ops-status ${item.status}`}>
												{statusLabels[item.status] ?? item.status}
											</span>
										</td>
										<td>{item.attempt_count}</td>
										<td>{item.transport ?? "—"}</td>
										<td>
											{item.received_at ? localTime(item.received_at) : "—"}
										</td>
										<td>{localTime(item.processed_at)}</td>
										<td>{item.last_error_code ?? "—"}</td>
										<td>{localTime(item.updated_at)}</td>
									</tr>
								))}
							</tbody>
						</table>
					)}
				</div>
			</div>
			<Pager page={page} pages={query.data?.pages ?? 0} setPage={setPage} />
		</section>
	);
}

export function OutboxPage() {
	const [status, setStatus] = useState("");
	const [page, setPage] = useState(1);
	const query = useQuery({
		queryKey: ["admin-outbox", status, page],
		queryFn: () =>
			api<AdminOutboxPage>(
				`/admin/outbox?page=${page}&page_size=25${status ? `&status=${status}` : ""}`,
			),
	});
	return (
		<section>
			<div className="page-heading">
				<div>
					<p className="eyebrow">TRANSACTIONAL OUTBOX</p>
					<h2>回复队列</h2>
				</div>
				<span className="result-count">共 {query.data?.total ?? 0} 项</span>
			</div>
			<div className="admin-filter">
				<label>
					状态
					<select
						value={status}
						onChange={(event) => {
							setStatus(event.target.value);
							setPage(1);
						}}
					>
						{replyStatuses.map((item) => (
							<option value={item} key={item}>
								{item ? statusLabels[item] : "全部状态"}
							</option>
						))}
					</select>
				</label>
			</div>
			<div className="table-panel">
				<div className="table-scroll">
					{query.isLoading ? (
						<div className="table-skeleton">正在加载回复队列…</div>
					) : query.isError ? (
						<div className="state-panel">
							<h3>回复队列加载失败</h3>
							<button onClick={() => query.refetch()}>重试</button>
						</div>
					) : !query.data?.items.length ? (
						<div className="empty-ledger">
							<CheckCircle2 size={28} />
							<h3>没有匹配的回复</h3>
						</div>
					) : (
						<table>
							<thead>
								<tr>
									<th>ID</th>
									<th>Event ID</th>
									<th>类型</th>
									<th>顺序</th>
									<th>状态</th>
									<th>尝试</th>
									<th>创建时间</th>
									<th>发送时间</th>
									<th>错误码</th>
								</tr>
							</thead>
							<tbody>
								{query.data.items.map((item) => (
									<tr key={item.id}>
										<td>
											<code>{item.id.slice(0, 8)}</code>
										</td>
										<td>{item.event_id ?? "—"}</td>
										<td>{item.reply_type}</td>
										<td>{item.sequence}</td>
										<td>
											<span className={`ops-status ${item.status}`}>
												{statusLabels[item.status] ?? item.status}
											</span>
										</td>
										<td>{item.attempt_count}</td>
										<td>{localTime(item.created_at)}</td>
										<td>{item.sent_at ? localTime(item.sent_at) : "—"}</td>
										<td>{item.last_error_code ?? "—"}</td>
									</tr>
								))}
							</tbody>
						</table>
					)}
				</div>
			</div>
			<Pager page={page} pages={query.data?.pages ?? 0} setPage={setPage} />
		</section>
	);
}

export function DeadPage() {
	const client = useQueryClient();
	const query = useQuery({
		queryKey: ["admin-dead"],
		queryFn: () => api<AdminDeadSummary>("/admin/dead"),
	});
	const [selectedEvent, setSelectedEvent] = useState<string | null>(null);
	const [reason, setReason] = useState("");
	const [preflight, setPreflight] = useState<EventReplayResult | null>(null);
	const [confirming, setConfirming] = useState(false);
	const [notice, setNotice] = useState("");
	const replayEvent = useMutation({
		mutationFn: (execute: boolean) =>
			api<EventReplayResult>(
				`/admin/events/${encodeURIComponent(selectedEvent!)}/replay`,
				{
					method: "POST",
					body: JSON.stringify({
						reason,
						execute,
						confirmation_event_id: execute ? selectedEvent : null,
					}),
				},
			),
		onSuccess: async (result) => {
			setPreflight(result);
			if (result.mode === "execute") {
				setNotice("事件已安全重新入队");
				setConfirming(false);
				await client.invalidateQueries({ queryKey: ["admin-dead"] });
			}
		},
	});
	const replayResult = useMutation({
		mutationFn: (id: string) =>
			api(`/admin/outbox/${id}/replay`, { method: "POST" }),
		onSuccess: async () => {
			setNotice("已有结果已重新入队；不会重新执行业务");
			await client.invalidateQueries({ queryKey: ["admin-dead"] });
		},
	});
	const openEvent = (eventId: string) => {
		setSelectedEvent(eventId);
		setReason("");
		setPreflight(null);
		setConfirming(false);
	};
	if (query.isLoading)
		return <div className="table-skeleton">正在加载 Dead 队列…</div>;
	if (query.isError || !query.data)
		return (
			<div className="state-panel">
				<h3>Dead 队列加载失败</h3>
				<button onClick={() => query.refetch()}>重试</button>
			</div>
		);
	return (
		<section>
			{notice && <div className="toast">{notice}</div>}
			<div className="page-heading">
				<div>
					<p className="eyebrow">GUARDED RECOVERY</p>
					<h2>Dead / Replay</h2>
				</div>
			</div>
			<div className="replay-warning">
				<AlertTriangle size={20} />
				<div>
					<strong>结果重发不会重新执行业务</strong>
					<p>
						事件重放可能重新执行业务，必须先通过安全预检并二次确认。此处不提供批量重放。
					</p>
				</div>
			</div>
			<div className="dead-grid">
				<section className="panel">
					<div className="panel-title">
						<h3>Dead Events</h3>
						<span>{query.data.event_count}</span>
					</div>
					{query.data.latest_events.length ? (
						query.data.latest_events.map((item) => (
							<button
								className="dead-row"
								key={item.event_id}
								onClick={() => openEvent(item.event_id)}
							>
								<span>
									<code>{item.event_id}</code>
									<small>{item.last_error_code ?? "未知错误"}</small>
								</span>
								<RotateCcw size={16} />
							</button>
						))
					) : (
						<p className="muted-empty">没有 Dead Event</p>
					)}
				</section>
				<section className="panel">
					<div className="panel-title">
						<h3>Dead Replies</h3>
						<span>{query.data.reply_count}</span>
					</div>
					{query.data.latest_replies.length ? (
						query.data.latest_replies.map((item) => (
							<button
								className="dead-row"
								key={item.id}
								disabled={replayResult.isPending}
								onClick={() => replayResult.mutate(item.id)}
							>
								<span>
									<code>{item.id.slice(0, 8)}</code>
									<small>
										{item.reply_type} · {item.last_error_code ?? "未知错误"}
									</small>
								</span>
								<RotateCcw size={16} />
							</button>
						))
					) : (
						<p className="muted-empty">没有 Dead Reply</p>
					)}
				</section>
			</div>
			{selectedEvent && (
				<>
					<button
						className="drawer-scrim"
						aria-label="关闭重放预检"
						onClick={() => setSelectedEvent(null)}
					/>
					<aside className="entry-drawer replay-drawer">
						<button
							className="drawer-close"
							aria-label="关闭"
							onClick={() => setSelectedEvent(null)}
						>
							<X />
						</button>
						<p className="eyebrow">EVENT REPLAY</p>
						<h2>{selectedEvent}</h2>
						<label className="reason-field">
							操作原因
							<textarea
								value={reason}
								maxLength={512}
								onChange={(event) => setReason(event.target.value)}
								placeholder="记录本次重放的调查结论（至少 3 个字符）"
							/>
						</label>
						<button
							className="primary-small"
							disabled={reason.trim().length < 3 || replayEvent.isPending}
							onClick={() => replayEvent.mutate(false)}
						>
							先执行 Dry Run
						</button>
						{replayEvent.error && (
							<p className="form-error">{replayEvent.error.message}</p>
						)}
						{preflight && (
							<div
								className={`preflight ${preflight.preflight.eligible ? "safe" : "blocked"}`}
							>
								<h3>
									{preflight.preflight.eligible
										? "安全预检通过"
										: "安全预检已阻断"}
								</h3>
								<dl>
									<div>
										<dt>当前状态</dt>
										<dd>{preflight.preflight.status ?? "不存在"}</dd>
									</div>
									<div>
										<dt>已有业务结果</dt>
										<dd>
											{preflight.preflight.business_result_committed ||
											preflight.preflight.ledger_entry_count
												? "是"
												: "否"}
										</dd>
									</div>
									<div>
										<dt>已有 Outbox</dt>
										<dd>{preflight.preflight.outbox_count}</dd>
									</div>
									<div>
										<dt>Lease</dt>
										<dd>{preflight.preflight.lease_state}</dd>
									</div>
								</dl>
								{preflight.preflight.reason_codes.length > 0 && (
									<p>阻断原因：{preflight.preflight.reason_codes.join("、")}</p>
								)}
								{preflight.preflight.eligible && (
									<button
										className="danger-solid replay-execute"
										onClick={() => setConfirming(true)}
									>
										继续事件重放
									</button>
								)}
							</div>
						)}
					</aside>
				</>
			)}
			{confirming && selectedEvent && (
				<div className="modal-layer">
					<div className="confirm-dialog">
						<h3>二次确认事件重放？</h3>
						<p>
							这会让 <code>{selectedEvent}</code>{" "}
							重新进入业务处理队列。系统会在事务内再次执行安全预检。
						</p>
						<div>
							<button onClick={() => setConfirming(false)}>返回</button>
							<button
								className="danger-solid"
								disabled={replayEvent.isPending}
								onClick={() => replayEvent.mutate(true)}
							>
								明确执行事件重放
							</button>
						</div>
					</div>
				</div>
			)}
		</section>
	);
}

export function HealthPage() {
	const query = useQuery({
		queryKey: ["admin-health"],
		queryFn: () => api<HealthSnapshot>("/admin/health"),
		refetchInterval: 15000,
	});
	if (query.isLoading)
		return (
			<div className="page-skeleton">
				<div />
				<div />
			</div>
		);
	if (query.isError || !query.data)
		return (
			<div className="state-panel">
				<h3>健康状态暂不可用</h3>
				<button onClick={() => query.refetch()}>重试</button>
			</div>
		);
	const labels: Record<string, string> = {
		application: "Application",
		database: "Database",
		migration: "Migration",
		event_worker: "Event Worker",
		reply_worker: "Reply Worker",
		cleanup_worker: "Cleanup Worker",
		receiver: "Receiver",
	};
	return (
		<section>
			<div className="page-heading">
				<div>
					<p className="eyebrow">SYSTEM HEALTH</p>
					<h2>系统状态</h2>
				</div>
				<span className={`health-overall ${query.data.status}`}>
					● {query.data.status === "ready" ? "正常" : "异常"}
				</span>
			</div>
			<div className="health-list">
				{Object.entries(query.data.checks).map(([name, check]) => (
					<article key={name}>
						<span className={`health-dot ${check.status}`} />
						<div>
							<strong>{labels[name] ?? name}</strong>
							<small>
								{check.reason ??
									(check.current
										? `Revision ${check.current}`
										: check.running
											? "Running"
											: check.status)}
							</small>
						</div>
						<b>{check.status}</b>
					</article>
				))}
			</div>
		</section>
	);
}

const reasonLabels: Record<string, string> = {
	network: "网络",
	timeout: "超时",
	rate_limited: "限流",
	authentication: "认证失败",
	permission: "无权限",
	remote_not_found: "远端不存在",
	remote_rejected: "远端拒绝",
	invalid_payload: "无效载荷",
	serialization: "序列化错误",
	database: "数据库",
	business_conflict: "业务冲突",
	expired: "已过期",
	unknown: "未知",
};
const sourceLabels: Record<string, string> = {
	events: "事件",
	outbox: "回复队列",
	pending_commands: "待确认",
};
const stateLabels: Record<string, string> = {
	pending: "待处理",
	retry: "重试中",
	dead: "Dead",
	resolved: "已解决",
	terminal: "已终止",
};

function DeadLetterSummaryCards({ items }: { items: DeadLetterItem[] }) {
	const bySource = { events: 0, outbox: 0, pending_commands: 0 };
	let retryable = 0;
	let review = 0;
	for (const item of items) {
		bySource[item.source] += 1;
		if (item.retryable) retryable += 1;
		if (item.requires_manual_review) review += 1;
	}
	return (
		<div className="dead-grid">
			{(Object.keys(bySource) as Array<keyof typeof bySource>).map((source) => (
				<section className="panel" key={source}>
					<div className="panel-title">
						<h3>{sourceLabels[source]}</h3>
						<span>{bySource[source]}</span>
					</div>
					<p className="muted-empty">
						{source === "events"
							? "事件"
							: source === "outbox"
								? "回复"
								: "确认"}{" "}
						dead / failed
					</p>
				</section>
			))}
			<section className="panel">
				<div className="panel-title">
					<h3>可重试</h3>
					<span>{retryable}</span>
				</div>
				<p className="muted-empty">transient / replay-safe 候选</p>
			</section>
			<section className="panel">
				<div className="panel-title">
					<h3>需人工审查</h3>
					<span>{review}</span>
				</div>
				<p className="muted-empty">不可盲目 replay</p>
			</section>
		</div>
	);
}

export function DeadLettersPage() {
	const client = useQueryClient();
	const [source, setSource] = useState("");
	const [state, setState] = useState("");
	const [reason, setReason] = useState("");
	const [retryableOnly, setRetryableOnly] = useState(false);
	const [page, setPage] = useState(1);
	const [selected, setSelected] = useState<DeadLetterDetail | null>(null);
	const [actionReason, setActionReason] = useState("");
	const [notice, setNotice] = useState("");
	const query = useQuery({
		queryKey: ["dead-letters", source, state, reason, retryableOnly, page],
		queryFn: () =>
			api<DeadLetterPage>(
				`/admin/dead-letters?page=${page}&page_size=50&sort=dead_at${source ? `&source=${source}` : ""}${state ? `&state=${state}` : ""}${reason ? `&reason=${reason}` : ""}${retryableOnly ? "&retryable=true" : ""}`,
			),
	});
	const invalidate = async () => {
		await client.invalidateQueries({ queryKey: ["dead-letters"] });
	};
	const replay = useMutation({
		mutationFn: () =>
			api<DeadLetterActionResponse>(
				`/admin/dead-letters/${selected!.source}/${encodeURIComponent(selected!.id)}/replay`,
				{ method: "POST", body: JSON.stringify({ reason: actionReason }) },
			),
		onSuccess: async (result) => {
			setNotice(result.message);
			await invalidate();
			if (selected)
				setSelected({
					...selected,
					state: "pending",
					status: "pending",
					resolved: false,
				});
		},
	});
	const resolve = useMutation({
		mutationFn: () =>
			api<DeadLetterActionResponse>(
				`/admin/dead-letters/${selected!.source}/${encodeURIComponent(selected!.id)}/resolve`,
				{ method: "POST", body: JSON.stringify({ reason: actionReason }) },
			),
		onSuccess: async (result) => {
			setNotice(result.message);
			await invalidate();
			if (selected) setSelected({ ...selected, resolved: true });
		},
	});
	const openDetail = async (item: DeadLetterItem) => {
		setActionReason("");
		try {
			const detail = await api<DeadLetterDetail>(
				`/admin/dead-letters/${item.source}/${encodeURIComponent(item.id)}`,
			);
			setSelected(detail);
		} catch {
			setSelected({
				...item,
				event_id: null,
				message_id: null,
				reply_type: null,
				transport: null,
				lease_owner: null,
				lease_expires_at: null,
				remote_message_id: null,
				next_attempt_at: null,
				updated_at: null,
				audit: [],
			});
		}
	};
	return (
		<section>
			{notice && <div className="toast">{notice}</div>}
			<div className="page-heading">
				<div>
					<p className="eyebrow">BACKLOG HYGIENE</p>
					<h2>Dead Letters</h2>
				</div>
				<span className="result-count">共 {query.data?.total ?? 0} 项</span>
			</div>
			<div className="replay-warning">
				<AlertTriangle size={20} />
				<div>
					<strong>重放只重新投递，不重新执行业务</strong>
					<p>
						事件重放会重新进入业务队列，必须先通过安全预检。terminal 或
						replay_safe=false 的记录不可直接重放，应使用“解决”标记审计。
					</p>
				</div>
			</div>
			<DeadLetterSummaryCards items={query.data?.items ?? []} />
			<div className="admin-filter">
				<label>
					来源
					<select
						value={source}
						onChange={(event) => {
							setSource(event.target.value);
							setPage(1);
						}}
					>
						<option value="">全部来源</option>
						<option value="events">事件</option>
						<option value="outbox">回复队列</option>
						<option value="pending_commands">待确认</option>
					</select>
				</label>
				<label>
					状态
					<select
						value={state}
						onChange={(event) => {
							setState(event.target.value);
							setPage(1);
						}}
					>
						<option value="">Dead / Failed</option>
						<option value="pending">待处理</option>
						<option value="retry">重试中</option>
						<option value="dead">Dead</option>
						<option value="terminal">已终止</option>
					</select>
				</label>
				<label>
					原因分类
					<select
						value={reason}
						onChange={(event) => {
							setReason(event.target.value);
							setPage(1);
						}}
					>
						<option value="">全部分类</option>
						{Object.entries(reasonLabels).map(([key, label]) => (
							<option value={key} key={key}>
								{label}
							</option>
						))}
					</select>
				</label>
				<label className="check-row">
					<input
						type="checkbox"
						checked={retryableOnly}
						onChange={(event) => {
							setRetryableOnly(event.target.checked);
							setPage(1);
						}}
					/>
					仅可重试
				</label>
			</div>
			<div className="table-panel">
				<div className="table-scroll">
					{query.isLoading ? (
						<div className="table-skeleton">正在加载 Dead Letters…</div>
					) : query.isError ? (
						<div className="state-panel">
							<h3>加载失败</h3>
							<button onClick={() => query.refetch()}>重试</button>
						</div>
					) : !query.data?.items.length ? (
						<div className="empty-ledger">
							<CheckCircle2 size={28} />
							<h3>没有匹配的 Dead Letter</h3>
						</div>
					) : (
						<table>
							<thead>
								<tr>
									<th>来源</th>
									<th>ID</th>
									<th>状态</th>
									<th>尝试</th>
									<th>原因</th>
									<th>可重放性</th>
									<th>Dead 时间</th>
									<th>操作</th>
								</tr>
							</thead>
							<tbody>
								{query.data.items.map((item) => (
									<tr key={`${item.source}:${item.id}`}>
										<td>{sourceLabels[item.source] ?? item.source}</td>
										<td>
											<code>
												{item.source === "events"
													? item.id
													: item.id.slice(0, 8)}
											</code>
										</td>
										<td>
											<span className={`ops-status ${item.status}`}>
												{stateLabels[item.state] ?? item.status}
												{item.resolved ? " · 已解决" : ""}
											</span>
										</td>
										<td>{item.attempts}</td>
										<td>
											{reasonLabels[item.reason_category] ??
												item.reason_category}
										</td>
										<td>
											{item.terminal ? (
												<span className="ops-status dead">不可重放</span>
											) : item.replay_safe ? (
												<span className="ops-status succeeded">可安全重放</span>
											) : item.requires_manual_review ? (
												<span className="ops-status failed">需审查</span>
											) : (
												<span className="ops-status failed">不安全</span>
											)}
										</td>
										<td>{item.dead_at ? localTime(item.dead_at) : "—"}</td>
										<td>
											<button
												className="link-button"
												onClick={() => openDetail(item)}
											>
												详情
											</button>
										</td>
									</tr>
								))}
							</tbody>
						</table>
					)}
				</div>
			</div>
			<Pager page={page} pages={query.data?.pages ?? 0} setPage={setPage} />
			{selected && (
				<>
					<button
						className="drawer-scrim"
						aria-label="关闭"
						onClick={() => setSelected(null)}
					/>
					<aside className="entry-drawer replay-drawer">
						<button
							className="drawer-close"
							aria-label="关闭"
							onClick={() => setSelected(null)}
						>
							<X />
						</button>
						<p className="eyebrow">DEAD LETTER DETAIL</p>
						<h2>
							{selected.source}:{" "}
							{selected.source === "events"
								? selected.id
								: selected.id.slice(0, 8)}
						</h2>
						<div className="detail-grid">
							<dl>
								<div>
									<dt>状态</dt>
									<dd>
										{stateLabels[selected.state] ?? selected.status}
										{selected.resolved ? " · 已解决" : ""}
									</dd>
								</div>
								<div>
									<dt>原因分类</dt>
									<dd>
										{reasonLabels[selected.reason_category] ??
											selected.reason_category}
									</dd>
								</div>
								<div>
									<dt>尝试次数</dt>
									<dd>{selected.attempts}</dd>
								</div>
								<div>
									<dt>错误摘要</dt>
									<dd>{selected.last_error_summary ?? "—"}</dd>
								</div>
								<div>
									<dt>重放评估</dt>
									<dd>
										{selected.terminal
											? "Terminal（不可重放）"
											: selected.retryable
												? "可重试"
												: "不可重试"}
										{selected.replay_safe ? " · 安全" : " · 有副作用风险"}
									</dd>
								</div>
								<div>
									<dt>类型</dt>
									<dd>{selected.payload_summary}</dd>
								</div>
							</dl>
						</div>
						<label className="reason-field">
							操作原因（至少 3 个字符）
							<textarea
								value={actionReason}
								maxLength={512}
								onChange={(event) => setActionReason(event.target.value)}
								placeholder="记录调查结论 / 处理依据"
							/>
						</label>
						<div className="action-row">
							<button
								className="primary-small"
								disabled={actionReason.trim().length < 3 || resolve.isPending}
								onClick={() => resolve.mutate()}
							>
								{resolve.isPending ? "记录中…" : "解决（不重放）"}
							</button>
							<button
								className="danger-solid"
								disabled={
									actionReason.trim().length < 3 ||
									selected.terminal ||
									!selected.replay_safe ||
									replay.isPending
								}
								onClick={() => replay.mutate()}
								title={
									selected.terminal
										? "该记录不可重放"
										: !selected.replay_safe
											? "存在副作用风险，不允许一键重放"
											: "重新入队，由 Worker 投递"
								}
							>
								{replay.isPending ? "入队中…" : "重放"}
							</button>
						</div>
						{selected.audit.length > 0 && (
							<section className="panel">
								<div className="panel-title">
									<h3>审计历史</h3>
								</div>
								<table className="audit-table">
									<thead>
										<tr>
											<th>动作</th>
											<th>操作者</th>
											<th>前后状态</th>
											<th>原因</th>
											<th>时间</th>
										</tr>
									</thead>
									<tbody>
										{selected.audit.map((entry, index) => (
											<tr key={index}>
												<td>
													{entry.action === "replay"
														? "重放"
														: entry.action === "resolve"
															? "解决"
															: entry.action}
												</td>
												<td>{entry.operator}</td>
												<td>
													{entry.before_status ?? "—"} →{" "}
													{entry.after_status ?? "—"}
												</td>
												<td>{entry.reason ?? "—"}</td>
												<td>
													{entry.created_at ? localTime(entry.created_at) : "—"}
												</td>
											</tr>
										))}
									</tbody>
								</table>
							</section>
						)}
						{replay.error && (
							<p className="form-error">{replay.error.message}</p>
						)}
						{resolve.error && (
							<p className="form-error">{resolve.error.message}</p>
						)}
					</aside>
				</>
			)}
		</section>
	);
}
