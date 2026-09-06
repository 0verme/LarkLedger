import { useEffect, useRef, useState } from "react";
import {
	CalendarDays,
	ChevronDown,
	ChevronLeft,
	ChevronRight,
} from "lucide-react";

export type ReportDateRange = {
	startDate: string;
	endDate: string;
};

export const REPORT_RANGE_PRESETS = [
	{ value: "this_month", label: "本月" },
	{ value: "last_month", label: "上月" },
	{ value: "recent_3_months", label: "近3个月" },
	{ value: "recent_6_months", label: "近6个月" },
	{ value: "this_year", label: "今年" },
	{ value: "last_year", label: "去年" },
] as const;

export type ReportRangePreset = (typeof REPORT_RANGE_PRESETS)[number]["value"];

type PickerMode = "month" | "custom";

type ParsedReportDateRange = {
	range: ReportDateRange;
	hasUrlRange: boolean;
	validFromUrl: boolean;
};

const MONTH_LABELS = Array.from({ length: 12 }, (_, index) => `${index + 1}月`);
const ISO_DATE_PATTERN = /^(\d{4})-(\d{2})-(\d{2})$/;

function localDate(year: number, monthIndex: number, day: number): Date {
	const result = new Date(0);
	result.setHours(0, 0, 0, 0);
	result.setFullYear(year, monthIndex, day);
	return result;
}

export function formatDateValue(value: Date): string {
	return `${value.getFullYear()}-${String(value.getMonth() + 1).padStart(2, "0")}-${String(value.getDate()).padStart(2, "0")}`;
}

export function parseDateValue(value: string): Date | null {
	const match = ISO_DATE_PATTERN.exec(value);
	if (!match) return null;
	const [, yearText, monthText, dayText] = match;
	const year = Number(yearText);
	const month = Number(monthText);
	const day = Number(dayText);
	if (month < 1 || month > 12 || day < 1 || day > 31) return null;
	const result = localDate(year, month - 1, day);
	return formatDateValue(result) === value ? result : null;
}

export function monthRange(year: number, month: number): ReportDateRange {
	const start = localDate(year, month - 1, 1);
	const end = localDate(year, month, 0);
	return { startDate: formatDateValue(start), endDate: formatDateValue(end) };
}

export function yearRange(year: number): ReportDateRange {
	return {
		startDate: formatDateValue(localDate(year, 0, 1)),
		endDate: formatDateValue(localDate(year, 11, 31)),
	};
}

function recentMonthRange(months: number, now: Date): ReportDateRange {
	const start = localDate(now.getFullYear(), now.getMonth() - months + 1, 1);
	const end = localDate(now.getFullYear(), now.getMonth() + 1, 0);
	return { startDate: formatDateValue(start), endDate: formatDateValue(end) };
}

export function getReportDateRange(
	preset: ReportRangePreset,
	now = new Date(),
): ReportDateRange {
	switch (preset) {
		case "this_month":
			return monthRange(now.getFullYear(), now.getMonth() + 1);
		case "last_month":
			return monthRange(now.getFullYear(), now.getMonth());
		case "recent_3_months":
			return recentMonthRange(3, now);
		case "recent_6_months":
			return recentMonthRange(6, now);
		case "this_year":
			return yearRange(now.getFullYear());
		case "last_year":
			return yearRange(now.getFullYear() - 1);
	}
}

export function isValidDateRange(range: ReportDateRange): boolean {
	return (
		parseDateValue(range.startDate) !== null &&
		parseDateValue(range.endDate) !== null &&
		range.startDate <= range.endDate
	);
}

export function readReportDateRange(
	params: URLSearchParams,
	now = new Date(),
): ParsedReportDateRange {
	const start = params.get("from") ?? params.get("start_date");
	const end = params.get("to") ?? params.get("end_date");
	const hasUrlRange = start !== null || end !== null;
	const candidate =
		start !== null && end !== null
			? { startDate: start, endDate: end }
			: undefined;
	const validFromUrl = !hasUrlRange || (candidate !== undefined && isValidDateRange(candidate));
	return {
		range: validFromUrl && candidate ? candidate : getReportDateRange("this_month", now),
		hasUrlRange,
		validFromUrl,
	};
}

function sameRange(left: ReportDateRange, right: ReportDateRange): boolean {
	return left.startDate === right.startDate && left.endDate === right.endDate;
}

function fullMonth(range: ReportDateRange): { year: number; month: number } | null {
	const start = parseDateValue(range.startDate);
	if (!start || !sameRange(range, monthRange(start.getFullYear(), start.getMonth() + 1))) {
		return null;
	}
	return { year: start.getFullYear(), month: start.getMonth() + 1 };
}

function fullYear(range: ReportDateRange): number | null {
	const start = parseDateValue(range.startDate);
	if (!start || !sameRange(range, yearRange(start.getFullYear()))) return null;
	return start.getFullYear();
}

export function formatReportRangeLabel(
	range: ReportDateRange,
	now = new Date(),
): string {
	const month = fullMonth(range);
	if (month) return `${month.year}年${month.month}月`;
	const year = fullYear(range);
	if (year) return `${year}年`;
	for (const preset of REPORT_RANGE_PRESETS) {
		if (sameRange(range, getReportDateRange(preset.value, now))) return preset.label;
	}
	return `${range.startDate.replaceAll("-", "/")} – ${range.endDate.replaceAll("-", "/")}`;
}

function selectedMonth(range: ReportDateRange): { year: number; month: number } | null {
	return fullMonth(range);
}

export function ReportDateRangePicker({
	value,
	onChange,
	now = new Date(),
	disabled = false,
}: {
	value: ReportDateRange;
	onChange: (range: ReportDateRange) => void;
	now?: Date;
	disabled?: boolean;
}) {
	const [open, setOpen] = useState(false);
	const [mode, setMode] = useState<PickerMode>("month");
	const [viewYear, setViewYear] = useState(now.getFullYear());
	const [draftStart, setDraftStart] = useState(value.startDate);
	const [draftEnd, setDraftEnd] = useState(value.endDate);
	const [error, setError] = useState("");
	const pickerRef = useRef<HTMLDivElement>(null);
	const triggerRef = useRef<HTMLButtonElement>(null);

	const closePicker = () => {
		setOpen(false);
		setError("");
	};

	const openPicker = () => {
		const selected = selectedMonth(value);
		const start = parseDateValue(value.startDate);
		setDraftStart(value.startDate);
		setDraftEnd(value.endDate);
		setMode("month");
		setViewYear(selected?.year ?? start?.getFullYear() ?? now.getFullYear());
		setError("");
		setOpen(true);
	};

	useEffect(() => {
		if (!open) return;
		const closeOnEscape = (event: KeyboardEvent) => {
			if (event.key !== "Escape") return;
			event.preventDefault();
			closePicker();
			triggerRef.current?.focus();
		};
		const closeOnOutsidePointer = (event: PointerEvent) => {
			if (event.target instanceof Node && !pickerRef.current?.contains(event.target)) {
				closePicker();
			}
		};
		document.addEventListener("keydown", closeOnEscape);
		document.addEventListener("pointerdown", closeOnOutsidePointer);
		return () => {
			document.removeEventListener("keydown", closeOnEscape);
			document.removeEventListener("pointerdown", closeOnOutsidePointer);
		};
	}, [open]);

	const applyRange = (range: ReportDateRange) => {
		onChange(range);
		closePicker();
		triggerRef.current?.focus();
	};

	const applyCustomRange = () => {
		if (!draftStart || !draftEnd) {
			setError("请选择开始日期和结束日期");
			return;
		}
		if (!isValidDateRange({ startDate: draftStart, endDate: draftEnd })) {
			setError("开始日期不能晚于结束日期");
			return;
		}
		applyRange({ startDate: draftStart, endDate: draftEnd });
	};

	const selected = selectedMonth(value);
	const currentYear = now.getFullYear();
	const currentMonth = now.getMonth() + 1;
	const label = formatReportRangeLabel(value, now);

	return (
		<div className="report-range-picker" ref={pickerRef}>
			<button
				ref={triggerRef}
				type="button"
				className="report-range-trigger"
				aria-haspopup="dialog"
				aria-expanded={open}
				aria-controls="report-range-panel"
				aria-label={`选择报告时间范围，当前为${label}`}
				disabled={disabled}
				onClick={() => (open ? closePicker() : openPicker())}
			>
				<CalendarDays size={16} aria-hidden="true" />
				<span>{label}</span>
				<ChevronDown size={15} aria-hidden="true" />
			</button>
			{open && (
				<div
					id="report-range-panel"
					className="report-range-popover"
					role="dialog"
					aria-label="报告时间范围"
				>
					<div className="report-range-tabs" role="tablist" aria-label="时间范围模式">
						<button
							type="button"
							role="tab"
							aria-selected={mode === "month"}
							className={mode === "month" ? "active" : ""}
							onClick={() => setMode("month")}
						>
							按月选择
						</button>
						<button
							type="button"
							role="tab"
							aria-selected={mode === "custom"}
							className={mode === "custom" ? "active" : ""}
							onClick={() => setMode("custom")}
						>
							自定义
						</button>
					</div>
					{mode === "month" ? (
						<>
							<div className="report-range-shortcuts" aria-label="快捷时间范围">
								{REPORT_RANGE_PRESETS.map((preset) => (
									<button
										type="button"
										key={preset.value}
										className={
											sameRange(value, getReportDateRange(preset.value, now))
												? "active"
												: ""
										}
										aria-pressed={sameRange(value, getReportDateRange(preset.value, now))}
										onClick={() => applyRange(getReportDateRange(preset.value, now))}
									>
										{preset.label}
									</button>
								))}
							</div>
							<div className="report-range-year-header">
								<button
									type="button"
									className="report-range-icon-button"
									aria-label="上一年"
									onClick={() => setViewYear((year) => Math.max(1, year - 1))}
								>
									<ChevronLeft size={17} aria-hidden="true" />
								</button>
								<label>
									<span>年份</span>
									<input
										type="number"
										min="1"
										max="9999"
										value={viewYear}
										aria-label="选择年份"
										onChange={(event) => {
											const next = Number(event.target.value);
											if (Number.isInteger(next) && next >= 1 && next <= 9999) {
												setViewYear(next);
											}
										}}
									/>
								</label>
								<button
									type="button"
									className="report-range-icon-button"
									aria-label="下一年"
									onClick={() => setViewYear((year) => Math.min(9999, year + 1))}
								>
									<ChevronRight size={17} aria-hidden="true" />
								</button>
							</div>
							<div className="report-range-months" aria-label={`${viewYear}年月份`}>
								{MONTH_LABELS.map((monthLabel, index) => {
									const month = index + 1;
									const isSelected =
										selected?.year === viewYear && selected.month === month;
									const isCurrent = currentYear === viewYear && currentMonth === month;
									return (
										<button
											type="button"
											key={monthLabel}
											className={`report-month-option${isSelected ? " selected" : ""}`}
											data-current={isCurrent}
											aria-pressed={isSelected}
											aria-label={`${viewYear}年${monthLabel}`}
											onClick={() => applyRange(monthRange(viewYear, month))}
										>
											{monthLabel}
										</button>
									);
								})}
							</div>
						</>
					) : (
						<div className="report-range-custom">
							<label>
								开始日期
								<input
									type="date"
									value={draftStart}
									max={draftEnd || undefined}
									aria-invalid={Boolean(error)}
									onChange={(event) => {
										setDraftStart(event.target.value);
										setError("");
									}}
								/>
							</label>
							<label>
								结束日期
								<input
									type="date"
									value={draftEnd}
									min={draftStart || undefined}
									aria-invalid={Boolean(error)}
									onChange={(event) => {
										setDraftEnd(event.target.value);
										setError("");
									}}
								/>
							</label>
							{error && <p className="report-range-error" role="alert">{error}</p>}
							<div className="report-range-footer">
								<button type="button" onClick={closePicker}>取消</button>
								<button type="button" className="primary-small" onClick={applyCustomRange}>
									应用
								</button>
							</div>
						</div>
					)}
				</div>
			)}
		</div>
	);
}
