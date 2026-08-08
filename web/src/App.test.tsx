import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
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
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(Response.json({ open_id: "ou_user", name: "小飞", avatar_url: "", role: "USER", expires_at: "2026-08-08T12:00:00+00:00" })));
    renderApp("/entries");
    expect(await screen.findByRole("heading", { name: "账目" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "事件" })).not.toBeInTheDocument();
  });

  it("shows operations navigation for administrators", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(Response.json({ open_id: "ou_admin", name: "管理员", avatar_url: "", role: "ADMIN", expires_at: "2026-08-08T12:00:00+00:00" })));
    renderApp("/admin/dead");
    expect(await screen.findByRole("heading", { name: "Dead / Replay" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "事件" })).toBeInTheDocument();
  });
});
