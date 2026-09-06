import { fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { beforeEach, describe, expect, it } from "vitest";
import {
	formatReportRangeLabel,
	getReportDateRange,
	isValidDateRange,
	monthRange,
	parseDateValue,
	readReportDateRange,
	ReportDateRangePicker,
	type ReportDateRange,
} from "../components/ReportDateRangePicker";

const NOW = new Date(2026, 8, 15, 12);

function PickerHarness({ initial }: { initial: ReportDateRange }) {
	const [value, setValue] = useState(initial);
	return (
		<>
			<ReportDateRangePicker value={value} onChange={setValue} now={NOW} />
			<output data-testid="range-value">
				{value.startDate}~{value.endDate}
			</output>
		</>
	);
}

describe("报告时间范围日期模型", () => {
	it("computes calendar month ends without hardcoded month lengths", () => {
		expect(monthRange(2025, 2)).toEqual({
			startDate: "2025-02-01",
			endDate: "2025-02-28",
		});
		expect(monthRange(2024, 2)).toEqual({
			startDate: "2024-02-01",
			endDate: "2024-02-29",
		});
		expect(monthRange(2026, 4).endDate).toBe("2026-04-30");
		expect(monthRange(2026, 1).endDate).toBe("2026-01-31");
	});

	it("supports shortcuts across year boundaries", () => {
		const january = new Date(2026, 0, 15, 12);
		expect(getReportDateRange("this_month", january)).toEqual({
			startDate: "2026-01-01",
			endDate: "2026-01-31",
		});
		expect(getReportDateRange("last_month", january)).toEqual({
			startDate: "2025-12-01",
			endDate: "2025-12-31",
		});
		expect(getReportDateRange("recent_3_months", NOW)).toEqual({
			startDate: "2026-07-01",
			endDate: "2026-09-30",
		});
		expect(getReportDateRange("recent_6_months", NOW)).toEqual({
			startDate: "2026-04-01",
			endDate: "2026-09-30",
		});
		expect(getReportDateRange("this_year", NOW)).toEqual({
			startDate: "2026-01-01",
			endDate: "2026-12-31",
		});
		expect(getReportDateRange("last_year", NOW)).toEqual({
			startDate: "2025-01-01",
			endDate: "2025-12-31",
		});
	});

	it("validates leap days and ordered custom ranges", () => {
		expect(parseDateValue("2024-02-29")).not.toBeNull();
		expect(parseDateValue("2025-02-29")).toBeNull();
		expect(
			isValidDateRange({
				startDate: "2025-10-15",
				endDate: "2026-03-20",
			}),
		).toBe(true);
		expect(
			isValidDateRange({
				startDate: "2026-03-20",
				endDate: "2025-10-15",
			}),
		).toBe(false);
		expect(
			formatReportRangeLabel(
				{ startDate: "2025-10-15", endDate: "2026-03-20" },
				NOW,
			),
		).toBe("2025/10/15 – 2026/03/20");
	});

	it("falls back safely for incomplete or invalid URL ranges", () => {
		const invalid = readReportDateRange(
			new URLSearchParams("from=2026-09-01&to=2026-08-31"),
			NOW,
		);
		expect(invalid.hasUrlRange).toBe(true);
		expect(invalid.validFromUrl).toBe(false);
		expect(invalid.range).toEqual(getReportDateRange("this_month", NOW));
		const missingEnd = readReportDateRange(
			new URLSearchParams("from=2026-09-01"),
			NOW,
		);
		expect(missingEnd.validFromUrl).toBe(false);
	});
});

describe("ReportDateRangePicker", () => {
	beforeEach(() => {
		document.body.innerHTML = "";
	});

	it("switches years and selects an arbitrary historical month", () => {
		render(<PickerHarness initial={monthRange(2026, 9)} />);
		fireEvent.click(screen.getByRole("button", { name: /选择报告时间范围/ }));
		fireEvent.click(screen.getByRole("button", { name: "上一年" }));
		fireEvent.click(screen.getByRole("button", { name: "2025年3月" }));
		expect(screen.getByTestId("range-value")).toHaveTextContent(
			"2025-03-01~2025-03-31",
		);
	});

	it("applies shortcut ranges and preserves a clear selected state", () => {
		render(<PickerHarness initial={monthRange(2026, 9)} />);
		fireEvent.click(screen.getByRole("button", { name: /选择报告时间范围/ }));
		const shortcut = screen.getByRole("button", { name: "近3个月" });
		fireEvent.click(shortcut);
		expect(screen.getByTestId("range-value")).toHaveTextContent(
			"2026-07-01~2026-09-30",
		);
	});

	it("rejects start dates after end dates and applies valid custom ranges", () => {
		render(<PickerHarness initial={monthRange(2026, 9)} />);
		fireEvent.click(screen.getByRole("button", { name: /选择报告时间范围/ }));
		fireEvent.click(screen.getByRole("tab", { name: "自定义" }));
		fireEvent.change(screen.getByLabelText("开始日期"), {
			target: { value: "2025-10-15" },
		});
		fireEvent.change(screen.getByLabelText("结束日期"), {
			target: { value: "2025-10-14" },
		});
		fireEvent.click(screen.getByRole("button", { name: "应用" }));
		expect(screen.getByRole("alert")).toHaveTextContent("开始日期不能晚于结束日期");
		expect(screen.getByTestId("range-value")).toHaveTextContent(
			"2026-09-01~2026-09-30",
		);

		fireEvent.change(screen.getByLabelText("结束日期"), {
			target: { value: "2026-03-20" },
		});
		fireEvent.click(screen.getByRole("button", { name: "应用" }));
		expect(screen.getByTestId("range-value")).toHaveTextContent(
			"2025-10-15~2026-03-20",
		);
	});

	it("closes on Escape and exposes keyboard-friendly dialog semantics", () => {
		render(<PickerHarness initial={monthRange(2026, 9)} />);
		const trigger = screen.getByRole("button", { name: /选择报告时间范围/ });
		fireEvent.click(trigger);
		expect(screen.getByRole("dialog", { name: "报告时间范围" })).toBeInTheDocument();
		fireEvent.keyDown(document, { key: "Escape" });
		expect(screen.queryByRole("dialog", { name: "报告时间范围" })).not.toBeInTheDocument();
		expect(trigger).toHaveFocus();
	});
});
