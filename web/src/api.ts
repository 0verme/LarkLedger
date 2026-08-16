export type Me = {
	open_id: string;
	name: string;
	avatar_url: string;
	role: "USER" | "ADMIN";
	expires_at: string;
};

export type Entry = {
	id: string;
	short_id: string;
	amount: string;
	currency: string;
	direction: "expense" | "income";
	category: string;
	note: string;
	occurred_at: string;
	source_type: string;
	created_at: string;
	updated_at: string;
	deleted_at: string | null;
	account_id: string;
	account_name: string | null;
	payer_user_id: string;
	payer_name: string | null;
};

export type EntryPage = {
	items: Entry[];
	page: number;
	page_size: number;
	total: number;
	pages: number;
};

export type EntryDetail = {
	entry: Entry;
	revisions: Array<{
		id: string;
		change_type: "update" | "delete" | "restore";
		before: Record<string, unknown>;
		after: Record<string, unknown>;
		created_at: string;
	}>;
};

export type DashboardData = {
	month_income: string;
	month_expense: string;
	month_balance: string;
	budget_usage_rate: string | null;
	pending_count: number;
	recent_entries: Entry[];
	trend: Array<{
		period: string;
		income: string;
		expense: string;
		balance: string;
	}>;
	categories: Array<{ category: string; amount: string; ratio: string }>;
};

export type AccountBalance = {
	account_id: string;
	ledger_id: string;
	account_name: string;
	account_type: "cash" | "asset" | "liability";
	currency: string;
	opening_balance: string;
	current_balance: string;
	archived: boolean;
};

export type Account = {
	id: string;
	ledger_id: string;
	name: string;
	type: "cash" | "asset" | "liability";
	subtype: string | null;
	provider: string | null;
	currency: string;
	opening_balance: string;
	status: "active" | "archived";
	is_default: boolean;
	visibility: "shared" | "private";
	owner_user_id: string | null;
	created_at: string;
	updated_at: string;
};

export type AccountList = { items: Account[] };

export type Transfer = {
	id: string;
	ledger_id: string;
	from_account_id: string;
	to_account_id: string;
	amount: string;
	currency: string;
	note: string;
	occurred_at: string;
	reversed_at: string | null;
	created_at: string;
	updated_at: string;
};

export type TransferPage = {
	items: Transfer[];
	page: number;
	page_size: number;
	total: number;
	pages: number;
};

export type TransferDetail = {
	transfer: Transfer;
	revisions: Array<{
		id: string;
		change_type: "create" | "update" | "reverse";
		before: Record<string, unknown>;
		after: Record<string, unknown>;
		created_at: string;
	}>;
};

export type AssetSummary = {
	ledger_id: string;
	currency: string;
	total_assets: string;
	total_liabilities: string;
	net_assets: string;
	accounts: AccountBalance[];
};

export type Ledger = {
	id: string;
	name: string;
	is_default: boolean;
	is_current: boolean;
	currency: string;
	timezone: string;
	kind: "personal" | "household_shared" | "business";
	household_id: string | null;
};

export type LedgerList = { items: Ledger[] };

export type HouseholdMember = {
	user_id: string;
	display_name: string;
	role: "owner" | "member";
	joined_at: string | null;
};
export type Household = {
	id: string;
	name: string;
	owner_user_id: string;
	role: "owner" | "member";
	status: string;
	ledger: Ledger;
	created_at: string;
	updated_at: string;
	members: HouseholdMember[] | null;
};
export type HouseholdList = { items: Household[] };
export type HouseholdInvitation = {
	id: string;
	invitation_code: string;
	household_id: string;
	household_name: string;
	target_user_id: string;
	status: string;
	expires_at: string;
	created_at: string;
};

export type PendingSummary = {
	confirmation_id: string;
	status: string;
	source_type: string;
	transport: string;
	risk_reason: string;
	entries_total: number;
	income_total: string;
	expense_total: string;
	currency: string;
	created_at: string;
	expires_at: string;
	completed_at: string | null;
};

export type PendingPage = {
	items: PendingSummary[];
	page: number;
	page_size: number;
	total: number;
	pages: number;
};

export type PendingDetail = {
	pending: PendingSummary;
	preview: {
		items: Array<{
			index: number | null;
			direction: string;
			amount: string;
			currency: string;
			category: string;
			occurred_at: string;
			note: string;
			duplicate_of: string | null;
		}>;
		budgets: Array<{ category: string; amount: string; currency: string }>;
		anomalies: string[];
	};
};

// P39 — Unified AI Entry: canonical outcome envelope from
// POST /api/web/v1/ai/entries. The UI branches on `status`, never on the
// free-form `message`.
export type AIEntryStatus =
	| "executed"
	| "confirmation_required"
	| "clarification_required"
	| "query_result"
	| "rejected"
	| "error";

export type AIEntryResult = {
	status: AIEntryStatus;
	message: string;
	request_id: string;
	replayed: boolean;
	operation: string | null;
	resource_id: string | null;
	amount: string | null;
	direction: "expense" | "income" | null;
	category: string | null;
	account: string | null;
	occurred_at: string | null;
	pending_command_id: string | null;
	confirmation_code: string | null;
	risk: string | null;
	expires_at: string | null;
	preview: Record<string, unknown> | null;
	missing_fields: string[];
};

export type PendingActionResponse = { message: string; pending: PendingDetail };

export type AdminEvent = {
	event_id: string;
	source_message_id: string | null;
	status: string;
	attempt_count: number;
	transport: string | null;
	received_at: string | null;
	processed_at: string;
	last_error_code: string | null;
	updated_at: string;
};

export type AdminOutbox = {
	id: string;
	event_id: string | null;
	reply_type: string;
	sequence: number;
	status: string;
	attempt_count: number;
	created_at: string;
	sent_at: string | null;
	last_error_code: string | null;
};

export type AdminEventPage = {
	items: AdminEvent[];
	page: number;
	page_size: number;
	total: number;
	pages: number;
};
export type AdminOutboxPage = {
	items: AdminOutbox[];
	page: number;
	page_size: number;
	total: number;
	pages: number;
};
export type AdminDeadSummary = {
	event_count: number;
	reply_count: number;
	latest_events: AdminEvent[];
	latest_replies: AdminOutbox[];
};
export type DeadLetterItem = {
	id: string;
	source: "events" | "outbox" | "pending_commands";
	status: string;
	state: "pending" | "retry" | "dead" | "resolved" | "terminal";
	created_at: string | null;
	dead_at: string | null;
	attempts: number;
	reason_category: string;
	retryable: boolean;
	replay_safe: boolean;
	requires_manual_review: boolean;
	terminal: boolean;
	payload_summary: string;
	last_error_summary: string | null;
	resolved: boolean;
};
export type DeadLetterPage = {
	items: DeadLetterItem[];
	page: number;
	page_size: number;
	total: number;
	pages: number;
};
export type DeadLetterAuditEntry = {
	action: string;
	operator: string;
	reason: string | null;
	before_status: string | null;
	after_status: string | null;
	error_code: string | null;
	request_id: string | null;
	created_at: string | null;
};
export type DeadLetterDetail = DeadLetterItem & {
	event_id: string | null;
	message_id: string | null;
	reply_type: string | null;
	transport: string | null;
	lease_owner: string | null;
	lease_expires_at: string | null;
	remote_message_id: string | null;
	next_attempt_at: string | null;
	updated_at: string | null;
	audit: DeadLetterAuditEntry[];
};
export type DeadLetterActionResponse = {
	source: string;
	target_id: string;
	action: string;
	outcome: string;
	before_status: string | null;
	after_status: string | null;
	audit_id: string | null;
	message: string;
};
export type ReplayPreflight = {
	event_found: boolean;
	eligible: boolean;
	status: string | null;
	business_result_committed: boolean;
	outbox_count: number;
	ledger_entry_count: number;
	batch_risk: string;
	lease_state: string;
	reason_codes: string[];
	recommended_action: string;
};
export type EventReplayResult = {
	mode: string;
	outcome: string;
	audit_id: string | null;
	resulting_status: string | null;
	preflight: ReplayPreflight;
};
export type HealthSnapshot = {
	status: string;
	checks: Record<
		string,
		{
			status: string;
			reason?: string;
			current?: string;
			enabled?: boolean;
			running?: boolean;
			last_error_code?: string | null;
		}
	>;
};
export type SafeSystemConfig = {
	version: string;
	event_mode: string;
	timezone: string;
	currency: string;
	worker_enabled: boolean;
	reply_worker_enabled: boolean;
	cleanup_worker_enabled: boolean;
	pending_enabled: boolean;
	ai_provider: string;
	ai_model: string;
	ai_api_key_configured: boolean;
	lark_app_secret_configured: boolean;
	dashboard_base_url: string;
	session_ttl_seconds: number;
	secure_cookie: boolean;
};

export type AnalyticsSummary = {
	range_start: string;
	range_end: string;
	income: string;
	expense: string;
	balance: string;
	entry_count: number;
};
export type AnalyticsTrendPoint = {
	period: string;
	income: string;
	expense: string;
	balance: string;
};
export type AnalyticsCategory = {
	category: string;
	amount: string;
	ratio: string;
};
export type AnalyticsMonthlyPoint = {
	period: string;
	income: string;
	expense: string;
	balance: string;
};
export type AnalyticsOverview = {
	summary: AnalyticsSummary;
	trend: AnalyticsTrendPoint[];
	categories: AnalyticsCategory[];
};
export type BudgetStatus = "none" | "normal" | "warning" | "exceeded";
export type BudgetItem = {
	category: string;
	amount: string | null;
	spent: string;
	remaining: string | null;
	usage_rate: string | null;
	status: BudgetStatus;
};
export type BudgetOverview = {
	currency: string;
	period: string;
	total_budget: string | null;
	total_spent: string;
	total_remaining: string | null;
	usage_rate: string | null;
	status: BudgetStatus;
	total_limit_set: boolean;
	allocated: string;
	unallocated: string | null;
	items: BudgetItem[];
};

export type MemberStats = {
	user_id: string;
	display_name: string;
	alias: string | null;
	role: "owner" | "member";
	expense_total: string;
	income_total: string;
	transaction_count: number;
};
export type OverviewBudget = {
	total_budget: string | null;
	total_spent: string;
	total_remaining: string | null;
	usage_rate: string | null;
	status: BudgetStatus;
};
export type AccountBalanceSummary = {
	currency: string;
	total_assets: string;
	total_liabilities: string;
	net_assets: string;
	account_count: number;
};
export type UpcomingRecurringItem = {
	rule_id: string;
	transaction_type: "expense" | "income";
	amount: string;
	currency: string;
	category: string;
	description: string;
	frequency: RecurringFrequency;
	next_occurrence: string;
	account_name: string | null;
};
export type HouseholdOverview = {
	ledger_id: string;
	ledger_name: string;
	ledger_kind: "personal" | "household_shared" | "business";
	period: string;
	income_total: string;
	expense_total: string;
	net_total: string;
	budget: OverviewBudget;
	account_balance_summary: AccountBalanceSummary;
	member_contributions: MemberStats[];
	top_categories: Array<{ category: string; amount: string; ratio: string }>;
	upcoming_recurring: UpcomingRecurringItem[];
	recent_transactions: Entry[];
};
export type ReportData = {
	range_start: string;
	range_end: string;
	currency: string;
	income_total: string;
	expense_total: string;
	balance: string;
	entry_count: number;
	categories: Array<{ category: string; amount: string }>;
	trend: Array<{ period: string; amount: string }>;
	trend_granularity: "day" | "month";
};

export type RecurringFrequency = "weekly" | "monthly" | "yearly";
export type RecurringRule = {
	id: string;
	ledger_id: string;
	transaction_type: "expense" | "income";
	amount: string;
	currency: string;
	category: string;
	description: string;
	frequency: RecurringFrequency;
	interval: number;
	next_occurrence: string;
	status: "active" | "paused" | "disabled";
	account_id: string;
	account_name: string | null;
	pending_count: number;
	created_at: string;
	updated_at: string;
};
export type RecurringRuleList = { items: RecurringRule[] };
export type RecurringRuleCreateInput = {
	transaction_type: "expense" | "income";
	amount: string;
	currency: string | null;
	category: string;
	description: string;
	frequency: RecurringFrequency;
	interval: number;
	next_occurrence: string;
	account_id: string;
};
export type RecurringRuleUpdateInput = Partial<
	Omit<RecurringRuleCreateInput, "transaction_type" | "interval">
> & { transaction_type?: "expense" | "income"; interval?: number };

export type GoalAccountBindingItem = {
	account_id: string;
	account_name: string | null;
	currency: string;
};
export type Goal = {
	id: string;
	ledger_id: string;
	name: string;
	description: string;
	goal_type: "savings";
	target_amount: string;
	currency: string;
	target_date: string | null;
	status: "active" | "completed" | "archived";
	created_by_user_id: string;
	created_at: string;
	updated_at: string;
	account_bindings: GoalAccountBindingItem[];
	current_amount: string;
	remaining_amount: string | null;
	progress_percent: string | null;
	is_target_reached: boolean;
};
export type GoalList = { items: Goal[] };
export type GoalCreateInput = {
	name: string;
	description?: string;
	target_amount: string;
	currency?: string | null;
	target_date?: string | null;
	account_ids: string[];
};
export type GoalUpdateInput = Partial<GoalCreateInput> & {
	status?: "active" | "completed" | "archived";
};
export type GoalProgress = {
	goal_id: string;
	name: string;
	target_amount: string;
	current_amount: string;
	remaining_amount: string;
	progress_ratio: string;
	progress_percent: string;
	currency: string;
	target_date: string | null;
	days_remaining: number | null;
	is_target_reached: boolean;
	monthly_saving_rate: string | null;
	estimated_months_to_goal: string | null;
	projected_shortfall_at_target_date: string | null;
};
export type InsightSeverity = "info" | "attention" | "warning";
export type Insight = {
	key: string;
	type: string;
	severity: InsightSeverity;
	title: string;
	summary: string;
	metric: Record<string, string>;
	period: string;
	related_category: string | null;
	related_goal: string | null;
	related_goal_name: string | null;
	related_account: string | null;
	generated_at: string;
	explanation: string | null;
};
export type InsightList = { insights: Insight[] };

export type ClientCredentialScope =
	| "ledger:read"
	| "ledger:write"
	| "pending:write";
export type ClientCredential = {
	id: string;
	name: string;
	token_prefix: string;
	scopes: string[];
	created_at: string;
	last_used_at: string | null;
	expires_at: string | null;
	revoked_at: string | null;
};
export type ClientCredentialCreated = ClientCredential & { token: string };
export type ClientCredentialList = { items: ClientCredential[] };

// P37 — human session views. These deliberately carry no digest, cookie or
// raw secret; the server never returns a session credential after creation.
export type WebSession = {
	id: string;
	created_at: string;
	last_seen_at: string;
	expires_at: string;
	revoked_at: string | null;
	current: boolean;
	device: string;
	user_agent: string | null;
};
export type SessionList = {
	items: WebSession[];
	current_session_id: string;
};
export type CurrentSession = {
	session_id: string;
	open_id: string;
	name: string;
	avatar_url: string;
	role: string;
	expires_at: string;
};

export const sessionApi = {
	me: () => api<CurrentSession>("/auth/session"),
	list: () => api<SessionList>("/auth/sessions"),
	revoke: (sessionId: string) =>
		api<void>(`/auth/sessions/${sessionId}`, { method: "DELETE" }),
	revokeOthers: () =>
		api<void>("/auth/sessions/revoke-others", { method: "POST" }),
	logout: () => api<void>("/auth/logout", { method: "POST" }),
};

// P45 — 全站唯一年金额 presentation helper。只负责显示格式，
// 不做浮点运算、不改业务语义。默认账本币种 CNY；
// 目标 / 周期账单等模型自带 currency 时显式传入。
export const money = (value: string | number, currency = "CNY") =>
	new Intl.NumberFormat("zh-CN", { style: "currency", currency }).format(
		Number(value),
	);

export const localTime = (value: string) =>
	new Intl.DateTimeFormat("zh-CN", {
		dateStyle: "medium",
		timeStyle: "short",
	}).format(new Date(value));

export class ApiError extends Error {
	constructor(
		public readonly status: number,
		message: string,
		public readonly requestId: string | null = null,
	) {
		super(message);
	}
}

/**
 * P38 §13 — one explicit submit = one Idempotency-Key. The backend replays
 * the same key instead of creating a second ledger row, so browser retries,
 * double-clicks and React double-fires never double-book.
 */
export function newIdempotencyKey(): string {
	if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
		return crypto.randomUUID();
	}
	return `web-${Date.now()}-${Math.random().toString(36).slice(2, 12)}`;
}

function cookie(name: string): string {
	const value = document.cookie
		.split(";")
		.map((part) => part.trim())
		.find((part) => part.startsWith(`${name}=`));
	return value ? decodeURIComponent(value.slice(name.length + 1)) : "";
}

const statusMessages: Record<number, string> = {
	401: "登录已失效，请重新登录",
	403: "没有权限执行此操作",
	404: "请求的内容不存在",
	409: "状态已变化，请刷新后重试",
	422: "输入内容有误，请检查后重试",
	503: "系统暂不可用，请稍后重试",
};

async function responseError(response: Response): Promise<ApiError> {
	const payload = (await response.json().catch(() => null)) as {
		detail?: unknown;
	} | null;
	const detail =
		typeof payload?.detail === "string" &&
		/[\u3400-\u9fff]/u.test(payload.detail)
			? payload.detail
			: null;
	if (response.status === 401)
		window.dispatchEvent(new Event("larkledger:auth-expired"));
	return new ApiError(
		response.status,
		detail ?? statusMessages[response.status] ?? "请求失败，请稍后重试",
		response.headers.get("X-Request-ID"),
	);
}

export function errorText(error: unknown): string {
	// Safe user-facing error text: a Chinese backend message when present, the
	// generic fallback otherwise, plus the request id for support (P38 §23).
	if (error instanceof ApiError) {
		return error.requestId
			? `${error.message}（请求编号：${error.requestId}）`
			: error.message;
	}
	return error instanceof Error ? error.message : "操作失败，请重试";
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
	const method = (init.method ?? "GET").toUpperCase();
	const headers = new Headers(init.headers);
	if (init.body && !headers.has("Content-Type"))
		headers.set("Content-Type", "application/json");
	if (!["GET", "HEAD", "OPTIONS"].includes(method)) {
		headers.set("X-CSRF-Token", cookie("lark_ledger_csrf"));
	}
	const response = await fetch(`/api/web/v1${path}`, {
		...init,
		headers,
		credentials: "same-origin",
	});
	if (!response.ok) {
		throw await responseError(response);
	}
	if (response.status === 204) return undefined as T;
	return (await response.json()) as T;
}

export async function downloadExport(
	payload: unknown,
): Promise<{ blob: Blob; filename: string }> {
	const response = await fetch("/api/web/v1/exports", {
		method: "POST",
		headers: {
			"Content-Type": "application/json",
			"X-CSRF-Token": cookie("lark_ledger_csrf"),
		},
		credentials: "same-origin",
		body: JSON.stringify(payload),
	});
	if (!response.ok) {
		throw await responseError(response);
	}
	const disposition = response.headers.get("Content-Disposition") ?? "";
	const filename =
		disposition.match(/filename="([^"]+)"/)?.[1] ?? "larkledger-export.csv";
	return { blob: await response.blob(), filename };
}
