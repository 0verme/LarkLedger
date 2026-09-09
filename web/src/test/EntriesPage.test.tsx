import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { EntriesPage } from "../pages/EntriesPage";

function LocationProbe() {
	const location = useLocation();
	return <output data-testid="location-search">{location.search}</output>;
}

function renderEntries(path: string) {
	const client = new QueryClient({
		defaultOptions: { queries: { retry: false } },
	});
	return render(
		<QueryClientProvider client={client}>
			<MemoryRouter initialEntries={[path]}>
				<LocationProbe />
				<EntriesPage />
			</MemoryRouter>
		</QueryClientProvider>,
	);
}

function stubEntriesApi() {
	vi.stubGlobal(
		"fetch",
		vi.fn((input: RequestInfo | URL) => {
			if (String(input).includes("/accounts"))
				return Promise.resolve(Response.json({ items: [] }));
			return Promise.resolve(
				Response.json({ items: [], page: 1, page_size: 25, total: 0, pages: 0 }),
			);
		}),
	);
}

afterEach(() => vi.unstubAllGlobals());

describe("EntriesPage filter clearing", () => {
	it("clears filters, resets the page, and preserves sorting", async () => {
		stubEntriesApi();
		renderEntries(
			"/entries?search=%E5%8D%88%E9%A5%AD&direction=expense&category=%E9%A4%90%E9%A5%AE&source_type=text&amount_min=10&amount_max=100&start=2026-08-01T00%3A00%3A00.000Z&end=2026-08-31T00%3A00%3A00.000Z&deleted=all&sort=amount&order=asc&page=3",
		);

		expect(
			await screen.findByRole("heading", { name: "每一笔，都可追溯。" }),
		).toBeInTheDocument();
		const clearButton = screen.getByRole("button", { name: "清除筛选" });
		expect(clearButton).toBeEnabled();

		fireEvent.click(clearButton);

		await waitFor(() => {
			const query = new URLSearchParams(
				screen.getByTestId("location-search").textContent ?? "",
			);
			for (const key of [
				"search",
				"direction",
				"category",
				"source_type",
				"amount_min",
				"amount_max",
				"start",
				"end",
				"deleted",
			]) {
				expect(query.has(key)).toBe(false);
			}
			expect(query.get("page")).toBe("1");
			expect(query.get("sort")).toBe("amount");
			expect(query.get("order")).toBe("asc");
		});
		expect(screen.getByPlaceholderText("搜索备注、分类或短 ID")).toHaveValue("");
	});

	it("disables clearing when there are no active filters", async () => {
		stubEntriesApi();
		renderEntries("/entries?sort=amount&order=asc&page=2");

		await screen.findByRole("heading", { name: "每一笔，都可追溯。" });
		expect(
			screen.getByRole("button", { name: "清除筛选" }),
		).toBeDisabled();
	});
});
