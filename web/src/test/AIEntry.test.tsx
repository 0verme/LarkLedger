import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AIEntryPanel } from "../components/AIEntryPanel";
import type { AIEntryResult } from "../api";

// P39 frontend matrix (WAI01–WAI11): the AI entry panel must distinguish
// executed / confirmation_required / clarification_required / error, stay
// idempotent, handle 401 and work on a mobile viewport — never a bare toast.

function result(overrides: Partial<AIEntryResult> = {}): AIEntryResult {
	return {
		status: "executed",
		message: "已记录 A83F2 支出 ¥28.00 · 餐饮（午餐）",
		request_id: "ai:test-1",
		replayed: false,
		operation: "create",
		resource_id: "entry-1",
		amount: "28.00",
		direction: "expense",
		category: "餐饮",
		account: null,
		occurred_at: "2026-08-10T04:30:00+00:00",
		pending_command_id: null,
		confirmation_code: null,
		risk: null,
		expires_at: null,
		preview: null,
		missing_fields: [],
		...overrides,
	};
}

function jsonOk(payload: unknown) {
	return Promise.resolve(
		new Response(JSON.stringify(payload), {
			status: 200,
			headers: { "Content-Type": "application/json" },
		}),
	);
}

function renderPanel(onDone = vi.fn()) {
	const client = new QueryClient({
		defaultOptions: { queries: { retry: false } },
	});
	const utils = render(
		<QueryClientProvider client={client}>
			<AIEntryPanel onDone={onDone} />
		</QueryClientProvider>,
	);
	return { ...utils, onDone };
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

describe("WAI01–WAI11 AI entry panel", () => {
	it("WAI01 shows the natural-language input and send button", () => {
		renderPanel();
		expect(screen.getByRole("textbox", { name: "AI 记账输入" })).toBeInTheDocument();
		expect(screen.getByRole("button", { name: /发送/ })).toBeInTheDocument();
		expect(screen.getByText(/直接说一句/)).toBeInTheDocument();
	});

	it("WAI02 submits 午饭28 with an Idempotency-Key and CSRF header", async () => {
		// ``vi.fn`` keeps the tuple arity of ``mock.calls`` for the typechecker;
		// the parameters are unused because assertions read ``mock.calls``.
		const fetchMock = vi.fn((_input: RequestInfo | URL, _init?: RequestInit) =>
			jsonOk(result()),
		);
		void fetchMock;
		vi.stubGlobal("fetch", fetchMock);
		const { onDone } = renderPanel();
		const input = screen.getByRole("textbox", { name: "AI 记账输入" });
		fireEvent.change(input, { target: { value: "午饭28" } });
		fireEvent.click(screen.getByRole("button", { name: /发送/ }));
		await waitFor(() => expect(onDone).toHaveBeenCalled());
		const call = fetchMock.mock.calls[0]!;
		const url = String(call[0]);
		const init = call[1] as RequestInit | undefined;
		expect(url).toContain("/api/web/v1/ai/entries");
		// Headers are normalized to lowercase by the fetch Headers spec.
		const headers = Object.fromEntries(
			new Headers(init?.headers).entries(),
		) as Record<string, string>;
		const csrf = Object.entries(headers).find(
			([key]) => key.toLowerCase() === "x-csrf-token",
		);
		expect(csrf?.[1]).toBe("test-csrf-token");
		expect(headers["idempotency-key"]).toBeTruthy();
		expect(JSON.parse(String(init?.body))).toEqual({ text: "午饭28" });
	});

	it("WAI03 executed refreshes the ledger and shows the result", async () => {
		vi.stubGlobal("fetch", vi.fn(() => jsonOk(result())));
		const { onDone } = renderPanel();
		const input = screen.getByRole("textbox", { name: "AI 记账输入" });
		fireEvent.change(input, { target: { value: "午饭28" } });
		fireEvent.click(screen.getByRole("button", { name: /发送/ }));
		await waitFor(() => expect(onDone).toHaveBeenCalled());
		// onDone → DashboardPage invalidates dashboard/assets/entries queries.
		expect(onDone).toHaveBeenCalledTimes(1);
		expect(await screen.findByText(/已记录 A83F2 支出 ¥28.00/)).toBeInTheDocument();
		// The input is cleared after a successful write.
		expect(input).toHaveValue("");
	});

	it("WAI04 send is disabled while empty or submitting", async () => {
		let release: ((value: Response | PromiseLike<Response>) => void) = () => undefined;
		vi.stubGlobal(
			"fetch",
			vi.fn(
				() =>
					new Promise<Response>((resolve) => {
						release = resolve;
					}),
			),
		);
		renderPanel();
		const input = screen.getByRole("textbox", { name: "AI 记账输入" });
		// Empty input: the send button is disabled.
		expect(screen.getByRole("button", { name: /发送/ })).toBeDisabled();
		fireEvent.change(input, { target: { value: "午饭28" } });
		await screen.findByRole("button", { name: /发送/ });
		expect(screen.getByRole("button", { name: /发送/ })).toBeEnabled();
		fireEvent.click(screen.getByRole("button", { name: /发送/ }));
		// While the AI provider is pending the button is disabled and labelled 解析中.
		const during = await screen.findByRole("button", { name: /解析中…/ });
		expect(during).toBeDisabled();
		release(jsonOk(result()));
		// After success the input is cleared, so the send button returns to its
		// disabled (empty input) state and the result panel appears.
		await waitFor(
			() => expect(screen.getByRole("button", { name: /发送/ })).toBeDisabled(),
			{ timeout: 5000 },
		);
		expect(await screen.findByText(/已记录 A83F2 支出 ¥28.00/)).toBeInTheDocument();
	});

	it("WAI05 error status renders a safe message with request id", async () => {
		vi.stubGlobal(
			"fetch",
			vi.fn(() =>
				jsonOk(
					result({
						status: "error",
						message: "这笔操作暂时无法完成，请稍后重试或换一种说法。",
						request_id: "ai:failed-9",
					}),
				),
			),
		);
		const { onDone } = renderPanel();
		const input = screen.getByRole("textbox", { name: "AI 记账输入" });
		fireEvent.change(input, { target: { value: "午饭28" } });
		fireEvent.click(screen.getByRole("button", { name: /发送/ }));
		expect(await screen.findByText(/这笔操作暂时无法完成/)).toBeInTheDocument();
		expect(screen.getByText(/ai:failed-9/)).toBeInTheDocument();
		expect(onDone).not.toHaveBeenCalled();
	});

	it("WAI06 clarification_required keeps the input editable", async () => {
		vi.stubGlobal(
			"fetch",
			vi.fn(() =>
				jsonOk(
					result({
						status: "clarification_required",
						message: "这笔 28 元是收入还是支出？请补充后重新发送。",
						request_id: "ai:clarify-1",
					}),
				),
			),
		);
		const { onDone } = renderPanel();
		const input = screen.getByRole("textbox", { name: "AI 记账输入" });
		fireEvent.change(input, { target: { value: "记一笔28" } });
		fireEvent.click(screen.getByRole("button", { name: /发送/ }));
		expect(await screen.findByText(/这笔 28 元是收入还是支出/)).toBeInTheDocument();
		expect(screen.getByText(/需要补充信息/)).toBeInTheDocument();
		// The user can amend the sentence and resend — the input is NOT cleared.
		expect(input).toHaveValue("记一笔28");
		expect(onDone).not.toHaveBeenCalled();
	});

	it("WAI07 confirmation_required shows the in-app confirm dialog", async () => {
		const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(false);
		vi.stubGlobal(
			"fetch",
			vi.fn(() =>
				jsonOk(
					result({
						status: "confirmation_required",
						message: "这项操作需要确认。",
						request_id: "ai:confirm-1",
						pending_command_id: "CABC01",
						confirmation_code: "CABC01",
						risk: "delete_entry",
						expires_at: "2026-08-10T06:00:00+00:00",
						preview: { items: [{ label: "删除", amount: "28.00" }] },
					}),
				),
			),
		);
		renderPanel();
		const input = screen.getByRole("textbox", { name: "AI 记账输入" });
		fireEvent.change(input, { target: { value: "删除上一笔" } });
		fireEvent.click(screen.getByRole("button", { name: /发送/ }));
		expect(await screen.findByText("确认操作")).toBeInTheDocument();
		expect(screen.getByRole("button", { name: /确认执行/ })).toBeInTheDocument();
		// Never the browser-native confirm.
		expect(confirmSpy).not.toHaveBeenCalled();
		confirmSpy.mockRestore();
	});

	it("WAI08 confirming executes exactly once and refreshes", async () => {
		const calls: string[] = [];
		vi.stubGlobal(
			"fetch",
			vi.fn((_input: RequestInfo | URL) => {
				const url = String(_input);
				calls.push(url);
				if (url.endsWith("/ai/entries")) {
					return jsonOk(
						result({
							status: "confirmation_required",
							message: "这项操作需要确认。",
							request_id: "ai:confirm-2",
							pending_command_id: "CABC02",
							confirmation_code: "CABC02",
							risk: "delete_entry",
						}),
					);
				}
				if (url.includes("/pending/CABC02/confirm")) {
					return jsonOk({ message: "已执行", pending: {} });
				}
				return jsonOk(result());
			}),
		);
		const { onDone } = renderPanel();
		const input = screen.getByRole("textbox", { name: "AI 记账输入" });
		fireEvent.change(input, { target: { value: "删除上一笔" } });
		fireEvent.click(screen.getByRole("button", { name: /发送/ }));
		await screen.findByText("确认操作");
		fireEvent.click(screen.getByRole("button", { name: /确认执行/ }));
		await waitFor(() => expect(onDone).toHaveBeenCalled());
		expect(calls.filter((url) => url.includes("/pending/CABC02/confirm")).length).toBe(1);
	});

	it("WAI09 session 401 shows the safe sign-in message", async () => {
		vi.stubGlobal(
			"fetch",
			vi.fn(() =>
				Promise.resolve(
					new Response(JSON.stringify({ detail: null }), {
						status: 401,
						headers: { "Content-Type": "application/json" },
					}),
				),
			),
		);
		renderPanel();
		const input = screen.getByRole("textbox", { name: "AI 记账输入" });
		fireEvent.change(input, { target: { value: "午饭28" } });
		fireEvent.click(screen.getByRole("button", { name: /发送/ }));
		expect(await screen.findByText(/登录已失效，请重新登录/)).toBeInTheDocument();
	});

	it("WAI10 duplicate replay reports replayed=true without a second write", async () => {
		vi.stubGlobal(
			"fetch",
			vi.fn(() => jsonOk(result({ replayed: true }))),
		);
		const { onDone } = renderPanel();
		const input = screen.getByRole("textbox", { name: "AI 记账输入" });
		fireEvent.change(input, { target: { value: "午饭28" } });
		fireEvent.click(screen.getByRole("button", { name: /发送/ }));
		expect(await screen.findByText(/已按原请求返回，未重复记账/)).toBeInTheDocument();
		expect(onDone).toHaveBeenCalled();
	});

	it("WAI11 renders on a mobile viewport", async () => {
		vi.stubGlobal("fetch", vi.fn(() => jsonOk(result())));
		window.innerWidth = 390;
		renderPanel();
		const input = screen.getByRole("textbox", { name: "AI 记账输入" });
		fireEvent.change(input, { target: { value: "打车35" } });
		fireEvent.click(screen.getByRole("button", { name: /发送/ }));
		expect(await screen.findByText(/已记录 A83F2 支出 ¥28.00/)).toBeInTheDocument();
	});
});
