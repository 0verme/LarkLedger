import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";
import type { AccountList, Entry, EntryPage, LedgerList } from "../api";

const ME = {
	open_id: "ou_user",
	name: "小飞",
	avatar_url: "",
	role: "USER",
	expires_at: "2026-09-08T12:00:00+00:00",
};

const LEDGERS: LedgerList = {
	items: [
		{
			id: "ledger-personal",
			name: "我的账本",
			is_default: true,
			is_current: true,
			currency: "CNY",
			timezone: "Asia/Shanghai",
			kind: "personal",
			household_id: null,
		},
		{
			id: "ledger-home",
			name: "家庭账本",
			is_default: false,
			is_current: false,
			currency: "CNY",
			timezone: "Asia/Shanghai",
			kind: "household_shared",
			household_id: "household-1",
		},
	],
};

function entry(overrides: Partial<Entry> = {}): Entry {
	return {
		id: "entry-1",
		short_id: "A83F2",
		amount: "28.00",
		currency: "CNY",
		direction: "expense",
		category: "餐饮",
		note: "午餐",
		occurred_at: "2026-08-10T04:30:00+00:00",
		source_type: "web",
		created_at: "2026-08-10T04:30:00+00:00",
		updated_at: "2026-08-10T04:30:00+00:00",
		deleted_at: null,
		account_id: "account-1",
		account_name: "支付宝",
		payer_user_id: "",
		payer_name: null,
		...overrides,
	};
}

const ACCOUNTS: AccountList = {
	items: [
		{
			id: "account-1",
			ledger_id: "ledger-personal",
			name: "支付宝",
			type: "asset",
			subtype: null,
			provider: null,
			currency: "CNY",
			opening_balance: "0",
			status: "active",
			is_default: true,
			visibility: "shared",
			owner_user_id: null,
			created_at: "2026-08-01T00:00:00+00:00",
			updated_at: "2026-08-01T00:00:00+00:00",
		},
		{
			id: "account-2",
			ledger_id: "ledger-personal",
			name: "私密钱包",
			type: "cash",
			subtype: null,
			provider: null,
			currency: "CNY",
			opening_balance: "0",
			status: "active",
			is_default: false,
			visibility: "private",
			owner_user_id: "user-1",
			created_at: "2026-08-01T00:00:00+00:00",
			updated_at: "2026-08-01T00:00:00+00:00",
		},
	],
};

const DASHBOARD = {
	month_income: "18000",
	month_expense: "28",
	month_balance: "17972",
	budget_usage_rate: null,
	pending_count: 0,
	recent_entries: [entry()],
	trend: [],
	categories: [{ category: "餐饮", amount: "28", ratio: "100" }],
};

const ASSETS = {
	ledger_id: "ledger-personal",
	currency: "CNY",
	total_assets: "1000",
	total_liabilities: "200",
	net_assets: "800",
	accounts: [
		{
			account_id: "account-1",
			ledger_id: "ledger-personal",
			account_name: "支付宝",
			account_type: "asset",
			currency: "CNY",
			opening_balance: "0",
			current_balance: "1000",
			archived: false,
		},
	],
};

// In-memory backend used by the fetch mock so journeys mutate real state.
type FakeBackend = {
	entries: ReturnType<typeof entry>[];
	posts: Array<{ key: string | null; body: unknown }>;
	headers: Record<string, string | null>;
};

function makeBackend(): FakeBackend {
	return { entries: [entry()], posts: [], headers: {} };
}

function ok(payload: unknown, status = 200, headers: Record<string, string> = {}) {
	return Promise.resolve(
		new Response(JSON.stringify(payload), {
			status,
			headers: { "Content-Type": "application/json", ...headers },
		}),
	);
}

function jsonOk(payload: unknown) {
	return Promise.resolve(
		new Response(JSON.stringify(payload), {
			status: 200,
			headers: { "Content-Type": "application/json" },
		}),
	);
}

function renderApp(path = "/") {
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

function setCsrfCookie() {
	document.cookie = "lark_ledger_csrf=test-csrf-token";
}

beforeEach(() => {
	setCsrfCookie();
	window.localStorage.clear();
});

afterEach(() => {
	vi.unstubAllGlobals();
	document.cookie = "lark_ledger_csrf=; Max-Age=0";
});

describe("W01–W06 first-party dashboard, ledger, entries and quick bookkeeping", () => {
	it("W01 logs in and lands on the home dashboard", async () => {
		vi.stubGlobal(
			"fetch",
			vi.fn((input: RequestInfo | URL) => {
				const url = String(input);
				if (url.endsWith("/me")) return jsonOk(ME);
				if (url.endsWith("/ledgers")) return jsonOk(LEDGERS);
				if (url.includes("/dashboard")) return jsonOk(DASHBOARD);
				if (url.includes("/assets")) return jsonOk(ASSETS);
				return jsonOk({ items: [], page: 1, page_size: 25, total: 0, pages: 0 });
			}),
		);
		renderApp("/");
		expect(await screen.findByText("本月，保持清晰。")).toBeInTheDocument();
		// The current ledger name is shown on the home page.
		expect(await screen.findByText(/个人账本 · 我的账本/)).toBeInTheDocument();
		expect(
			screen.getAllByRole("button", { name: /记一笔/ })[0],
		).toBeInTheDocument();
	});

	it("W02 ledger selector switches the active ledger and pages follow", async () => {
		vi.stubGlobal(
			"fetch",
			vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
				const url = String(input);
				if (url.endsWith("/me")) return jsonOk(ME);
				if (url.includes("/ledgers/ledger-home/select") && init?.method === "POST") {
					// select ledger → the target becomes current
					LEDGERS.items = LEDGERS.items.map((item) => ({
						...item,
						is_current: item.id === "ledger-home",
					}));
					return jsonOk(LEDGERS.items.find((item) => item.id === "ledger-home"));
				}
				if (url.endsWith("/ledgers")) return jsonOk(LEDGERS);
				if (url.includes("/dashboard")) return jsonOk(DASHBOARD);
				if (url.includes("/assets")) return jsonOk(ASSETS);
				return jsonOk({ items: [], page: 1, page_size: 25, total: 0, pages: 0 });
			}),
		);
		renderApp("/");
		await screen.findByText("本月，保持清晰。");
		const selector = await screen.findByLabelText("当前账本");
		fireEvent.change(selector, { target: { value: "ledger-home" } });
		await waitFor(() => {
			const fetchMock = vi.mocked(fetch);
			expect(
				fetchMock.mock.calls.some(
					([url, init]) =>
						String(url).endsWith("/ledgers/ledger-home/select") &&
						init?.method === "POST",
				),
			).toBe(true);
		});
		// The home page reflects the switched ledger.
		expect(await screen.findByText(/家庭账本 · 家庭账本/)).toBeInTheDocument();
	});

	it("W03 shows the transaction list with amount, category and note", async () => {
		vi.stubGlobal(
			"fetch",
			vi.fn((input: RequestInfo | URL) => {
				const url = String(input);
				if (url.endsWith("/me")) return jsonOk(ME);
				if (url.includes("/accounts")) return jsonOk(ACCOUNTS);
				const page: EntryPage = {
					items: [entry()],
					page: 1,
					page_size: 25,
					total: 1,
					pages: 1,
				};
				return jsonOk(page);
			}),
		);
		renderApp("/entries");
		expect(await screen.findByText("每一笔，都可追溯。")).toBeInTheDocument();
		expect(await screen.findByText("午餐")).toBeInTheDocument();
		expect(screen.getByText("餐饮")).toBeInTheDocument();
	});

	it("W04 creates an expense via quick bookkeeping", async () => {
		const backend = makeBackend();
		vi.stubGlobal(
			"fetch",
			vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
				const url = String(input);
				if (url.endsWith("/me")) return jsonOk(ME);
				if (url.includes("/ledgers")) return jsonOk(LEDGERS);
				if (url.includes("/dashboard")) return jsonOk(DASHBOARD);
				if (url.includes("/assets")) return jsonOk(ASSETS);
				if (url.includes("/accounts")) return jsonOk(ACCOUNTS);
				if (url.endsWith("/entries") && init?.method === "POST") {
					const body = JSON.parse(String(init.body)) as {
						amount: string;
						direction: "expense" | "income";
						category: string;
						note: string;
					};
					backend.posts.push({
						key: new Headers(init.headers).get("Idempotency-Key"),
						body,
					});
					return ok({ entry: entry({ ...body }), revisions: [] }, 201);
				}
				return jsonOk({ items: [], page: 1, page_size: 25, total: 0, pages: 0 });
			}),
		);
		renderApp("/");
		await screen.findByText("本月，保持清晰。");
		fireEvent.click(screen.getAllByRole("button", { name: /记一笔/ })[0]);
		const dialog = await screen.findByRole("heading", { name: "记一笔" });
		expect(dialog).toBeInTheDocument();
		const amount = await screen.findByLabelText(/金额/);
		fireEvent.change(amount, { target: { value: "28" } });
		fireEvent.click(screen.getByRole("button", { name: "餐饮" }));
		const note = await screen.findByLabelText("备注");
		fireEvent.change(note, { target: { value: "午饭" } });
		fireEvent.click(screen.getByRole("button", { name: "保存" }));
		await waitFor(() => {
			expect(backend.posts).toHaveLength(1);
		});
		expect(backend.posts[0].body).toMatchObject({
			amount: "28",
			direction: "expense",
			category: "餐饮",
			note: "午饭",
		});
		expect(backend.posts[0].key).toBeTruthy();
		// Success feedback.
		expect(await screen.findByText("已记下这笔")).toBeInTheDocument();
	});

	it("W05 creates an income via the direction toggle", async () => {
		const backend = makeBackend();
		vi.stubGlobal(
			"fetch",
			vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
				const url = String(input);
				if (url.endsWith("/me")) return jsonOk(ME);
				if (url.includes("/ledgers")) return jsonOk(LEDGERS);
				if (url.includes("/dashboard")) return jsonOk(DASHBOARD);
				if (url.includes("/assets")) return jsonOk(ASSETS);
				if (url.includes("/accounts")) return jsonOk(ACCOUNTS);
				if (url.endsWith("/entries") && init?.method === "POST") {
					const body = JSON.parse(String(init.body)) as {
						direction: "expense" | "income";
						amount: string;
					};
					backend.posts.push({ key: null, body });
					return ok(
						{
							entry: entry({ direction: body.direction, amount: body.amount }),
							revisions: [],
						},
						201,
					);
				}
				return jsonOk({ items: [], page: 1, page_size: 25, total: 0, pages: 0 });
			}),
		);
		renderApp("/");
		await screen.findByText("本月，保持清晰。");
		fireEvent.click(screen.getAllByRole("button", { name: /记一笔/ })[0]);
		await screen.findByRole("heading", { name: "记一笔" });
		fireEvent.click(screen.getByRole("button", { name: /收入/ }));
		const amount = await screen.findByLabelText(/金额/);
		fireEvent.change(amount, { target: { value: "18000" } });
		fireEvent.click(screen.getByRole("button", { name: "工资" }));
		fireEvent.click(screen.getByRole("button", { name: "保存" }));
		await waitFor(() => expect(backend.posts).toHaveLength(1));
		expect(backend.posts[0].body).toMatchObject({
			amount: "18000",
			direction: "income",
			category: "工资",
		});
	});

	it("W06 double submit is guarded by the disabled saving state", async () => {
		const backend = makeBackend();
		let resolveCreate: (value: Promise<Response>) => void = () => {};
		vi.stubGlobal(
			"fetch",
			vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
				const url = String(input);
				if (url.endsWith("/me")) return jsonOk(ME);
				if (url.includes("/ledgers")) return jsonOk(LEDGERS);
				if (url.includes("/dashboard")) return jsonOk(DASHBOARD);
				if (url.includes("/assets")) return jsonOk(ASSETS);
				if (url.includes("/accounts")) return jsonOk(ACCOUNTS);
				if (url.endsWith("/entries") && init?.method === "POST") {
					const body = JSON.parse(String(init.body)) as { amount: string };
					backend.posts.push({
						key: new Headers(init.headers).get("Idempotency-Key"),
						body,
					});
					return new Promise<Response>((resolve) => {
						resolveCreate = resolve;
					});
				}
				return jsonOk({ items: [], page: 1, page_size: 25, total: 0, pages: 0 });
			}),
		);
		renderApp("/");
		await screen.findByText("本月，保持清晰。");
		fireEvent.click(screen.getAllByRole("button", { name: /记一笔/ })[0]);
		await screen.findByRole("heading", { name: "记一笔" });
		const amount = await screen.findByLabelText(/金额/);
		fireEvent.change(amount, { target: { value: "28" } });
		fireEvent.click(screen.getByRole("button", { name: "餐饮" }));
		const save = screen.getByRole("button", { name: "保存" });
		fireEvent.click(save);
		await waitFor(() => expect(save).toBeDisabled());
		expect(screen.getByRole("button", { name: "保存中…" })).toBeInTheDocument();
		// While saving the button shows the busy state; a second click is
		// blocked because the mutation is pending and the button is disabled.
		fireEvent.click(save);
		expect(save).toBeDisabled();
		expect(backend.posts).toHaveLength(1);
		// The same Idempotency-Key would be replayed server-side on a real
		// retry — the client never fires a second request while pending.
		resolveCreate(
			ok({ entry: entry({ amount: "28.00" }), revisions: [] }, 201),
		);
		await waitFor(() => expect(backend.posts).toHaveLength(1));
		expect(await screen.findByText("已记下这笔")).toBeInTheDocument();
	});
});

describe("W07–W10 transactions edit, delete, restore and accounts", () => {
	function fetchWithDetail() {
		const backend = makeBackend();
		vi.stubGlobal(
			"fetch",
			vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
				const url = String(input);
				if (url.endsWith("/me")) return jsonOk(ME);
				if (url.includes("/accounts")) return jsonOk(ACCOUNTS);
				if (url.includes("/entries/A83F2") && init?.method === "PATCH") {
					backend.entries = [
						entry({
							amount: "30.00",
							note: "午餐涨价了",
							updated_at: "2026-08-10T05:00:00+00:00",
						}),
					];
					return ok(
						{
							entry: backend.entries[0],
							revisions: [
								{
									id: "rev-1",
									change_type: "update",
									before: { amount: "28.00" },
									after: { amount: "30.00" },
									created_at: "2026-08-10T05:00:00+00:00",
								},
							],
						},
						200,
					);
				}
				if (url.includes("/entries/A83F2") && init?.method === "DELETE") {
					backend.entries = [
						entry({ deleted_at: "2026-08-10T05:00:00+00:00" }),
					];
					return ok(
						{
							entry: backend.entries[0],
							revisions: [
								{
									id: "rev-2",
									change_type: "delete",
									before: {},
									after: {},
									created_at: "2026-08-10T05:00:00+00:00",
								},
							],
						},
						200,
					);
				}
				if (url.endsWith("/entries/A83F2/restore")) {
					backend.entries = [entry()];
					return ok(
						{
							entry: backend.entries[0],
							revisions: [
								{
									id: "rev-3",
									change_type: "restore",
									before: {},
									after: {},
									created_at: "2026-08-10T05:00:00+00:00",
								},
							],
						},
						200,
					);
				}
				if (url.includes("/entries/A83F2")) {
					return ok({ entry: backend.entries[0], revisions: [] }, 200);
				}
				const page: EntryPage = {
					items: backend.entries,
					page: 1,
					page_size: 25,
					total: backend.entries.length,
					pages: 1,
				};
				return jsonOk(page);
			}),
		);
		return backend;
	}

	it("W07 updates an entry amount and note", async () => {
		const backend = fetchWithDetail();
		renderApp("/entries?entry=A83F2");
		expect(await screen.findByRole("heading", { name: /28\.00/ })).toBeInTheDocument();
		fireEvent.click(screen.getByRole("button", { name: /修改/ }));
		const dialog = await screen.findByRole("heading", { name: /修改 #A83F2/ });
		expect(dialog).toBeInTheDocument();
		const amount = screen.getByLabelText("金额");
		fireEvent.change(amount, { target: { value: "30" } });
		fireEvent.click(screen.getByRole("button", { name: "保存修改" }));
		await waitFor(() => {
			expect(backend.entries[0].amount).toBe("30.00");
		});
		expect(await screen.findByText("操作已保存")).toBeInTheDocument();
	});

	it("W08 deletes an entry after an explicit confirmation dialog", async () => {
		const backend = fetchWithDetail();
		renderApp("/entries?entry=A83F2");
		await screen.findByRole("heading", { name: /28\.00/ });
		fireEvent.click(screen.getByRole("button", { name: /删除/ }));
		expect(
			await screen.findByRole("heading", { name: /确认删除 #A83F2/ }),
		).toBeInTheDocument();
		fireEvent.click(screen.getByRole("button", { name: "确认删除" }));
		await waitFor(() => {
			expect(backend.entries[0].deleted_at).not.toBeNull();
		});
	});

	it("W09 restores a deleted entry", async () => {
		const backend = fetchWithDetail();
		backend.entries = [entry({ deleted_at: "2026-08-10T05:00:00+00:00" })];
		renderApp("/entries?entry=A83F2");
		expect(await screen.findByRole("button", { name: /恢复/ })).toBeInTheDocument();
		fireEvent.click(screen.getByRole("button", { name: /恢复/ }));
		await waitFor(() => {
			expect(backend.entries[0].deleted_at).toBeNull();
		});
	});

	it("W10 shows accounts with private visibility badge", async () => {
		vi.stubGlobal(
			"fetch",
			vi.fn((input: RequestInfo | URL) => {
				const url = String(input);
				if (url.endsWith("/me")) return jsonOk(ME);
				if (url.includes("/accounts")) return jsonOk(ACCOUNTS);
				if (url.includes("/assets")) return jsonOk(ASSETS);
				return jsonOk({ items: [], page: 1, page_size: 25, total: 0, pages: 0 });
			}),
		);
		renderApp("/accounts");
		expect(await screen.findByText("支付宝")).toBeInTheDocument();
		expect(screen.getByText("私密钱包")).toBeInTheDocument();
		// The private badge is rendered (plus the toggle button label).
		expect(screen.getAllByText("私人").length).toBeGreaterThanOrEqual(2);
	});

	it("W11 private isolation: the private account is absent for a non-owner backend view", async () => {
		// The backend is authoritative: when a member fetches accounts, the
		// private row is simply not in the payload (404 at the API level is
		// covered by WEB12). The UI renders exactly what it receives.
		const memberAccounts: AccountList = { items: [ACCOUNTS.items[0]] };
		vi.stubGlobal(
			"fetch",
			vi.fn((input: RequestInfo | URL) => {
				const url = String(input);
				if (url.endsWith("/me")) return jsonOk(ME);
				if (url.includes("/accounts")) return jsonOk(memberAccounts);
				if (url.includes("/assets")) return jsonOk(ASSETS);
				return jsonOk({ items: [], page: 1, page_size: 25, total: 0, pages: 0 });
			}),
		);
		renderApp("/accounts");
		await screen.findByText("支付宝");
		expect(screen.queryByText("私密钱包")).not.toBeInTheDocument();
	});
});

describe("W12–W15 household, session expiry, CSRF and mobile", () => {
	it("W12 household ledger is switchable and its transactions render", async () => {
		const homeEntry = entry({
			id: "entry-home",
			short_id: "H9F11",
			note: "家庭支出",
		});
		vi.stubGlobal(
			"fetch",
			vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
				const url = String(input);
				if (url.endsWith("/me")) return jsonOk(ME);
				if (url.includes("/ledgers/ledger-home/select") && init?.method === "POST") {
					LEDGERS.items = LEDGERS.items.map((item) => ({
						...item,
						is_current: item.id === "ledger-home",
					}));
					return jsonOk(
						LEDGERS.items.find((item) => item.id === "ledger-home"),
					);
				}
				if (url.endsWith("/ledgers")) return jsonOk(LEDGERS);
				if (url.includes("/dashboard")) return jsonOk(DASHBOARD);
				if (url.includes("/assets")) return jsonOk(ASSETS);
				if (url.includes("/accounts")) return jsonOk(ACCOUNTS);
				return jsonOk({
					items: [homeEntry],
					page: 1,
					page_size: 25,
					total: 1,
					pages: 1,
				});
			}),
		);
		renderApp("/");
		await screen.findByText("本月，保持清晰。");
		const selector = await screen.findByLabelText("当前账本");
		fireEvent.change(selector, { target: { value: "ledger-home" } });
		await waitFor(() =>
			expect(
				vi
					.mocked(fetch)
					.mock.calls.some(
						([url, init]) =>
							String(url).endsWith("/ledgers/ledger-home/select") &&
							init?.method === "POST",
					),
			).toBe(true),
		);
		// Navigate to the shared ledger's transactions.
		renderApp("/entries");
		expect(await screen.findByText("家庭支出")).toBeInTheDocument();
	});

	it("W13 a 401 response returns the user to the login screen", async () => {
		vi.stubGlobal(
			"fetch",
			vi.fn((input: RequestInfo | URL) => {
				const url = String(input);
				if (url.endsWith("/me")) return jsonOk(ME);
				if (url.endsWith("/ledgers")) {
					// The session is revoked on the server: the ledger fetch
					// fails with 401 → auth-expired event → the app clears the
					// UI auth state and returns to the login page (Journey E).
					return Promise.resolve(
						new Response(JSON.stringify({ detail: "登录会话已失效" }), {
							status: 401,
							headers: { "Content-Type": "application/json" },
						}),
					);
				}
				if (url.includes("/dashboard")) return jsonOk(DASHBOARD);
				if (url.includes("/assets")) return jsonOk(ASSETS);
				return jsonOk({ items: [], page: 1, page_size: 25, total: 0, pages: 0 });
			}),
		);
		renderApp("/");
		expect(
			await screen.findByRole("link", { name: /使用飞书登录/ }),
		).toBeInTheDocument();
	});

	it("W14 CSRF: state-changing requests carry the X-CSRF-Token header", async () => {
		const backend = makeBackend();
		vi.stubGlobal(
			"fetch",
			vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
				const url = String(input);
				if (url.endsWith("/me")) return jsonOk(ME);
				if (url.includes("/ledgers")) return jsonOk(LEDGERS);
				if (url.includes("/dashboard")) return jsonOk(DASHBOARD);
				if (url.includes("/assets")) return jsonOk(ASSETS);
				if (url.includes("/accounts")) return jsonOk(ACCOUNTS);
				if (url.endsWith("/entries") && init?.method === "POST") {
					const headers = new Headers(init.headers);
					backend.headers["X-CSRF-Token"] = headers.get("X-CSRF-Token");
					return ok({ entry: entry(), revisions: [] }, 201);
				}
				return jsonOk({ items: [], page: 1, page_size: 25, total: 0, pages: 0 });
			}),
		);
		renderApp("/");
		await screen.findByText("本月，保持清晰。");
		fireEvent.click(screen.getAllByRole("button", { name: /记一笔/ })[0]);
		await screen.findByRole("heading", { name: "记一笔" });
		fireEvent.change(await screen.findByLabelText(/金额/), {
			target: { value: "28" },
		});
		fireEvent.click(screen.getByRole("button", { name: "餐饮" }));
		fireEvent.click(screen.getByRole("button", { name: "保存" }));
		await waitFor(() => {
			expect(backend.headers["X-CSRF-Token"]).toBe("test-csrf-token");
		});
	});

	it("W15 mobile: the quick-add button opens bookkeeping from any page", async () => {
		vi.stubGlobal(
			"fetch",
			vi.fn((input: RequestInfo | URL) => {
				const url = String(input);
				if (url.endsWith("/me")) return jsonOk(ME);
				if (url.includes("/ledgers")) return jsonOk(LEDGERS);
				if (url.includes("/dashboard")) return jsonOk(DASHBOARD);
				if (url.includes("/assets")) return jsonOk(ASSETS);
				if (url.includes("/accounts")) return jsonOk(ACCOUNTS);
				return jsonOk({ items: [], page: 1, page_size: 25, total: 0, pages: 0 });
			}),
		);
		renderApp("/accounts");
		await screen.findByText("支付宝");
		const fab = await screen.findByRole("button", { name: "记一笔" });
		expect(fab).toBeInTheDocument();
		// Tapping it opens the quick bookkeeping form in one step.
		fireEvent.click(fab);
		expect(await screen.findByRole("heading", { name: "记一笔" })).toBeInTheDocument();
	});
});
