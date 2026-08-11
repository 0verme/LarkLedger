import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";

function renderApp(path: string, fetchMock: (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>) {
  vi.stubGlobal("fetch", vi.fn(fetchMock));
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[path]}><App /></MemoryRouter>
    </QueryClientProvider>,
  );
}

const account = { id: "acc-1", ledger_id: "l-1", name: "招行储蓄", type: "asset", subtype: null, provider: null, currency: "CNY", opening_balance: "30000.00", status: "active", is_default: true, visibility: "shared", owner_user_id: null, created_at: "2026-08-01T00:00:00Z", updated_at: "2026-08-01T00:00:00Z" };

const goal = {
  id: "goal-1",
  ledger_id: "l-1",
  name: "应急储备",
  description: "",
  goal_type: "savings",
  target_amount: "60000.00",
  currency: "CNY",
  target_date: "2027-03-31",
  status: "active",
  created_by_user_id: "u-1",
  created_at: "2026-08-01T00:00:00Z",
  updated_at: "2026-08-01T00:00:00Z",
  account_bindings: [{ account_id: "acc-1", account_name: "招行储蓄", currency: "CNY" }],
  current_amount: "32400.00",
  remaining_amount: "27600.00",
  progress_percent: "54.00",
  is_target_reached: false,
};

const insight = {
  key: "budget_risk:餐饮:2026-08",
  type: "budget_risk",
  severity: "warning",
  title: "预算使用速度偏快",
  summary: "本月餐饮预算已使用 71.00%，而时间才过去 32.26%。按当前使用速度，预算存在超支风险。",
  metric: { category: "餐饮", usage_rate: "71.00", elapsed_ratio: "32.26" },
  period: "2026-08",
  related_category: "餐饮",
  related_goal: null,
  related_goal_name: null,
  related_account: null,
  generated_at: "2026-08-08T04:00:00Z",
  explanation: null,
};

function defaultFetch(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  const url = String(input);
  if (url.endsWith("/me")) {
    return Promise.resolve(Response.json({ open_id: "ou_user", name: "小飞", avatar_url: "", role: "USER", expires_at: "2026-08-08T12:00:00+00:00" }));
  }
  if (url.includes("/goals") && init?.method === "POST") {
    return Promise.resolve(Response.json(goal, { status: 201 }));
  }
  if (url.includes("/goals")) {
    return Promise.resolve(Response.json({ items: [goal] }));
  }
  if (url.includes("/accounts")) {
    return Promise.resolve(Response.json({ items: [account] }));
  }
  if (url.includes("/dashboard")) {
    return Promise.resolve(Response.json({
      month_income: "1000.00", month_expense: "300.00", month_balance: "700.00",
      budget_usage_rate: "6.00", pending_count: 0, recent_entries: [],
      trend: [], categories: [],
    }));
  }
  if (url.includes("/insights")) {
    return Promise.resolve(Response.json({ insights: [insight] }));
  }
  if (url.includes("/overview")) {
    return Promise.resolve(Response.json({
      ledger_id: "l-1", ledger_name: "我的账本", ledger_kind: "personal", period: "2026-08",
      income_total: "1000.00", expense_total: "300.00", net_total: "700.00",
      budget: { total_budget: "5000.00", total_spent: "300.00", total_remaining: "4700.00", usage_rate: "6.00", status: "normal" },
      account_balance_summary: { currency: "CNY", total_assets: "32400.00", total_liabilities: "0.00", net_assets: "32400.00", account_count: 1 },
      member_contributions: [], top_categories: [], upcoming_recurring: [], recent_transactions: [],
    }));
  }
  if (url.includes("/ledgers")) {
    return Promise.resolve(Response.json({ items: [{ id: "l-1", name: "我的账本", is_default: true, is_current: true, currency: "CNY", timezone: "Asia/Shanghai", kind: "personal", household_id: null }] }));
  }
  return Promise.resolve(Response.json({ items: [], page: 1, page_size: 25, total: 0, pages: 0 }));
}

afterEach(() => vi.unstubAllGlobals());

describe("goals page", () => {
  it("renders the goal list with deterministic progress", async () => {
    renderApp("/goals", defaultFetch);
    expect(await screen.findByText("把想存到的钱，变成看得见的进度。")).toBeInTheDocument();
    expect(await screen.findByText("应急储备")).toBeInTheDocument();
    expect(screen.getByText(/32,400\.00/)).toBeInTheDocument();
    expect(screen.getByText(/目标日期 2027-03-31/)).toBeInTheDocument();
    expect(screen.getByText(/54\.0%/)).toBeInTheDocument();
  });

  it("shows a friendly empty state when there are no goals", async () => {
    renderApp("/goals", (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/me")) {
        return Promise.resolve(Response.json({ open_id: "ou_user", name: "小飞", avatar_url: "", role: "USER", expires_at: "2026-08-08T12:00:00+00:00" }));
      }
      if (url.includes("/goals")) return Promise.resolve(Response.json({ items: [] }));
      if (url.includes("/accounts")) return Promise.resolve(Response.json({ items: [account] }));
      return Promise.resolve(Response.json({ items: [], page: 1, page_size: 25, total: 0, pages: 0 }));
    });
    expect(await screen.findByText("还没有财务目标")).toBeInTheDocument();
  });

  it("creates a goal from the dialog", async () => {
    renderApp("/goals", defaultFetch);
    const createButton = await screen.findByRole("button", { name: /创建目标/ });
    fireEvent.click(createButton);
    const nameInput = await screen.findByPlaceholderText("例如：应急储备");
    fireEvent.change(nameInput, { target: { value: "旅行基金" } });
    const amountInput = screen.getByPlaceholderText("60000");
    fireEvent.change(amountInput, { target: { value: "20000" } });
    await waitFor(() => expect(screen.getByRole("button", { name: "保存" })).not.toBeDisabled());
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    expect(await screen.findByText("目标已创建")).toBeInTheDocument();
  });
});

describe("overview insights", () => {
  it("renders deterministic insight cards on the overview", async () => {
    renderApp("/overview", defaultFetch);
    expect(await screen.findByText("值得关注")).toBeInTheDocument();
    expect(await screen.findByText(/预算存在超支风险/)).toBeInTheDocument();
  });

  it("shows a calm empty state when there are no insights", async () => {
    renderApp("/overview", (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/me")) {
        return Promise.resolve(Response.json({ open_id: "ou_user", name: "小飞", avatar_url: "", role: "USER", expires_at: "2026-08-08T12:00:00+00:00" }));
      }
      if (url.includes("/insights")) return Promise.resolve(Response.json({ insights: [] }));
      if (url.includes("/overview")) {
        return Promise.resolve(Response.json({
          ledger_id: "l-1", ledger_name: "我的账本", ledger_kind: "personal", period: "2026-08",
          income_total: "0.00", expense_total: "0.00", net_total: "0.00",
          budget: { total_budget: null, total_spent: "0.00", total_remaining: null, usage_rate: null, status: "none" },
          account_balance_summary: { currency: "CNY", total_assets: "0.00", total_liabilities: "0.00", net_assets: "0.00", account_count: 0 },
          member_contributions: [], top_categories: [], upcoming_recurring: [], recent_transactions: [],
        }));
      }
      if (url.includes("/ledgers")) {
        return Promise.resolve(Response.json({ items: [{ id: "l-1", name: "我的账本", is_default: true, is_current: true, currency: "CNY", timezone: "Asia/Shanghai", kind: "personal", household_id: null }] }));
      }
      return Promise.resolve(Response.json({ items: [], page: 1, page_size: 25, total: 0, pages: 0 }));
    });
    expect(await screen.findByText("目前没有需要特别关注的变化")).toBeInTheDocument();
  });
});
