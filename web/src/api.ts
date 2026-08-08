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
  direction: "EXPENSE" | "INCOME";
  category: string;
  note: string;
  occurred_at: string;
  source_type: string;
  created_at: string;
  updated_at: string;
  deleted_at: string | null;
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
  trend: Array<{ period: string; income: string; expense: string; balance: string }>;
  categories: Array<{ category: string; amount: string; ratio: string }>;
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
    items: Array<{ index: number | null; direction: string; amount: string; currency: string; category: string; occurred_at: string; note: string; duplicate_of: string | null }>;
    budgets: Array<{ category: string; amount: string; currency: string }>;
    anomalies: string[];
  };
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

export type AdminEventPage = { items: AdminEvent[]; page: number; page_size: number; total: number; pages: number };
export type AdminOutboxPage = { items: AdminOutbox[]; page: number; page_size: number; total: number; pages: number };
export type AdminDeadSummary = { event_count: number; reply_count: number; latest_events: AdminEvent[]; latest_replies: AdminOutbox[] };
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
export type EventReplayResult = { mode: string; outcome: string; audit_id: string | null; resulting_status: string | null; preflight: ReplayPreflight };
export type HealthSnapshot = { status: string; checks: Record<string, { status: string; reason?: string; current?: string; enabled?: boolean; running?: boolean; last_error_code?: string | null }> };

export const money = (value: string | number) =>
  new Intl.NumberFormat("zh-CN", { style: "currency", currency: "CNY" }).format(Number(value));

export const localTime = (value: string) =>
  new Intl.DateTimeFormat("zh-CN", { dateStyle: "medium", timeStyle: "short" }).format(new Date(value));

export class ApiError extends Error {
  constructor(
    public readonly status: number,
    message: string,
  ) {
    super(message);
  }
}

function cookie(name: string): string {
  const value = document.cookie
    .split(";")
    .map((part) => part.trim())
    .find((part) => part.startsWith(`${name}=`));
  return value ? decodeURIComponent(value.slice(name.length + 1)) : "";
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const method = (init.method ?? "GET").toUpperCase();
  const headers = new Headers(init.headers);
  if (init.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  if (!["GET", "HEAD", "OPTIONS"].includes(method)) {
    headers.set("X-CSRF-Token", cookie("lark_ledger_csrf"));
  }
  const response = await fetch(`/api/web/v1${path}`, {
    ...init,
    headers,
    credentials: "same-origin",
  });
  if (!response.ok) {
    const payload = (await response.json().catch(() => null)) as {
      detail?: string;
    } | null;
    throw new ApiError(response.status, payload?.detail ?? "请求失败，请稍后重试");
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}
