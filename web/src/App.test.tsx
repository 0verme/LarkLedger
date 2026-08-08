import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { App } from "./App";

function renderApp(path = "/") {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[path]}><App /></MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(() => vi.unstubAllGlobals());

describe("dashboard routing and protection", () => {
  it("shows the Feishu login when the session is missing", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: "请先登录" }), { status: 401, headers: { "Content-Type": "application/json" } })));
    renderApp();
    expect(await screen.findByRole("link", { name: /使用飞书登录/ })).toHaveAttribute("href", "/api/web/v1/auth/login");
  });

  it("renders the protected route and hides admin navigation for users", async () => {
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/me")) return Promise.resolve(Response.json({ open_id: "ou_user", name: "小飞", avatar_url: "", role: "USER", expires_at: "2026-08-08T12:00:00+00:00" }));
      return Promise.resolve(Response.json({ items: [], page: 1, page_size: 25, total: 0, pages: 0 }));
    }));
    renderApp("/entries");
    expect(await screen.findByRole("heading", { name: "每一笔，都可追溯。" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "事件" })).not.toBeInTheDocument();
  });

  it("renders dashboard data and opens an entry drawer", async () => {
    const entry = { id: "1", short_id: "A83F2", amount: "32.00", currency: "CNY", direction: "EXPENSE", category: "餐饮", note: "午饭", occurred_at: "2026-08-08T04:00:00Z", source_type: "text", created_at: "2026-08-08T04:00:00Z", updated_at: "2026-08-08T04:00:00Z", deleted_at: null };
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/me")) return Promise.resolve(Response.json({ open_id: "ou_user", name: "小飞", avatar_url: "", role: "USER", expires_at: "2026-08-08T12:00:00+00:00" }));
      if (url.includes("/dashboard")) return Promise.resolve(Response.json({ month_income: "100", month_expense: "32", month_balance: "68", budget_usage_rate: "64", pending_count: 1, recent_entries: [entry], trend: [], categories: [{ category: "餐饮", amount: "32", ratio: "100" }] }));
      if (url.includes("/entries/A83F2")) return Promise.resolve(Response.json({ entry, revisions: [] }));
      return Promise.resolve(Response.json({ items: [entry], page: 1, page_size: 25, total: 1, pages: 1 }));
    }));
    const view = renderApp();
    expect(await screen.findByText("本月，保持清晰。")).toBeInTheDocument();
    view.unmount();
    renderApp("/entries");
    fireEvent.click(await screen.findByRole("button", { name: "#A83F2" }));
    expect(await screen.findByRole("heading", { name: /32\.00/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /修改/ })).toBeInTheDocument();
  });

  it("shows operations navigation for administrators", async () => {
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/me")) return Promise.resolve(Response.json({ open_id: "ou_admin", name: "管理员", avatar_url: "", role: "ADMIN", expires_at: "2026-08-08T12:00:00+00:00" }));
      return Promise.resolve(Response.json({ event_count: 0, reply_count: 0, latest_events: [], latest_replies: [] }));
    }));
    renderApp("/admin/dead");
    expect(await screen.findByRole("heading", { name: "Dead / Replay" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "事件" })).toBeInTheDocument();
  });

  it("shows only redacted configuration and supports mobile navigation", async () => {
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/me")) return Promise.resolve(Response.json({ open_id: "ou_admin", name: "管理员", avatar_url: "", role: "ADMIN", expires_at: "2026-08-08T12:00:00+00:00" }));
      return Promise.resolve(Response.json({ version: "0.3.0", event_mode: "websocket", timezone: "Asia/Shanghai", currency: "CNY", worker_enabled: true, reply_worker_enabled: true, cleanup_worker_enabled: true, pending_enabled: true, ai_provider: "DeepSeek-compatible", ai_model: "deepseek-chat", ai_api_key_configured: true, lark_app_secret_configured: true, dashboard_base_url: "https://ledger.example.com", session_ttl_seconds: 28800, secure_cookie: true }));
    }));
    renderApp("/admin/config");
    expect(await screen.findByRole("heading", { name: "系统配置" })).toBeInTheDocument();
    expect(screen.getByText("DeepSeek-compatible")).toBeInTheDocument();
    expect(screen.getAllByText("已配置")).toHaveLength(2);
    fireEvent.click(screen.getByRole("button", { name: "打开导航" }));
    expect(document.querySelector(".sidebar")).toHaveClass("open");
    fireEvent.click(screen.getAllByRole("button", { name: "关闭导航" }).at(-1)!);
    expect(document.querySelector(".sidebar")).not.toHaveClass("open");
  });

  it("returns to login when an authenticated request expires", async () => {
    let expired = false;
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/me")) return Promise.resolve(Response.json({ open_id: "ou_user", name: "小飞", avatar_url: "", role: "USER", expires_at: "2026-08-08T12:00:00+00:00" }));
      if (!expired) {
        expired = true;
        return Promise.resolve(new Response(JSON.stringify({ detail: "session expired" }), { status: 401, headers: { "Content-Type": "application/json" } }));
      }
      return Promise.resolve(Response.json({}));
    }));
    renderApp("/entries");
    expect(await screen.findByRole("link", { name: /使用飞书登录/ })).toBeInTheDocument();
  });

  it("dry-runs event replay before explicit execution", async () => {
    const event = { event_id: "evt-dead", source_message_id: "om_se…dead", status: "dead", attempt_count: 3, transport: "webhook", received_at: "2026-08-08T04:00:00Z", processed_at: "2026-08-08T04:01:00Z", last_error_code: "TemporaryFailure", updated_at: "2026-08-08T04:01:00Z" };
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/me")) return Promise.resolve(Response.json({ open_id: "ou_admin", name: "管理员", avatar_url: "", role: "ADMIN", expires_at: "2026-08-08T12:00:00+00:00" }));
      if (init?.method === "POST") {
        const body = JSON.parse(String(init.body));
        return Promise.resolve(Response.json({ mode: body.execute ? "execute" : "dry-run", outcome: body.execute ? "requeued" : "eligible", audit_id: null, resulting_status: body.execute ? "received" : null, preflight: { event_found: true, eligible: true, status: "dead", business_result_committed: false, outbox_count: 0, ledger_entry_count: 0, batch_risk: "single_or_unknown", lease_state: "none", reason_codes: [], recommended_action: "execute" } }));
      }
      return Promise.resolve(Response.json({ event_count: 1, reply_count: 0, latest_events: [event], latest_replies: [] }));
    });
    vi.stubGlobal("fetch", fetchMock);
    renderApp("/admin/dead");
    fireEvent.click(await screen.findByText("evt-dead"));
    fireEvent.change(screen.getByPlaceholderText(/记录本次重放/), { target: { value: "temporary failure" } });
    fireEvent.click(screen.getByRole("button", { name: "先执行 Dry Run" }));
    expect(await screen.findByRole("heading", { name: "安全预检通过" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "继续事件重放" }));
    fireEvent.click(screen.getByRole("button", { name: "明确执行事件重放" }));
    expect(await screen.findByText("事件已安全重新入队")).toBeInTheDocument();
    const requests = fetchMock.mock.calls.filter((call) => call[1]?.method === "POST");
    expect(JSON.parse(String(requests[0][1]?.body)).execute).toBe(false);
    expect(JSON.parse(String(requests[1][1]?.body)).confirmation_event_id).toBe("evt-dead");
  });

  it("renders analytics and updates a category budget", async () => {
    const me = { open_id: "ou_user", name: "小飞", avatar_url: "", role: "USER", expires_at: "2026-08-08T12:00:00+00:00" };
    const budget = { currency: "CNY", total_budget: "200", total_spent: "40", total_remaining: "160", usage_rate: "20", items: [{ category: "餐饮", amount: "200", spent: "40", remaining: "160", usage_rate: "20" }] };
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/me")) return Promise.resolve(Response.json(me));
      if (url.includes("analytics/monthly")) return Promise.resolve(Response.json([{ period: "2026-08", income: "100", expense: "40", balance: "60" }]));
      if (url.includes("/analytics?")) return Promise.resolve(Response.json({ summary: { range_start: "2026-08-01T00:00:00Z", range_end: "2026-08-09T00:00:00Z", income: "100", expense: "40", balance: "60", entry_count: 2 }, trend: [{ period: "2026-08-08", income: "100", expense: "40", balance: "60" }], categories: [{ category: "餐饮", amount: "40", ratio: "100" }] }));
      if (url.includes("/budgets")) return Promise.resolve(Response.json(init?.method === "PUT" ? { ...budget, total_budget: "300", total_remaining: "260", usage_rate: "13.333", items: [{ ...budget.items[0], amount: "300", remaining: "260", usage_rate: "13.333" }] } : budget));
      return Promise.resolve(Response.json({}));
    }));
    const view = renderApp("/analytics");
    expect(await screen.findByRole("heading", { name: "看懂钱花去了哪里。" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /餐饮/ })).toHaveAttribute("href", "/entries?category=%E9%A4%90%E9%A5%AE");
    view.unmount();
    renderApp("/budgets");
    fireEvent.click(await screen.findByRole("button", { name: "修改 餐饮" }));
    fireEvent.change(screen.getByLabelText("每月预算"), { target: { value: "300" } });
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    expect(await screen.findByText("预算已更新")).toBeInTheDocument();
    expect(screen.getAllByText(/¥300\.00/).length).toBeGreaterThan(0);
  });

  it("confirms and cancels frozen pending previews", async () => {
    const base = { status: "pending", source_type: "image", transport: "feishu", risk_reason: "图片识别", entries_total: 1, income_total: "0", expense_total: "32", currency: "CNY", created_at: "2026-08-08T04:00:00Z", expires_at: "2026-08-08T05:00:00Z", completed_at: null };
    const rows = [{ ...base, confirmation_id: "#C-A83F2" }, { ...base, confirmation_id: "#C-B83F2" }];
    const preview = { items: [{ index: null, direction: "expense", amount: "32", currency: "CNY", category: "餐饮", occurred_at: "2026-08-08 12:00", note: "午饭", duplicate_of: null }], budgets: [], anomalies: [] };
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/me")) return Promise.resolve(Response.json({ open_id: "ou_user", name: "小飞", avatar_url: "", role: "USER", expires_at: "2026-08-08T12:00:00+00:00" }));
      const row = rows.find((item) => url.includes(item.confirmation_id.slice(3))) ?? rows[0];
      if (init?.method === "POST") {
        const status = url.endsWith("/confirm") ? "executed" : "cancelled";
        return Promise.resolve(Response.json({ message: status === "executed" ? "已确认入账" : "已取消", pending: { pending: { ...row, status }, preview } }));
      }
      if (url.includes("/pending/")) return Promise.resolve(Response.json({ pending: row, preview }));
      return Promise.resolve(Response.json({ items: rows, page: 1, page_size: 20, total: 2, pages: 1 }));
    }));
    renderApp("/pending");
    fireEvent.click(await screen.findByText("#C-A83F2"));
    fireEvent.click(await screen.findByRole("button", { name: /确认执行/ }));
    const confirmButtons = await screen.findAllByRole("button", { name: "确认执行" });
    fireEvent.click(confirmButtons.at(-1)!);
    expect(await screen.findByText("已确认入账")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /^关闭$/ }));
    fireEvent.click(await screen.findByText("#C-B83F2"));
    fireEvent.click(await screen.findByRole("button", { name: /^取消$/ }));
    fireEvent.click(await screen.findByRole("button", { name: "确认取消" }));
    expect((await screen.findAllByText("已取消")).length).toBeGreaterThan(0);
  });
});
