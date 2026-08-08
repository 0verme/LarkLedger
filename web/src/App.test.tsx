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
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(Response.json({ open_id: "ou_admin", name: "管理员", avatar_url: "", role: "ADMIN", expires_at: "2026-08-08T12:00:00+00:00" })));
    renderApp("/admin/dead");
    expect(await screen.findByRole("heading", { name: "Dead / Replay" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "事件" })).toBeInTheDocument();
  });
});
