import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ReportsPage } from "../pages/FinancePages";
import { monthRange } from "../components/ReportDateRangePicker";

const REPORT_MARCH = {
	range_start: "2025-03-01T00:00:00+08:00",
	range_end: "2025-04-01T00:00:00+08:00",
	currency: "CNY",
	income_total: "100.00",
	expense_total: "40.00",
	balance: "60.00",
	entry_count: 2,
	categories: [{ category: "餐饮", amount: "40.00" }],
	trend: [{ period: "2025-03-01", amount: "40.00" }],
	trend_granularity: "day" as const,
};

const REPORT_CUSTOM = {
	range_start: "2025-10-15T00:00:00+08:00",
	range_end: "2026-03-21T00:00:00+08:00",
	currency: "CNY",
	income_total: "300.00",
	expense_total: "90.00",
	balance: "210.00",
	entry_count: 3,
	categories: [{ category: "交通", amount: "90.00" }],
	trend: [{ period: "2025-10-01", amount: "90.00" }],
	trend_granularity: "month" as const,
};

function LocationProbe() {
	const location = useLocation();
	return <output data-testid="location-search">{location.search}</output>;
}

function renderReports(path: string) {
	const client = new QueryClient({
		defaultOptions: { queries: { retry: false } },
	});
	return render(
		<QueryClientProvider client={client}>
			<MemoryRouter initialEntries={[path]}>
				<LocationProbe />
				<ReportsPage />
			</MemoryRouter>
		</QueryClientProvider>,
	);
}

function response(payload: unknown) {
	return Promise.resolve(
		new Response(JSON.stringify(payload), {
			status: 200,
			headers: { "Content-Type": "application/json" },
		}),
	);
}

describe("ReportsPage date range integration", () => {
	beforeEach(() => {
		vi.useFakeTimers({ toFake: ["Date"] });
		vi.setSystemTime(new Date(2026, 8, 15, 12));
	});

	afterEach(() => {
		vi.useRealTimers();
		vi.unstubAllGlobals();
	});

	it("restores a URL range and renders every report section from one response", async () => {
		const calls: string[] = [];
		vi.stubGlobal(
			"fetch",
			vi.fn((input: RequestInfo | URL) => {
				const url = String(input);
				calls.push(url);
				return response(REPORT_MARCH);
			}),
		);

		renderReports("/reports?from=2025-03-01&to=2025-03-31");
		expect(await screen.findByText("¥100.00")).toBeInTheDocument();
		expect(screen.getAllByText("¥40.00")).toHaveLength(2);
		expect(screen.getByText("¥60.00")).toBeInTheDocument();
		expect(screen.getByText("餐饮")).toBeInTheDocument();
		expect(screen.getByText("2 笔")).toBeInTheDocument();
		expect(screen.getByText("每日")).toBeInTheDocument();
		expect(
			screen.getByRole("button", { name: /当前为2025年3月/ }),
		).toBeInTheDocument();
		await waitFor(() => {
			expect(calls).toContain(
				"/api/web/v1/reports?start_date=2025-03-01&end_date=2025-03-31",
			);
		});
		expect(screen.getByTestId("location-search")).toHaveTextContent(
			"from=2025-03-01&to=2025-03-31",
		);
	});

	it("updates the URL and refreshes KPIs, trend, categories and count together", async () => {
		const calls: string[] = [];
		vi.stubGlobal(
			"fetch",
			vi.fn((input: RequestInfo | URL) => {
				const url = String(input);
				calls.push(url);
				return response(
					url.includes("2025-10-15") ? REPORT_CUSTOM : REPORT_MARCH,
				);
			}),
		);

		renderReports("/reports?from=2025-03-01&to=2025-03-31");
		await screen.findByText("¥100.00");
		fireEvent.click(screen.getByRole("button", { name: /选择报告时间范围/ }));
		fireEvent.click(screen.getByRole("tab", { name: "自定义" }));
		fireEvent.change(screen.getByLabelText("开始日期"), {
			target: { value: "2025-10-15" },
		});
		fireEvent.change(screen.getByLabelText("结束日期"), {
			target: { value: "2026-03-20" },
		});
		fireEvent.click(screen.getByRole("button", { name: "应用" }));

		await waitFor(() => {
			expect(screen.getByTestId("location-search")).toHaveTextContent(
				"from=2025-10-15&to=2026-03-20",
			);
			expect(calls).toContain(
				"/api/web/v1/reports?start_date=2025-10-15&end_date=2026-03-20",
			);
		});
		expect(await screen.findByText("¥300.00")).toBeInTheDocument();
		expect(screen.getAllByText("¥90.00")).toHaveLength(2);
		expect(screen.getByText("¥210.00")).toBeInTheDocument();
		expect(screen.getByText("交通")).toBeInTheDocument();
		expect(screen.getByText("3 笔")).toBeInTheDocument();
		expect(screen.getByText("每月")).toBeInTheDocument();
	});

	it("renders a zero-valued report as an empty business state", async () => {
		const emptyReport = {
			...REPORT_MARCH,
			income_total: "0.00",
			expense_total: "0.00",
			balance: "0.00",
			entry_count: 0,
			categories: [],
			trend: [],
		};
		vi.stubGlobal("fetch", vi.fn(() => response(emptyReport)));

		renderReports("/reports?from=2026-09-01&to=2026-09-30");
		expect(
			await screen.findByRole("heading", { name: "本月暂无收支记录" }),
		).toBeInTheDocument();
		expect(screen.getByText("0 笔")).toBeInTheDocument();
		expect(screen.getByRole("link", { name: "记一笔" })).toHaveAttribute(
			"href",
			"/entries?new=1",
		);
		expect(screen.queryByText("报告加载失败")).not.toBeInTheDocument();
	});

	it("shows a report error and retries the failed request", async () => {
		let attempts = 0;
		vi.stubGlobal(
			"fetch",
			vi.fn(() => {
				attempts += 1;
				if (attempts === 1) {
					return Promise.resolve(
						new Response(JSON.stringify({ detail: "服务暂不可用" }), {
							status: 503,
							headers: { "Content-Type": "application/json" },
						}),
					);
				}
				return response(REPORT_MARCH);
			}),
		);

		renderReports("/reports?from=2026-09-01&to=2026-09-30");
		expect(
			await screen.findByRole("heading", { name: "报表加载失败" }),
		).toBeInTheDocument();
		fireEvent.click(screen.getByRole("button", { name: "重试" }));
		expect(await screen.findByText("¥100.00")).toBeInTheDocument();
		expect(attempts).toBe(2);
	});

	it("falls back and removes unsafe URL parameters", async () => {
		const calls: string[] = [];
		vi.stubGlobal(
			"fetch",
			vi.fn((input: RequestInfo | URL) => {
				calls.push(String(input));
				return response(REPORT_MARCH);
			}),
		);

		renderReports("/reports?from=2026-09-31&to=2026-08-01");
		await screen.findByText("¥100.00");
		const fallback = monthRange(2026, 9);
		await waitFor(() => {
			expect(calls).toContain(
				`/api/web/v1/reports?start_date=${fallback.startDate}&end_date=${fallback.endDate}`,
			);
			expect(screen.getByTestId("location-search")).toHaveTextContent("");
		});
	});
});
