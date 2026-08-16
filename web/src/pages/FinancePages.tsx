import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
	ChevronLeft,
	ChevronRight,
	Download,
	FileBarChart,
	Pencil,
	PiggyBank,
	Plus,
	Trash2,
} from "lucide-react";
import { Link } from "react-router-dom";
import {
	api,
	downloadExport,
	money,
	type AnalyticsMonthlyPoint,
	type AnalyticsOverview,
	type BudgetItem,
	type BudgetOverview,
	type BudgetStatus,
	type ReportData,
} from "../api";
import { EmptyState, PageSkeleton } from "../components/States";

const periodOptions = [
	{ value: "7d", label: "7 天" },
	{ value: "30d", label: "30 天" },
	{ value: "90d", label: "90 天" },
	{ value: "year", label: "本年" },
	{ value: "custom", label: "自定义" },
] as const;

export function AnalyticsPage() {
	const [period, setPeriod] = useState("30d");
	const defaultDates = useMemo(() => presetDates("90d"), []);
	const [customStart, setCustomStart] = useState(defaultDates.start);
	const [customEnd, setCustomEnd] = useState(defaultDates.end);
	const query =
		period === "custom"
			? `period=custom&start_date=${customStart}&end_date=${customEnd}`
			: `period=${period}`;
	const overview = useQuery({
		queryKey: ["analytics", query],
		queryFn: () => api<AnalyticsOverview>(`/analytics?${query}`),
	});
	const monthly = useQuery({
		queryKey: ["analytics-monthly"],
		queryFn: () =>
			api<AnalyticsMonthlyPoint[]>("/analytics/monthly?period=year"),
	});
	const loading = overview.isLoading || monthly.isLoading;
	const failed = overview.isError || monthly.isError;
	const maximum = Math.max(
		1,
		...(overview.data?.trend ?? []).flatMap((item) => [
			Number(item.income),
			Number(item.expense),
		]),
	);
	if (loading) return <PageSkeleton rows={3} />;
	if (failed || !overview.data)
		return (
			<div className="state-panel">
				<h3>分析数据加载失败</h3>
				<button
					onClick={() => {
						overview.refetch();
						monthly.refetch();
					}}
				>
					重试
				</button>
			</div>
		);
	const { summary, trend, categories } = overview.data;
	return (
		<section>
			<div className="page-heading">
				<div>
					<p className="eyebrow">FINANCIAL ANALYTICS</p>
					<h2>看懂钱花去了哪里。</h2>
				</div>
				<div className="range-switch">
					{periodOptions.map((item) => (
						<button
							className={period === item.value ? "active" : ""}
							key={item.value}
							onClick={() => setPeriod(item.value)}
						>
							{item.label}
						</button>
					))}
				</div>
			</div>
			{period === "custom" && (
				<div className="custom-range">
					<label>
						开始
						<input
							type="date"
							value={customStart}
							onChange={(event) => setCustomStart(event.target.value)}
						/>
					</label>
					<label>
						结束
						<input
							type="date"
							value={customEnd}
							onChange={(event) => setCustomEnd(event.target.value)}
						/>
					</label>
				</div>
			)}
			<div className="metric-grid">
				<article>
					<span>收入</span>
					<strong className="positive">{money(summary.income)}</strong>
				</article>
				<article>
					<span>支出</span>
					<strong>{money(summary.expense)}</strong>
				</article>
				<article>
					<span>净额</span>
					<strong className={Number(summary.balance) >= 0 ? "positive" : ""}>
						{money(summary.balance)}
					</strong>
				</article>
			</div>
			{summary.entry_count === 0 ? (
				<EmptyState
					icon={<FileBarChart size={30} />}
					title="这个时间范围还没有账目"
					description="在这里创建第一笔记录，或直接在飞书里说。"
				/>
			) : (
				<>
					<div className="dashboard-grid">
						<section className="panel">
							<div className="panel-title">
								<h3>每日收支趋势</h3>
								<span>{summary.entry_count} 笔</span>
							</div>
							<div className="analytics-chart">
								{trend.map((item) => (
									<div
										className="analytics-day"
										key={item.period}
										title={`${item.period} 收入 ${money(item.income)} 支出 ${money(item.expense)}`}
									>
										<i
											className="income"
											style={{
												height: `${Math.max(2, (Number(item.income) / maximum) * 100)}%`,
											}}
										/>
										<i
											className="expense"
											style={{
												height: `${Math.max(2, (Number(item.expense) / maximum) * 100)}%`,
											}}
										/>
									</div>
								))}
							</div>
							<div className="chart-legend">
								<span className="income">收入</span>
								<span className="expense">支出</span>
							</div>
						</section>
						<section className="panel">
							<div className="panel-title">
								<h3>支出分类</h3>
								<span>点击查看账目</span>
							</div>
							<div className="category-list">
								{categories.map((item) => (
									<Link
										key={item.category}
										to={`/entries?category=${encodeURIComponent(item.category)}`}
									>
										<b>{item.category}</b>
										<span>{money(item.amount)}</span>
										<em>{Number(item.ratio).toFixed(1)}%</em>
									</Link>
								))}
							</div>
						</section>
					</div>
					<section className="panel">
						<div className="panel-title">
							<h3>月度趋势</h3>
							<span>本年</span>
						</div>
						<div className="monthly-grid">
							{monthly.data?.map((item) => (
								<article key={item.period}>
									<b>{item.period}</b>
									<span>收入 {money(item.income)}</span>
									<span>支出 {money(item.expense)}</span>
									<strong
										className={Number(item.balance) >= 0 ? "positive" : ""}
									>
										{money(item.balance)}
									</strong>
								</article>
							))}
						</div>
					</section>
				</>
			)}
		</section>
	);
}

function currentMonth(): string {
	const now = new Date();
	return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}`;
}

function shiftMonth(period: string, delta: number): string {
	const [year, month] = period.split("-").map(Number);
	const shifted = new Date(year, month - 1 + delta, 1);
	return `${shifted.getFullYear()}-${String(shifted.getMonth() + 1).padStart(2, "0")}`;
}

function budgetStatusMeta(status: BudgetStatus | undefined): {
	label: string;
	tone: string;
} {
	switch (status) {
		case "warning":
			return { label: "预警", tone: "warning" };
		case "exceeded":
			return { label: "超支", tone: "exceeded" };
		case "none":
			return { label: "未设置", tone: "none" };
		default:
			return { label: "正常", tone: "normal" };
	}
}

export function BudgetsPage() {
	const client = useQueryClient();
	const [period, setPeriod] = useState(currentMonth());
	const [editing, setEditing] = useState<{
		category: string;
		amount: string;
	} | null>(null);
	const [notice, setNotice] = useState("");
	const query = useQuery({
		queryKey: ["budgets", period],
		queryFn: () => api<BudgetOverview>(`/budgets?period=${period}`),
	});
	const setCategory = useMutation({
		mutationFn: ({ category, amount }: { category: string; amount: string }) =>
			api<BudgetOverview>(
				`/budgets/${encodeURIComponent(category)}?period=${period}`,
				{ method: "PUT", body: JSON.stringify({ amount }) },
			),
		onSuccess: (data) => {
			client.setQueryData(["budgets", period], data);
			setEditing(null);
			setNotice("预算已更新");
		},
	});
	const remove = useMutation({
		mutationFn: (category: string) =>
			api<BudgetOverview>(
				`/budgets/${encodeURIComponent(category)}?period=${period}`,
				{ method: "DELETE" },
			),
		onSuccess: (data) => {
			client.setQueryData(["budgets", period], data);
			setNotice("预算已删除");
		},
	});
	const setTotal = useMutation({
		mutationFn: (amount: string) =>
			api<BudgetOverview>(`/budgets/total?period=${period}`, {
				method: "PUT",
				body: JSON.stringify({ amount }),
			}),
		onSuccess: (data) => {
			client.setQueryData(["budgets", period], data);
			setEditing(null);
			setNotice("总预算已更新");
		},
	});
	const removeTotal = useMutation({
		mutationFn: () =>
			api<BudgetOverview>(`/budgets/total?period=${period}`, {
				method: "DELETE",
			}),
		onSuccess: (data) => {
			client.setQueryData(["budgets", period], data);
			setNotice("总预算已删除");
		},
	});
	if (query.isLoading) return <PageSkeleton rows={2} />;
	if (query.isError || !query.data)
		return (
			<div className="state-panel">
				<h3>预算加载失败</h3>
				<button onClick={() => query.refetch()}>重试</button>
			</div>
		);
	const data = query.data;
	const heroStatus = budgetStatusMeta(data.status);
	const unsetSpent = data.items
		.filter((item) => item.amount === null)
		.reduce((sum, item) => sum + Number(item.spent), 0);
	const canEditTotal = data.total_limit_set || data.total_budget !== null;
	return (
		<section>
			{notice && <div className="toast">{notice}</div>}
			<div className="page-heading">
				<div>
					<p className="eyebrow">MONTHLY BUDGET</p>
					<h2>预算留有余地，生活更从容。</h2>
				</div>
				<div className="month-nav">
					<button
						aria-label="上个月"
						onClick={() => setPeriod(shiftMonth(period, -1))}
					>
						<ChevronLeft size={17} />
					</button>
					<strong>{data.period ?? period}</strong>
					<button
						aria-label="下个月"
						onClick={() => setPeriod(shiftMonth(period, 1))}
					>
						<ChevronRight size={17} />
					</button>
				</div>
			</div>
			{data.total_budget !== null ? (
				<section className="budget-hero">
					<PiggyBank size={26} />
					<div>
						<span>
							总预算{" "}
							<i className={`status-chip ${heroStatus.tone}`}>
								{heroStatus.label}
							</i>
						</span>
						<strong>{money(data.total_budget)}</strong>
						<div className="budget-track">
							<i
								className={Number(data.usage_rate ?? 0) >= 100 ? "over" : ""}
								style={{
									width: `${Math.min(100, Number(data.usage_rate ?? 0))}%`,
								}}
							/>
						</div>
						<small>
							已支出 {money(data.total_spent)} ·{" "}
							{Number(data.total_remaining ?? 0) < 0
								? `超出 ${money(-Number(data.total_remaining ?? 0))}`
								: `剩余 ${money(data.total_remaining ?? 0)}`}{" "}
							· {Number(data.usage_rate ?? 0).toFixed(1)}%
						</small>
					</div>
					{canEditTotal && (
						<div className="hero-actions">
							<button
								aria-label="修改总预算"
								onClick={() =>
									setEditing({
										category: "总预算",
										amount: String(data.total_budget ?? ""),
									})
								}
							>
								<Pencil size={15} />
							</button>
							<button
								className="danger"
								aria-label="删除总预算"
								onClick={() => removeTotal.mutate()}
							>
								<Trash2 size={15} />
							</button>
						</div>
					)}
				</section>
			) : (
				<section className="budget-hero budget-hero-empty">
					<PiggyBank size={26} />
					<div>
						<span>总预算</span>
						<strong>{data.allocated ? money(data.allocated) : "未设置"}</strong>
						<small>
							{data.allocated
								? `已分配分类预算 ${money(data.allocated)}${data.unallocated !== null ? ` · 未分配 ${money(data.unallocated)}` : ""} · 已支出 ${money(data.total_spent)}`
								: `本月已支出 ${money(data.total_spent)}${unsetSpent ? ` · 未设预算分类 ${money(unsetSpent)}` : ""}`}
						</small>
					</div>
					<button
						className="primary-small hero-set"
						onClick={() => setEditing({ category: "总预算", amount: "" })}
					>
						<Plus size={15} /> 设置总预算
					</button>
				</section>
			)}
			{data.items.length === 0 ? (
				<EmptyState
					icon={<PiggyBank size={30} />}
					title="本月还没有设置预算"
					description="先设置一个总预算，或在下方为分类设置预算。"
					action={
						<button
							className="primary-small"
							onClick={() => setEditing({ category: "", amount: "" })}
						>
							<Plus size={15} /> 设置分类预算
						</button>
					}
				/>
			) : (
				<div className="budget-list">
					{data.items.map((item) => (
						<BudgetRow
							key={item.category}
							item={item}
							onEdit={(category) =>
								setEditing({ category, amount: String(item.amount ?? "") })
							}
							onDelete={(category) => remove.mutate(category)}
						/>
					))}
				</div>
			)}
			{editing && (
				<form
					className="modal-layer"
					onSubmit={(event) => {
						event.preventDefault();
						if (!editing.category) return;
						const amount = editing.amount;
						if (editing.category === "总预算") setTotal.mutate(amount);
						else setCategory.mutate({ category: editing.category, amount });
					}}
				>
					<div className="edit-dialog">
						<h3>
							{editing.category === "总预算"
								? "设置本月总预算"
								: `设置 ${editing.category} 预算`}
						</h3>
						{editing.category === "" ? (
							<label>
								分类名称
								<input
									type="text"
									maxLength={64}
									value={editing.category}
									onChange={(event) =>
										setEditing({ ...editing, category: event.target.value })
									}
								/>
							</label>
						) : null}
						{editing.category !== "总预算" && (
							<label>
								每月预算
								<input
									type="number"
									min="0.01"
									step="0.01"
									value={editing.amount}
									onChange={(event) =>
										setEditing({ ...editing, amount: event.target.value })
									}
								/>
							</label>
						)}
						{editing.category === "总预算" && (
							<label>
								每月总预算
								<input
									type="number"
									min="0.01"
									step="0.01"
									value={editing.amount}
									onChange={(event) =>
										setEditing({ ...editing, amount: event.target.value })
									}
								/>
							</label>
						)}
						{(setCategory.error || setTotal.error) && (
							<p className="form-error">
								{setCategory.error?.message ?? setTotal.error?.message}
							</p>
						)}
						<div>
							<button type="button" onClick={() => setEditing(null)}>
								取消
							</button>
							<button
								className="primary-small"
								disabled={
									setCategory.isPending ||
									setTotal.isPending ||
									!editing.category.trim() ||
									Number(editing.amount) <= 0
								}
							>
								保存
							</button>
						</div>
					</div>
				</form>
			)}
		</section>
	);
}

function BudgetRow({
	item,
	onEdit,
	onDelete,
}: {
	item: BudgetItem;
	onEdit: (category: string) => void;
	onDelete: (category: string) => void;
}) {
	const meta = budgetStatusMeta(item.status);
	if (item.amount === null) {
		return (
			<article className="budget-unset">
				<div>
					<strong>{item.category}</strong>
					<span>未设置预算 · 本月已消费 {money(item.spent)}</span>
				</div>
				<footer>
					<span className={`status-chip ${meta.tone}`}>{meta.label}</span>
					<button
						className="primary-small"
						aria-label={`设置 ${item.category} 预算`}
						onClick={() => onEdit(item.category)}
					>
						<Plus size={14} /> 设置
					</button>
				</footer>
			</article>
		);
	}
	return (
		<article>
			<div>
				<strong>{item.category}</strong>
				<span>
					{money(item.spent)} / {money(item.amount)}
				</span>
			</div>
			<div className="budget-track">
				<i
					className={Number(item.usage_rate ?? 0) >= 100 ? "over" : ""}
					style={{ width: `${Math.min(100, Number(item.usage_rate ?? 0))}%` }}
				/>
			</div>
			<footer>
				<span>
					<i className={`status-chip ${meta.tone}`}>{meta.label}</i>{" "}
					{Number(item.usage_rate ?? 0) >= 100
						? `超出 ${money(-Number(item.remaining ?? 0))}`
						: `剩余 ${money(item.remaining ?? 0)}`}{" "}
					· {Number(item.usage_rate ?? 0).toFixed(1)}%
				</span>
				<button
					aria-label={`修改 ${item.category}`}
					onClick={() => onEdit(item.category)}
				>
					<Pencil size={15} />
				</button>
				<button
					className="danger"
					aria-label={`删除 ${item.category}`}
					onClick={() => onDelete(item.category)}
				>
					<Trash2 size={15} />
				</button>
			</footer>
		</article>
	);
}

function presetDates(preset: string) {
	const now = new Date();
	const end = new Date(now.getFullYear(), now.getMonth(), now.getDate());
	const start = new Date(end);
	if (preset === "month") start.setDate(1);
	else if (preset === "last_month") {
		start.setMonth(start.getMonth() - 1, 1);
		end.setDate(0);
	} else start.setDate(start.getDate() - 89);
	const format = (value: Date) =>
		`${value.getFullYear()}-${String(value.getMonth() + 1).padStart(2, "0")}-${String(value.getDate()).padStart(2, "0")}`;
	return { start: format(start), end: format(end) };
}

export function ReportsPage() {
	const [preset, setPreset] = useState("month");
	const initialCustom = useMemo(() => presetDates("90d"), []);
	const [customStart, setCustomStart] = useState(initialCustom.start);
	const [customEnd, setCustomEnd] = useState(initialCustom.end);
	const dates = useMemo(
		() =>
			preset === "custom"
				? { start: customStart, end: customEnd }
				: presetDates(preset),
		[preset, customStart, customEnd],
	);
	const query = useQuery({
		queryKey: ["report", dates.start, dates.end],
		queryFn: () =>
			api<ReportData>(
				`/reports?start_date=${dates.start}&end_date=${dates.end}`,
			),
		retry: false,
	});
	const max = Math.max(
		1,
		...(query.data?.trend.map((item) => Number(item.amount)) ?? []),
	);
	return (
		<section>
			<div className="page-heading">
				<div>
					<p className="eyebrow">FINANCIAL REPORT</p>
					<h2>收支报告</h2>
				</div>
				<select
					className="preset-select"
					value={preset}
					onChange={(event) => setPreset(event.target.value)}
				>
					<option value="month">本月</option>
					<option value="last_month">上月</option>
					<option value="90d">最近 90 天</option>
					<option value="custom">自定义</option>
				</select>
			</div>
			{preset === "custom" && (
				<div className="custom-range">
					<label>
						开始
						<input
							type="date"
							value={customStart}
							onChange={(event) => setCustomStart(event.target.value)}
						/>
					</label>
					<label>
						结束
						<input
							type="date"
							value={customEnd}
							onChange={(event) => setCustomEnd(event.target.value)}
						/>
					</label>
				</div>
			)}
			{query.isLoading ? (
				<PageSkeleton rows={2} />
			) : query.isError ? (
				<div className="state-panel">
					<h3>报告加载失败</h3>
					<button onClick={() => query.refetch()}>重试</button>
				</div>
			) : !query.data ? (
				<EmptyState
					icon={<FileBarChart size={30} />}
					title="这个时间范围还没有账目"
					description="在这里创建第一笔记录，或直接在飞书里说。"
				/>
			) : (
				<>
					<div className="metric-grid">
						<article>
							<span>收入</span>
							<strong className="positive">
								{money(query.data.income_total)}
							</strong>
						</article>
						<article>
							<span>支出</span>
							<strong>{money(query.data.expense_total)}</strong>
						</article>
						<article>
							<span>结余</span>
							<strong>{money(query.data.balance)}</strong>
						</article>
					</div>
					<div className="dashboard-grid">
						<section className="panel">
							<div className="panel-title">
								<h3>支出趋势</h3>
								<span>
									{query.data.trend_granularity === "day" ? "每日" : "每月"}
								</span>
							</div>
							<div className="report-bars">
								{query.data.trend.map((item) => (
									<i
										key={item.period}
										style={{
											height: `${Math.max(2, (Number(item.amount) / max) * 100)}%`,
										}}
										title={`${item.period} ${money(item.amount)}`}
									/>
								))}
							</div>
						</section>
						<section className="panel">
							<div className="panel-title">
								<h3>主要分类</h3>
								<span>{query.data.entry_count} 笔</span>
							</div>
							<div className="category-list">
								{query.data.categories.map((item) => (
									<Link
										to={`/entries?category=${encodeURIComponent(item.category)}`}
										key={item.category}
									>
										<b>{item.category}</b>
										<span>{money(item.amount)}</span>
									</Link>
								))}
							</div>
						</section>
					</div>
				</>
			)}
		</section>
	);
}

export function ExportsPage() {
	const [preset, setPreset] = useState("last_90_days");
	const [includeDeleted, setIncludeDeleted] = useState(false);
	const [startDate, setStartDate] = useState("");
	const [endDate, setEndDate] = useState("");
	const [notice, setNotice] = useState("");
	const mutation = useMutation({
		mutationFn: () =>
			downloadExport({
				preset,
				include_deleted: includeDeleted,
				start_date: preset === "custom" ? startDate : null,
				end_date: preset === "custom" ? endDate : null,
			}),
		onSuccess: ({ blob, filename }) => {
			const url = URL.createObjectURL(blob);
			const anchor = document.createElement("a");
			anchor.href = url;
			anchor.download = filename;
			anchor.click();
			URL.revokeObjectURL(url);
			setNotice("CSV 已开始下载");
		},
	});
	return (
		<section>
			{notice && <div className="toast">{notice}</div>}
			<div className="page-heading">
				<div>
					<p className="eyebrow">CSV EXPORT</p>
					<h2>带走你的账本数据。</h2>
				</div>
			</div>
			<div className="export-card">
				<Download size={30} />
				<h3>导出 CSV</h3>
				<p>
					仅导出当前登录用户的账目。文件使用 UTF-8 BOM，并沿用 5000 行、5MB
					与公式注入防护限制。
				</p>
				<label>
					时间范围
					<select
						value={preset}
						onChange={(event) => setPreset(event.target.value)}
					>
						<option value="last_90_days">最近 90 天</option>
						<option value="this_month">本月</option>
						<option value="all">全部</option>
						<option value="custom">自定义</option>
					</select>
				</label>
				{preset === "custom" && (
					<div className="export-dates">
						<label>
							开始日期
							<input
								type="date"
								value={startDate}
								onChange={(event) => setStartDate(event.target.value)}
							/>
						</label>
						<label>
							结束日期
							<input
								type="date"
								value={endDate}
								onChange={(event) => setEndDate(event.target.value)}
							/>
						</label>
					</div>
				)}
				<label className="check-label">
					<input
						type="checkbox"
						checked={includeDeleted}
						onChange={(event) => setIncludeDeleted(event.target.checked)}
					/>{" "}
					包含已删除账目
				</label>
				{mutation.error && (
					<p className="form-error">{mutation.error.message}</p>
				)}
				<button
					className="primary-small export-button"
					disabled={
						mutation.isPending ||
						(preset === "custom" && (!startDate || !endDate))
					}
					onClick={() => mutation.mutate()}
				>
					<Download size={16} />{" "}
					{mutation.isPending ? "正在生成…" : "生成并下载"}
				</button>
			</div>
		</section>
	);
}
