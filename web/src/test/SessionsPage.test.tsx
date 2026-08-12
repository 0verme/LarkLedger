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

function meResponse() {
	return Promise.resolve(
		Response.json({
			open_id: "ou_user",
			name: "小飞",
			avatar_url: "",
			role: "USER",
			expires_at: "2026-09-08T12:00:00+00:00",
		}),
	);
}

const phoneSession = {
	id: "sess-phone",
	created_at: "2026-08-13T01:00:00Z",
	last_seen_at: "2026-08-14T02:00:00Z",
	expires_at: "2026-09-08T12:00:00Z",
	revoked_at: null,
	current: false,
	device: "iOS · Safari（移动端）",
	user_agent: "Mozilla/5.0 (iPhone)",
};
const laptopSession = {
	id: "sess-laptop",
	created_at: "2026-08-14T01:00:00Z",
	last_seen_at: "2026-08-14T03:00:00Z",
	expires_at: "2026-09-08T12:00:00Z",
	revoked_at: null,
	current: true,
	device: "Windows · Chrome",
	user_agent: "Mozilla/5.0 (Windows NT 10.0)",
};

function defaultFetch(
	input: RequestInfo | URL,
	init?: RequestInit,
): Promise<Response> {
	const url = String(input);
	if (url.endsWith("/me")) return meResponse();
	if (url.endsWith("/auth/session")) {
		return Promise.resolve(
			Response.json({
				session_id: laptopSession.id,
				open_id: "ou_user",
				name: "小飞",
				avatar_url: "",
				role: "USER",
				expires_at: "2026-09-08T12:00:00+00:00",
			}),
		);
	}
	if (url.endsWith("/auth/sessions") && init?.method === "DELETE") {
		return Promise.resolve(new Response(null, { status: 204 }));
	}
	if (url.endsWith("/auth/sessions/revoke-others")) {
		return Promise.resolve(new Response(null, { status: 204 }));
	}
	if (url.endsWith("/auth/sessions")) {
		return Promise.resolve(
			Response.json({
				items: [laptopSession, phoneSession],
				current_session_id: laptopSession.id,
			}),
		);
	}
	if (url.endsWith("/auth/logout")) {
		return Promise.resolve(new Response(null, { status: 204 }));
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
	if (url.endsWith("/entries")) {
		return Promise.resolve(
			Response.json({ items: [], page: 1, page_size: 25, total: 0, pages: 0 }),
		);
	}
	return Promise.resolve(
		Response.json({ items: [], page: 1, page_size: 25, total: 0, pages: 0 }),
	);
}

afterEach(() => {
	vi.unstubAllGlobals();
});

describe("SessionsPage", () => {
	it("renders the session list with current marking and device labels", async () => {
		renderApp("/sessions", defaultFetch);
		expect(
			await screen.findByRole("heading", { name: "登录会话" }),
		).toBeInTheDocument();
		expect(await screen.findByText("Windows · Chrome")).toBeInTheDocument();
		expect(screen.getByText("iOS · Safari（移动端）")).toBeInTheDocument();
		expect(screen.getByText("当前会话")).toBeInTheDocument();
		expect(screen.getByText("已登录设备")).toBeInTheDocument();
	});

	it("revokes another device session via DELETE", async () => {
		const fetchMock = vi.fn(defaultFetch);
		vi.stubGlobal("fetch", fetchMock);
		const client = new QueryClient({
			defaultOptions: { queries: { retry: false } },
		});
		render(
			<QueryClientProvider client={client}>
				<MemoryRouter initialEntries={["/sessions"]}>
					<App />
				</MemoryRouter>
			</QueryClientProvider>,
		);
		const revokeButtons = await screen.findAllByRole("button", { name: "注销" });
		expect(revokeButtons).toHaveLength(1); // current session is not revocable
		fireEvent.click(revokeButtons[0]);
		await waitFor(() => {
			expect(fetchMock).toHaveBeenCalledWith(
				expect.stringContaining("/auth/sessions/sess-phone"),
				expect.objectContaining({ method: "DELETE" }),
			);
		});
	});

	it("revoke-all-others shows confirmation and calls the endpoint", async () => {
		const fetchMock = vi.fn(defaultFetch);
		vi.stubGlobal("fetch", fetchMock);
		const client = new QueryClient({
			defaultOptions: { queries: { retry: false } },
		});
		render(
			<QueryClientProvider client={client}>
				<MemoryRouter initialEntries={["/sessions"]}>
					<App />
				</MemoryRouter>
			</QueryClientProvider>,
		);
		const revokeOthers = await screen.findByRole("button", {
			name: "注销其他设备",
		});
		// Wait for the session list to load so the button is enabled.
		await waitFor(() => expect(revokeOthers).toBeEnabled());
		fireEvent.click(revokeOthers);
		expect(
			await screen.findByText("注销其他所有设备？"),
		).toBeInTheDocument();
		fireEvent.click(screen.getByRole("button", { name: "确认注销" }));
		await waitFor(() => {
			expect(fetchMock).toHaveBeenCalledWith(
				expect.stringContaining("/auth/sessions/revoke-others"),
				expect.objectContaining({ method: "POST" }),
			);
		});
	});

	it("dispatches auth-expired after logout", async () => {
		const dispatched: string[] = [];
		const listener = (event: Event) => dispatched.push(event.type);
		window.addEventListener("larkledger:auth-expired", listener);
		renderApp("/sessions", defaultFetch);
		const logout = await screen.findByRole("button", {
			name: "退出当前会话",
		});
		fireEvent.click(logout);
		await waitFor(() => {
			expect(dispatched).toContain("larkledger:auth-expired");
		});
		window.removeEventListener("larkledger:auth-expired", listener);
	});
});
