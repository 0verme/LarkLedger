import { useState } from "react";
import { ArrowDownRight, ArrowUpRight, CalendarDays, Coins } from "lucide-react";
import { newIdempotencyKey } from "../api";

export type EntryDirection = "expense" | "income";

export type QuickEntryBody = {
	amount: string;
	direction: EntryDirection;
	category: string;
	note: string;
	account_id: string | null;
	occurred_at: string;
};

// P38 §12 — remembered last-used account per browser (UX only; authorization
// always comes from the backend LedgerAuthorizationService).
const LAST_ACCOUNT_KEY = "larkledger:last-account";

// P38 §12 — the high-frequency categories shown as one-tap chips. The input
// stays free-form so any category the domain accepts can be typed.
const COMMON_CATEGORIES = [
	"餐饮",
	"交通",
	"购物",
	"居住",
	"娱乐",
	"医疗",
	"教育",
	"工资",
	"理财",
	"其他",
];

function rememberAccount(accountId: string) {
	try {
		window.localStorage.setItem(LAST_ACCOUNT_KEY, accountId);
	} catch {
		// localStorage unavailable (private mode) — remembering is best-effort.
	}
}

function rememberedAccount(): string | null {
	try {
		return window.localStorage.getItem(LAST_ACCOUNT_KEY);
	} catch {
		return null;
	}
}

function localNow(): string {
	const now = new Date();
	now.setMinutes(now.getMinutes() - now.getTimezoneOffset());
	return now.toISOString().slice(0, 16);
}

export function QuickEntryDialog({
	accounts,
	busy,
	error,
	onClose,
	onSave,
}: {
	accounts: Array<{ id: string; name: string; status: string }>;
	busy: boolean;
	error?: string;
	onClose: () => void;
	onSave: (body: QuickEntryBody, idempotencyKey: string) => void;
}) {
	const [direction, setDirection] = useState<EntryDirection>("expense");
	const [amount, setAmount] = useState("");
	const [category, setCategory] = useState("");
	const [note, setNote] = useState("");
	const remembered = rememberedAccount();
	const usable = accounts.filter((account) => account.status !== "archived");
	const [accountId, setAccountId] = useState(
		remembered && usable.some((account) => account.id === remembered)
			? remembered
			: "",
	);
	const [occurred, setOccurred] = useState(localNow);
	const submit = () => {
		onSave(
			{
				amount,
				direction,
				category: category.trim(),
				note: note.trim(),
				account_id: accountId || null,
				occurred_at: new Date(occurred).toISOString(),
			},
			newIdempotencyKey(),
		);
		if (accountId) rememberAccount(accountId);
	};
	const valid = Number(amount) > 0 && category.trim().length > 0;
	return (
		<div className="modal-layer">
			<form
				className="edit-dialog quick-entry"
				onSubmit={(event) => {
					event.preventDefault();
					submit();
				}}
			>
				<h3>记一笔</h3>
				<div className="direction-toggle" role="group" aria-label="收支方向">
					<button
						type="button"
						className={direction === "expense" ? "active expense" : ""}
						aria-pressed={direction === "expense"}
						onClick={() => setDirection("expense")}
					>
						<ArrowDownRight size={16} /> 支出
					</button>
					<button
						type="button"
						className={direction === "income" ? "active income" : ""}
						aria-pressed={direction === "income"}
						onClick={() => setDirection("income")}
					>
						<ArrowUpRight size={16} /> 收入
					</button>
				</div>
				<label>
					金额（{direction === "expense" ? "支出" : "收入"}）
					<div className="amount-input">
						<Coins size={17} />
						<input
							autoFocus
							inputMode="decimal"
							type="number"
							min="0.01"
							step="0.01"
							placeholder="0.00"
							value={amount}
							onChange={(event) => setAmount(event.target.value)}
						/>
					</div>
				</label>
				<label>
					账户
					<select
						value={accountId}
						onChange={(event) => setAccountId(event.target.value)}
					>
						<option value="">（默认账户）</option>
						{usable.map((account) => (
							<option key={account.id} value={account.id}>
								{account.name}
							</option>
						))}
					</select>
				</label>
				<label>
					分类
					<input
						maxLength={64}
						value={category}
						onChange={(event) => setCategory(event.target.value)}
						placeholder="例如：餐饮"
					/>
				</label>
				<div className="category-chips" role="group" aria-label="常用分类">
					{COMMON_CATEGORIES.map((item) => (
						<button
							type="button"
							key={item}
							className={category === item ? "active" : ""}
							onClick={() => setCategory(item)}
						>
							{item}
						</button>
					))}
				</div>
				<label>
					备注
					<textarea
						maxLength={500}
						value={note}
						onChange={(event) => setNote(event.target.value)}
						placeholder="可选"
					/>
				</label>
				<label>
					发生时间
					<div className="amount-input">
						<CalendarDays size={17} />
						<input
							type="datetime-local"
							value={occurred}
							onChange={(event) => setOccurred(event.target.value)}
						/>
					</div>
				</label>
				{error && (
					<p className="form-error">
						{error}
						{/* request id is safe to show and helps support without
						    leaking internals (P38 §23) */}
					</p>
				)}
				<div>
					<button type="button" onClick={onClose} disabled={busy}>
						取消
					</button>
					<button
						className="primary-small"
						disabled={busy || !valid}
						aria-busy={busy}
					>
						{busy ? "保存中…" : "保存"}
					</button>
				</div>
			</form>
		</div>
	);
}
