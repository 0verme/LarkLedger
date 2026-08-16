import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
	CalendarClock,
	Pause,
	Pencil,
	Play,
	Plus,
	RotateCcw,
	SkipForward,
} from "lucide-react";
import {
	api,
	money,
	type Account,
	type AccountList,
	type RecurringFrequency,
	type RecurringRule,
	type RecurringRuleCreateInput,
	type RecurringRuleList,
} from "../api";
import { EmptyState, PageSkeleton } from "../components/States";

const frequencyLabels: Record<RecurringFrequency, string> = {
	weekly: "每周",
	monthly: "每月",
	yearly: "每年",
};

function today(): string {
	const now = new Date();
	return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-${String(now.getDate()).padStart(2, "0")}`;
}

function statusMeta(status: RecurringRule["status"]): {
	label: string;
	tone: string;
} {
	switch (status) {
		case "paused":
			return { label: "已暂停", tone: "warning" };
		case "disabled":
			return { label: "已停用", tone: "none" };
		default:
			return { label: "启用", tone: "normal" };
	}
}

type RuleForm = {
	transaction_type: "expense" | "income";
	amount: string;
	currency: string;
	category: string;
	description: string;
	frequency: RecurringFrequency;
	next_occurrence: string;
	account_id: string;
};

export function RecurringRulesPage() {
	const client = useQueryClient();
	const [editing, setEditing] = useState<{
		ruleId: string | null;
		form: RuleForm;
	} | null>(null);
	const [notice, setNotice] = useState("");
	const rules = useQuery({
		queryKey: ["recurring-rules"],
		queryFn: () => api<RecurringRuleList>("/recurring-rules"),
	});
	const accounts = useQuery({
		queryKey: ["accounts"],
		queryFn: () => api<AccountList>("/accounts?include_archived=true"),
	});
	const refresh = async () => {
		await client.invalidateQueries({ queryKey: ["recurring-rules"] });
	};
	const action = useMutation({
		mutationFn: ({
			path,
			method,
			body,
		}: {
			path: string;
			method: string;
			body?: object;
		}) =>
			api<RecurringRule>(path, {
				method,
				body: body ? JSON.stringify(body) : undefined,
			}),
		onSuccess: async (_, vars) => {
			await refresh();
			if (vars.path.endsWith("/pause")) setNotice("周期账单已暂停");
			else if (vars.path.endsWith("/resume")) setNotice("周期账单已恢复");
			else if (vars.path.endsWith("/skip")) setNotice("已跳过本期");
			else if (vars.path.endsWith("/disable")) setNotice("周期账单已停用");
			else if (vars.method === "PATCH") setNotice("周期账单已更新");
			else setNotice("周期账单已创建");
			setEditing(null);
			window.setTimeout(() => setNotice(""), 2400);
		},
	});
	const save = (body: RecurringRuleCreateInput) => {
		if (editing?.ruleId) {
			action.mutate({
				path: `/recurring-rules/${editing.ruleId}`,
				method: "PATCH",
				body,
			});
		} else {
			action.mutate({ path: "/recurring-rules", method: "POST", body });
		}
	};
	if (rules.isLoading) return <PageSkeleton rows={2} />;
	if (rules.isError || !rules.data) {
		return (
			<div className="state-panel">
				<h3>周期账单加载失败</h3>
				<button onClick={() => rules.refetch()}>重试</button>
			</div>
		);
	}
	const items = rules.data.items;
	const accountsById = new Map(
		(accounts.data?.items ?? []).map((item) => [item.id, item]),
	);
	return (
		<section className="recurring-page">
			{notice && <div className="toast">{notice}</div>}
			<div className="page-heading">
				<div>
					<p className="eyebrow">RECURRING RULES</p>
					<h2>周期账单，到期先提醒，确认再入账。</h2>
				</div>
				<button
					className="primary-small"
					onClick={() =>
						setEditing({ ruleId: null, form: emptyForm(items, accountsById) })
					}
				>
					<Plus size={16} /> 创建周期账单
				</button>
			</div>
			{items.length === 0 ? (
				<EmptyState
					icon={<CalendarClock size={30} />}
					title="还没有周期账单"
					description="在飞书里说“每月8号房租3500”，或直接在这里创建。"
					action={
						<button
							className="primary-small"
							onClick={() =>
								setEditing({
									ruleId: null,
									form: emptyForm(items, accountsById),
								})
							}
						>
							<Plus size={15} /> 创建周期账单
						</button>
					}
				/>
			) : (
				<section className="table-panel">
					<div className="table-scroll">
						<table>
							<thead>
								<tr>
									<th>名称</th>
									<th>类型</th>
									<th>金额</th>
									<th>账户</th>
									<th>周期</th>
									<th>下次日期</th>
									<th>状态</th>
									<th>操作</th>
								</tr>
							</thead>
							<tbody>
								{items.map((rule) => {
									const meta = statusMeta(rule.status);
									const label = rule.description || rule.category;
									const sign =
										rule.transaction_type === "income" ? "收入" : "支出";
									return (
										<tr key={rule.id}>
											<td>
												<strong>{label}</strong>
												{rule.pending_count > 0 && (
													<span
														className="status-chip warning"
														style={{ marginLeft: 8 }}
													>
														待确认
													</span>
												)}
												<div className="table-sub">{rule.category}</div>
											</td>
											<td>{sign}</td>
											<td
												className={
													rule.transaction_type === "income" ? "positive" : ""
												}
											>
												{money(rule.amount, rule.currency)}
											</td>
											<td>{rule.account_name ?? "默认账户"}</td>
											<td>
												{frequencyLabels[rule.frequency]}
												{rule.interval > 1 ? `×${rule.interval}` : ""}
											</td>
											<td>{rule.next_occurrence}</td>
											<td>
												<span
													className={`status-dot ${meta.tone === "normal" ? "" : "deleted"}`}
												>
													{meta.label}
												</span>
											</td>
											<td>
												<div className="row-actions">
													<button
														aria-label={`修改 ${label}`}
														disabled={action.isPending}
														onClick={() =>
															setEditing({
																ruleId: rule.id,
																form: formFromRule(rule),
															})
														}
													>
														<Pencil size={15} /> 修改
													</button>
													{rule.status === "active" && (
														<>
															<button
																aria-label={`暂停 ${label}`}
																disabled={action.isPending}
																onClick={() =>
																	action.mutate({
																		path: `/recurring-rules/${rule.id}/pause`,
																		method: "POST",
																	})
																}
															>
																<Pause size={15} /> 暂停
															</button>
															<button
																aria-label={`跳过本期 ${label}`}
																disabled={action.isPending}
																onClick={() =>
																	action.mutate({
																		path: `/recurring-rules/${rule.id}/skip`,
																		method: "POST",
																	})
																}
															>
																<SkipForward size={15} /> 跳过
															</button>
														</>
													)}
													{rule.status === "paused" && (
														<button
															aria-label={`恢复 ${label}`}
															disabled={action.isPending}
															onClick={() =>
																action.mutate({
																	path: `/recurring-rules/${rule.id}/resume`,
																	method: "POST",
																})
															}
														>
															<Play size={15} /> 恢复
														</button>
													)}
													{rule.status !== "disabled" && (
														<button
															className="danger"
															aria-label={`停用 ${label}`}
															disabled={action.isPending}
															onClick={() =>
																action.mutate({
																	path: `/recurring-rules/${rule.id}/disable`,
																	method: "POST",
																})
															}
														>
															<RotateCcw size={15} /> 停用
														</button>
													)}
												</div>
											</td>
										</tr>
									);
								})}
							</tbody>
						</table>
					</div>
				</section>
			)}
			{editing && (
				<RuleDialog
					form={editing.form}
					accounts={accounts.data?.items ?? []}
					busy={action.isPending}
					error={action.error?.message}
					isEdit={editing.ruleId !== null}
					onClose={() => setEditing(null)}
					onSave={save}
				/>
			)}
		</section>
	);
}

function emptyForm(
	items: RecurringRule[],
	accounts: Map<string, Account>,
): RuleForm {
	const defaultAccount = [...accounts.values()].find((item) => item.is_default);
	return {
		transaction_type: "expense",
		amount: "",
		currency: "",
		category: "",
		description: "",
		frequency: "monthly",
		next_occurrence: today(),
		account_id: defaultAccount?.id ?? accounts.values().next().value?.id ?? "",
	};
}

function formFromRule(rule: RecurringRule): RuleForm {
	return {
		transaction_type: rule.transaction_type === "income" ? "income" : "expense",
		amount: rule.amount,
		currency: rule.currency === "CNY" ? "" : rule.currency,
		category: rule.category,
		description: rule.description,
		frequency: rule.frequency,
		next_occurrence: rule.next_occurrence,
		account_id: rule.account_id,
	};
}

function RuleDialog({
	form,
	accounts,
	busy,
	error,
	isEdit,
	onClose,
	onSave,
}: {
	form: RuleForm;
	accounts: Account[];
	busy: boolean;
	error?: string;
	isEdit: boolean;
	onClose: () => void;
	onSave: (body: RecurringRuleCreateInput) => void;
}) {
	const [value, setValue] = useState<RuleForm>(form);
	const valid =
		value.description.trim() &&
		Number(value.amount) > 0 &&
		value.category.trim() &&
		value.next_occurrence &&
		value.account_id;
	const submit = () => {
		onSave({
			transaction_type: value.transaction_type,
			amount: value.amount,
			currency: value.currency.trim()
				? value.currency.trim().toUpperCase()
				: null,
			category: value.category,
			description: value.description,
			frequency: value.frequency,
			interval: 1,
			next_occurrence: value.next_occurrence,
			account_id: value.account_id,
		});
	};
	return (
		<div className="modal-layer">
			<form
				className="edit-dialog"
				onSubmit={(event) => {
					event.preventDefault();
					if (valid) submit();
				}}
			>
				<h3>{isEdit ? "修改周期账单" : "创建周期账单"}</h3>
				<label>
					名称
					<input
						autoFocus
						maxLength={64}
						value={value.description}
						onChange={(event) =>
							setValue({ ...value, description: event.target.value })
						}
						placeholder="例如：房租"
					/>
				</label>
				<label>
					类型
					<select
						value={value.transaction_type}
						onChange={(event) =>
							setValue({
								...value,
								transaction_type: event.target.value as "expense" | "income",
							})
						}
					>
						<option value="expense">支出</option>
						<option value="income">收入</option>
					</select>
				</label>
				<label>
					金额
					<input
						type="number"
						min="0.01"
						step="0.01"
						value={value.amount}
						onChange={(event) =>
							setValue({ ...value, amount: event.target.value })
						}
						placeholder="3500"
					/>
				</label>
				<label>
					币种
					<input
						maxLength={3}
						value={value.currency}
						onChange={(event) =>
							setValue({ ...value, currency: event.target.value })
						}
						placeholder="CNY（留空）"
					/>
				</label>
				<label>
					分类
					<input
						maxLength={64}
						value={value.category}
						onChange={(event) =>
							setValue({ ...value, category: event.target.value })
						}
						placeholder="例如：房租"
					/>
				</label>
				<label>
					账户
					<select
						value={value.account_id}
						onChange={(event) =>
							setValue({ ...value, account_id: event.target.value })
						}
					>
						<option value="">请选择账户</option>
						{accounts.map((item) => (
							<option key={item.id} value={item.id}>
								{item.name}
								{item.is_default ? "（默认）" : ""}
							</option>
						))}
					</select>
				</label>
				<label>
					周期
					<select
						value={value.frequency}
						onChange={(event) =>
							setValue({
								...value,
								frequency: event.target.value as RecurringFrequency,
							})
						}
					>
						<option value="weekly">每周</option>
						<option value="monthly">每月</option>
						<option value="yearly">每年</option>
					</select>
				</label>
				<label>
					下次发生日期
					<input
						type="date"
						value={value.next_occurrence}
						onChange={(event) =>
							setValue({ ...value, next_occurrence: event.target.value })
						}
					/>
				</label>
				{error && <p className="form-error">{error}</p>}
				<div>
					<button type="button" onClick={onClose}>
						取消
					</button>
					<button className="primary-small" disabled={busy || !valid}>
						{busy ? "保存中…" : "保存"}
					</button>
				</div>
			</form>
		</div>
	);
}
