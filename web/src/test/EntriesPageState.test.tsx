import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, fireEvent } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { EntriesPage } from "../pages/EntriesPage";

function renderEntries(path: string) {
	const client = new QueryClient({
		defaultOptions: { queries: { retry: false } },
	});
	return render(
		<QueryClientProvider client={client}>
			<MemoryRouter initialEntries={[path]}>
				<EntriesPage />
			</MemoryRouter>
		</QueryClientProvider>,
	);
}

const emptyPage = {
	items: [],
	page: 1,
	page_size: 25,
	total: 0,
	pages: 0,
};

function jsonResponse(payload: unknown, status = 200) {
	return Promise.resolve(
		new Response(JSON.stringify(payload), {
			status,
			headers: { "Content-Type": "application/json" },
		}),
	);
}

afterEach(() => vi.unstubAllGlobals());

describe("EntriesPage state semantics", () => {
	it("distinguishes filtered empty from a first-use empty list", async () => {
		vi.stubGlobal(
			"fetch",
			vi.fn((input: RequestInfo | URL) => {
				return String(input).includes("/accounts")
					? jsonResponse({ items: [] })
					: jsonResponse(emptyPage);
			}),
		);

		renderEntries("/entries?category=%E9%A4%90%E9%A5%AE");
		expect(
			await screen.findByRole("heading", { name: "当前筛选条件下暂无结果" }),
		).toBeInTheDocument();
		fireEvent.click(screen.getAllByRole("button", { name: "清除筛选" })[0]);
		expect(await screen.findByRole("heading", { name: "还没有流水" })).toBeInTheDocument();
	});

	it("uses the shared error state and retries the list request", async () => {
		let entriesAttempts = 0;
		vi.stubGlobal(
			"fetch",
			vi.fn((input: RequestInfo | URL) => {
				if (String(input).includes("/accounts")) return jsonResponse({ items: [] });
				entriesAttempts += 1;
				return entriesAttempts === 1
					? jsonResponse({ detail: "服务暂不可用" }, 503)
					: jsonResponse(emptyPage);
			}),
		);

		renderEntries("/entries");
		expect(
			await screen.findByRole("heading", { name: "流水加载失败" }),
		).toBeInTheDocument();
		fireEvent.click(screen.getByRole("button", { name: "重试" }));
		expect(await screen.findByRole("heading", { name: "还没有流水" })).toBeInTheDocument();
		expect(entriesAttempts).toBe(2);
	});
});
