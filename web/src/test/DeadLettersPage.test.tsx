import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import type {
	DeadLetterActionResponse,
	DeadLetterDetail,
	DeadLetterItem,
	DeadLetterPage,
} from "../api";
import { DeadLettersPage } from "../pages/AdminPages";

function renderPage(
	fetchMock: (
		input: RequestInfo | URL,
		init?: RequestInit,
	) => Promise<Response>,
) {
	const mock = vi.fn(fetchMock);
	vi.stubGlobal("fetch", mock);
	const client = new QueryClient({
		defaultOptions: { queries: { retry: false } },
	});
	return render(
		<QueryClientProvider client={client}>
			<MemoryRouter>
				<DeadLettersPage />
			</MemoryRouter>
		</QueryClientProvider>,
	);
}

afterEach(() => vi.unstubAllGlobals());

const unsafeEvent: DeadLetterItem = {
	id: "evt-400",
	source: "events",
	status: "dead",
	state: "dead",
	created_at: "2026-08-01T04:00:00Z",
	dead_at: "2026-08-02T04:00:00Z",
	attempts: 3,
	reason_category: "remote_rejected",
	retryable: false,
	replay_safe: false,
	requires_manual_review: true,
	terminal: false,
	payload_summary: "event/webhook",
	last_error_summary: "Client error '400 Bad Request'",
	resolved: false,
};

const safeOutbox: DeadLetterItem = {
	id: "outbox01",
	source: "outbox",
	status: "dead",
	state: "dead",
	created_at: "2026-08-01T05:00:00Z",
	dead_at: "2026-08-02T05:00:00Z",
	attempts: 2,
	reason_category: "network",
	retryable: true,
	replay_safe: true,
	requires_manual_review: false,
	terminal: false,
	payload_summary: "reply/text",
	last_error_summary: "Connection reset",
	resolved: false,
};

function pagePayload(items: DeadLetterItem[]): DeadLetterPage {
	return { items, page: 1, page_size: 50, total: items.length, pages: 1 };
}

function listOnlyMock(items: DeadLetterItem[]) {
	return (input: RequestInfo | URL) => {
		const url = String(input);
		if (url.includes("/admin/dead-letters")) {
			return Promise.resolve(Response.json(pagePayload(items)));
		}
		return Promise.resolve(Response.json({}));
	};
}

function sourceSelect() {
	return screen.getAllByRole("combobox")[0];
}

describe("DeadLettersPage", () => {
	it("renders summary cards, table rows and replayability badges", async () => {
		renderPage(listOnlyMock([unsafeEvent, safeOutbox]));
		expect(
			await screen.findByRole("heading", { name: "Dead Letters" }),
		).toBeInTheDocument();
		expect(await screen.findByText("共 2 项")).toBeInTheDocument();
		// summary cards: per-source counts + derived buckets
		const eventCards = screen.getAllByText("事件");
		expect(eventCards.some((el) => el.tagName === "H3")).toBe(true);
		const eventCount = eventCards
			.find((el) => el.tagName === "H3")!
			.closest("section")!
			.querySelector("span")!;
		expect(eventCount).toHaveTextContent("1");
		expect(screen.getByText("可重试")).toBeInTheDocument();
		expect(screen.getByText("需人工审查")).toBeInTheDocument();
		// table rows with status / reason / replayability
		expect(screen.getByText("evt-400")).toBeInTheDocument();
		expect(screen.getByText("outbox01")).toBeInTheDocument();
		expect(screen.getAllByText("远端拒绝").length).toBeGreaterThan(0);
		expect(screen.getByText("可安全重放")).toBeInTheDocument();
		expect(screen.getByText("需审查")).toBeInTheDocument();
		expect(screen.getAllByRole("button", { name: "详情" })).toHaveLength(2);
	});

	it("shows the loading skeleton while the list is pending", () => {
		renderPage(
			() =>
				new Promise<Response>(() => {
					/* never resolves — keeps the query in the loading state */
				}),
		);
		expect(
			screen.getByRole("status", { name: "正在加载" }),
		).toBeInTheDocument();
	});

	it("shows an error panel and refetches on retry", async () => {
		let calls = 0;
		renderPage((input) => {
			const url = String(input);
			if (url.includes("/admin/dead-letters")) {
				calls += 1;
				if (calls === 1) {
					return Promise.resolve(
						new Response(JSON.stringify({ detail: "boom" }), {
							status: 500,
							headers: { "Content-Type": "application/json" },
						}),
					);
				}
				return Promise.resolve(Response.json(pagePayload([safeOutbox])));
			}
			return Promise.resolve(Response.json({}));
		});
		expect(
			await screen.findByRole("heading", { name: "加载失败" }),
		).toBeInTheDocument();
		fireEvent.click(screen.getByRole("button", { name: "重试" }));
		expect(await screen.findByText("outbox01")).toBeInTheDocument();
		expect(calls).toBeGreaterThanOrEqual(2);
	});

	it("shows the empty state when nothing matches", async () => {
		renderPage(listOnlyMock([]));
		expect(
			await screen.findByRole("heading", { name: "没有匹配的 Dead Letter" }),
		).toBeInTheDocument();
		expect(
			screen.queryByRole("button", { name: "详情" }),
		).not.toBeInTheDocument();
	});

	it("opens the detail drawer with audit history", async () => {
		const detail: DeadLetterDetail = {
			...safeOutbox,
			event_id: null,
			message_id: "om_xxx",
			reply_type: "text",
			transport: "feishu",
			lease_owner: null,
			lease_expires_at: null,
			remote_message_id: null,
			next_attempt_at: null,
			updated_at: "2026-08-02T05:00:00Z",
			audit: [
				{
					action: "resolve",
					operator: "ou_admin",
					reason: "historical fixture",
					before_status: "dead",
					after_status: "dead",
					error_code: null,
					request_id: "req-1",
					created_at: "2026-08-03T01:00:00Z",
				},
			],
		};
		renderPage((input) => {
			const url = String(input);
			if (url.endsWith("/admin/dead-letters/outbox/outbox01")) {
				return Promise.resolve(Response.json(detail));
			}
			if (url.includes("/admin/dead-letters")) {
				return Promise.resolve(Response.json(pagePayload([safeOutbox])));
			}
			return Promise.resolve(Response.json({}));
		});
		fireEvent.click(await screen.findByRole("button", { name: "详情" }));
		expect(
			await screen.findByRole("heading", { name: /outbox: outbox01/ }),
		).toBeInTheDocument();
		expect(screen.getByText("Connection reset")).toBeInTheDocument();
		// audit history renders operator and reason
		expect(
			await screen.findByRole("heading", { name: "审计历史" }),
		).toBeInTheDocument();
		expect(screen.getByText("ou_admin")).toBeInTheDocument();
		expect(screen.getByText("historical fixture")).toBeInTheDocument();
	});

	it("disables replay for terminal and unsafe rows and requires a reason", async () => {
		const terminal: DeadLetterItem = {
			...unsafeEvent,
			terminal: true,
			state: "terminal",
		};
		renderPage((input) => {
			const url = String(input);
			if (url.endsWith("/admin/dead-letters/events/evt-400")) {
				return Promise.resolve(
					Response.json({ ...terminal, event_id: "evt-400", audit: [] }),
				);
			}
			if (url.includes("/admin/dead-letters")) {
				return Promise.resolve(Response.json(pagePayload([terminal])));
			}
			return Promise.resolve(Response.json({}));
		});
		fireEvent.click(await screen.findByRole("button", { name: "详情" }));
		const replayButton = await screen.findByRole("button", { name: "重放" });
		// terminal rows are not replayable, and the reason field is empty anyway
		expect(replayButton).toBeDisabled();
		expect(replayButton).toHaveAttribute("title", "该记录不可重放");
		const resolveButton = screen.getByRole("button", {
			name: "解决（不重放）",
		});
		expect(resolveButton).toBeDisabled(); // reason too short
		fireEvent.change(screen.getByPlaceholderText(/记录调查结论/), {
			target: { value: "investigated and closed" },
		});
		await waitFor(() => expect(resolveButton).not.toBeDisabled());
		expect(replayButton).toBeDisabled(); // still terminal
	});

	it("keeps replay disabled while the reason is shorter than 3 characters", async () => {
		renderPage((input) => {
			const url = String(input);
			if (url.endsWith("/admin/dead-letters/outbox/outbox01")) {
				return Promise.resolve(
					Response.json({ ...safeOutbox, event_id: null, audit: [] }),
				);
			}
			if (url.includes("/admin/dead-letters")) {
				return Promise.resolve(Response.json(pagePayload([safeOutbox])));
			}
			return Promise.resolve(Response.json({}));
		});
		fireEvent.click(await screen.findByRole("button", { name: "详情" }));
		const replayButton = await screen.findByRole("button", { name: "重放" });
		expect(replayButton).toBeDisabled();
		fireEvent.change(screen.getByPlaceholderText(/记录调查结论/), {
			target: { value: "ab" },
		});
		expect(replayButton).toBeDisabled();
		fireEvent.change(screen.getByPlaceholderText(/记录调查结论/), {
			target: { value: "transient, retry now" },
		});
		await waitFor(() => expect(replayButton).not.toBeDisabled());
		expect(replayButton).toHaveAttribute("title", "重新入队，由 Worker 投递");
	});

	it("posts a replay with the reason and shows the notice", async () => {
		const action: DeadLetterActionResponse = {
			source: "outbox",
			target_id: "outbox01",
			action: "replay",
			outcome: "requeued",
			before_status: "dead",
			after_status: "pending",
			audit_id: "audit-1",
			message: "回复已重新入队，将由回复 Worker 按正常租约路径投递",
		};
		renderPage((input, init) => {
			const url = String(input);
			if (
				url.endsWith("/admin/dead-letters/outbox/outbox01/replay") &&
				init?.method === "POST"
			) {
				return Promise.resolve(Response.json(action));
			}
			if (url.endsWith("/admin/dead-letters/outbox/outbox01")) {
				return Promise.resolve(
					Response.json({ ...safeOutbox, event_id: null, audit: [] }),
				);
			}
			if (url.includes("/admin/dead-letters")) {
				return Promise.resolve(Response.json(pagePayload([safeOutbox])));
			}
			return Promise.resolve(Response.json({}));
		});
		fireEvent.click(await screen.findByRole("button", { name: "详情" }));
		fireEvent.change(await screen.findByPlaceholderText(/记录调查结论/), {
			target: { value: "dependency recovered" },
		});
		fireEvent.click(await screen.findByRole("button", { name: "重放" }));
		expect(
			await screen.findByText(
				"回复已重新入队，将由回复 Worker 按正常租约路径投递",
			),
		).toBeInTheDocument();
		const mock = vi.mocked(fetch);
		const post = mock.mock.calls.find((call) => call[1]?.method === "POST");
		expect(post).toBeDefined();
		expect(String(post![0])).toContain(
			"/admin/dead-letters/outbox/outbox01/replay",
		);
		expect(JSON.parse(String(post![1]?.body))).toEqual({
			reason: "dependency recovered",
		});
	});

	it("resolves without replaying and marks the item resolved", async () => {
		const action: DeadLetterActionResponse = {
			source: "events",
			target_id: "evt-400",
			action: "resolve",
			outcome: "resolved",
			before_status: "dead",
			after_status: "dead",
			audit_id: "audit-2",
			message: "已记录解决标记；源记录保留，仅用于审计追溯",
		};
		renderPage((input, init) => {
			const url = String(input);
			if (
				url.endsWith("/admin/dead-letters/events/evt-400/resolve") &&
				init?.method === "POST"
			) {
				return Promise.resolve(Response.json(action));
			}
			if (url.endsWith("/admin/dead-letters/events/evt-400")) {
				return Promise.resolve(
					Response.json({ ...unsafeEvent, event_id: "evt-400", audit: [] }),
				);
			}
			if (url.includes("/admin/dead-letters")) {
				return Promise.resolve(Response.json(pagePayload([unsafeEvent])));
			}
			return Promise.resolve(Response.json({}));
		});
		fireEvent.click(await screen.findByRole("button", { name: "详情" }));
		fireEvent.change(await screen.findByPlaceholderText(/记录调查结论/), {
			target: { value: "permanent failure, acknowledge" },
		});
		fireEvent.click(
			await screen.findByRole("button", { name: "解决（不重放）" }),
		);
		expect(
			await screen.findByText("已记录解决标记；源记录保留，仅用于审计追溯"),
		).toBeInTheDocument();
		expect(await screen.findByText(/已解决/)).toBeInTheDocument();
		const mock = vi.mocked(fetch);
		const post = mock.mock.calls.find((call) => call[1]?.method === "POST");
		expect(post).toBeDefined();
		expect(String(post![0])).toContain(
			"/admin/dead-letters/events/evt-400/resolve",
		);
		expect(JSON.parse(String(post![1]?.body))).toEqual({
			reason: "permanent failure, acknowledge",
		});
	});

	it("refetches the list when a source filter changes", async () => {
		renderPage((input) => {
			const url = String(input);
			if (url.includes("/admin/dead-letters")) {
				return Promise.resolve(Response.json(pagePayload([safeOutbox])));
			}
			return Promise.resolve(Response.json({}));
		});
		await screen.findByText("outbox01");
		const mock = vi.mocked(fetch);
		const baseline = mock.mock.calls.length;
		fireEvent.change(sourceSelect(), { target: { value: "events" } });
		await waitFor(() => {
			const listCall = mock.mock.calls
				.slice(baseline)
				.find((call) => String(call[0]).includes("/admin/dead-letters?"));
			expect(listCall).toBeDefined();
			expect(String(listCall![0])).toContain("source=events");
		});
	});
});
