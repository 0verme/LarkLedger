import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";

function renderApp(
	path: string,
	fetchMock: (
		input: RequestInfo | URL,
		init?: RequestInit,
	) => Promise<Response>,
) {
	vi.stubGlobal("fetch", vi.fn(fetchMock));
	const client = new QueryClient({
		defaultOptions: { queries: { retry: false } },
	});
	return render(
		<QueryClientProvider client={client}>
			<MemoryRouter initialEntries={[path]}>
				<App />
			</MemoryRouter>
		</QueryClientProvider>,
	);
}

const credential = {
	id: "cred-1",
	name: "ESP32 记账按钮",
	token_prefix: "llv1_AbCdEf",
	scopes: ["ledger:read", "ledger:write"],
	created_at: "2026-08-13T00:00:00Z",
	last_used_at: "2026-08-14T08:00:00Z",
	expires_at: null,
	revoked_at: null,
};

function defaultFetch(
	input: RequestInfo | URL,
	init?: RequestInit,
): Promise<Response> {
	const url = String(input);
	if (url.endsWith("/me")) {
		return Promise.resolve(
			Response.json({
				open_id: "ou_user",
				name: "小飞",
				avatar_url: "",
				role: "USER",
				expires_at: "2026-08-08T12:00:00+00:00",
			}),
		);
	}
	if (url.includes("/client-credentials") && init?.method === "POST") {
		return Promise.resolve(
			Response.json(
				{ ...credential, id: "cred-2", token: "llv1_OneTimeSecretValue123" },
				{ status: 201 },
			),
		);
	}
	if (url.includes("/client-credentials") && init?.method === "DELETE") {
		return Promise.resolve(new Response(null, { status: 204 }));
	}
	if (url.includes("/client-credentials")) {
		return Promise.resolve(Response.json({ items: [credential] }));
	}
	if (url.includes("/ledgers")) {
		return Promise.resolve(
			Response.json({
				items: [
					{
						id: "l-1",
						name: "我的账本",
						is_default: true,
						is_current: true,
						currency: "CNY",
						timezone: "Asia/Shanghai",
						kind: "personal",
						household_id: null,
					},
				],
			}),
		);
	}
	return Promise.resolve(
		Response.json({ items: [], page: 1, page_size: 25, total: 0, pages: 0 }),
	);
}

afterEach(() => vi.unstubAllGlobals());

describe("api tokens page", () => {
	it("lists tokens with prefix, scopes and last-used time", async () => {
		renderApp("/api-tokens", defaultFetch);
		expect(
			await screen.findByRole("heading", { name: "API 令牌" }),
		).toBeInTheDocument();
		expect(await screen.findByText("ESP32 记账按钮")).toBeInTheDocument();
		expect(screen.getByText("llv1_AbCdEf…")).toBeInTheDocument();
		expect(
			screen.getByText(/读写（记账 \/ 修改 \/ 删除）/),
		).toBeInTheDocument();
		expect(screen.getByText("有效")).toBeInTheDocument();
	});

	it("creates a token and shows the secret exactly once", async () => {
		renderApp("/api-tokens", defaultFetch);
		const createButtons = await screen.findAllByRole("button", {
			name: /创建令牌/,
		});
		fireEvent.click(createButtons[0]);
		const nameInput = await screen.findByPlaceholderText(/ESP32/);
		fireEvent.change(nameInput, { target: { value: "CLI 记账" } });
		const submitButtons = screen.getAllByRole("button", { name: /创建令牌/ });
		await waitFor(() =>
			expect(submitButtons[submitButtons.length - 1]).not.toBeDisabled(),
		);
		fireEvent.click(submitButtons[submitButtons.length - 1]);
		expect(await screen.findByText("请立即保存")).toBeInTheDocument();
		expect(screen.getByText("llv1_OneTimeSecretValue123")).toBeInTheDocument();
		expect(screen.getByText(/明文只会显示这一次/)).toBeInTheDocument();
	});

	it("revokes a token", async () => {
		vi.spyOn(window, "confirm").mockReturnValue(true);
		renderApp("/api-tokens", defaultFetch);
		const revoke = await screen.findByRole("button", { name: /撤销/ });
		fireEvent.click(revoke);
		expect(
			await screen.findByText("令牌已撤销，立即失效。"),
		).toBeInTheDocument();
	});
});
