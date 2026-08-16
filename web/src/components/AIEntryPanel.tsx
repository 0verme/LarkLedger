import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { AlertTriangle, Check, Loader2, Send, Sparkles, X } from "lucide-react";
import {
	api,
	errorText,
	money,
	newIdempotencyKey,
	type AIEntryResult,
	type PendingActionResponse,
} from "../api";

// P39 §42 — the backend enforces the same cap; the UI mirrors it so an
// oversized prompt never reaches the provider.
const MAX_AI_TEXT = 500;

export type AIEntryStatus = AIEntryResult["status"];

function ResultPanel({ result, onClear }: { result: AIEntryResult; onClear: () => void }) {
	switch (result.status) {
		case "executed":
		case "query_result":
			return (
				<div className="ai-result ai-result-ok" role="status">
					<Check size={16} />
					<p>
						{result.message}
						{result.replayed ? <small>（已按原请求返回，未重复记账）</small> : null}
					</p>
				</div>
			);
		case "clarification_required":
			return (
				<div className="ai-result ai-result-hint" role="status">
					<Sparkles size={16} />
					<p>
						<strong>需要补充信息</strong>
						<span>{result.message}</span>
						<small>补充后直接修改上方输入内容再发送，例如：「记一笔 28 支出」。</small>
					</p>
				</div>
			);
		case "confirmation_required":
			return (
				<div className="ai-result ai-result-warn" role="status">
					<AlertTriangle size={16} />
					<p>
						<strong>这项操作需要确认</strong>
						<span>{result.message}</span>
						{result.expires_at ? (
							<small>确认单将在 {new Date(result.expires_at).toLocaleString()} 过期。</small>
						) : null}
					</p>
				</div>
			);
		default:
			return (
				<div className="ai-result ai-result-error" role="alert">
					<AlertTriangle size={16} />
					<p>
						{result.message}
						<small>
							请求编号：{result.request_id}（供排查，不会影响使用）
						</small>
					</p>
					<button type="button" className="ai-result-close" onClick={onClear} aria-label="关闭">
						<X size={14} />
					</button>
				</div>
			);
	}
}

function ConfirmationDialog({
	result,
	busy,
	error,
	onConfirm,
	onCancel,
}: {
	result: AIEntryResult;
	busy: boolean;
	error?: string;
	onConfirm: () => void;
	onCancel: () => void;
}) {
	const code = result.confirmation_code ?? result.pending_command_id ?? "";
	return (
		<div className="modal-layer">
			<div className="confirm-dialog ai-confirm">
				<h3>确认操作</h3>
				<p className="ai-confirm-copy">
					你确认要执行这次 AI 记账操作吗？{result.risk ? "（高风险操作需要确认。）" : ""}
				</p>
				{result.preview ? (
					<div className="ai-preview">
						{typeof result.preview.items === "object" &&
						Array.isArray(result.preview.items) &&
						result.preview.items.length ? (
							(result.preview.items as Array<{
								label?: string;
								amount?: string;
								direction?: string;
								category?: string;
								note?: string;
							}>).map((item, index) => (
								<div key={index}>
									<span>{item.label ?? item.category ?? "账目"}</span>
									<b>
										{item.direction === "income" ? "+" : item.direction === "expense" ? "-" : ""}
										{item.amount ? money(item.amount) : ""}
									</b>
								</div>
							))
						) : (
							<div>
								<span>{result.message}</span>
							</div>
						)}
					</div>
				) : null}
				<code className="ai-confirm-code">#{code}</code>
				{error && <p className="form-error">{error}</p>}
				<div>
					<button type="button" onClick={onCancel} disabled={busy}>
						取消
					</button>
					<button className="danger-solid" onClick={onConfirm} disabled={busy} aria-busy={busy}>
						{busy ? "处理中…" : "确认执行"}
					</button>
				</div>
			</div>
		</div>
	);
}

export function AIEntryPanel({ onDone }: { onDone: () => void }) {
	const [text, setText] = useState("");
	const [result, setResult] = useState<AIEntryResult | null>(null);
	const trimmed = text.trim();

	const submit = useMutation({
		mutationFn: async () => {
			const outcome = await api<AIEntryResult>("/ai/entries", {
				method: "POST",
				headers: { "Idempotency-Key": newIdempotencyKey() },
				body: JSON.stringify({ text: trimmed }),
			});
			return outcome;
		},
		onSuccess: (outcome) => {
			setResult(outcome);
			if (outcome.status === "executed") {
				setText("");
				onDone();
			}
		},
	});

	const confirm = useMutation({
		mutationFn: async (confirmationId: string) => {
			const outcome = await api<PendingActionResponse>(
				`/pending/${encodeURIComponent(confirmationId)}/confirm`,
				{ method: "POST" },
			);
			return outcome;
		},
		onSuccess: () => {
			setResult(null);
			setText("");
			onDone();
		},
	});

	const cancel = useMutation({
		mutationFn: async (confirmationId: string) => {
			await api<PendingActionResponse>(`/pending/${encodeURIComponent(confirmationId)}/cancel`, {
				method: "POST",
			});
		},
		onSuccess: () => {
			setResult(null);
		},
	});

	const confirmationId = result?.pending_command_id ?? null;
	const busy = submit.isPending || confirm.isPending || cancel.isPending;

	const send = () => {
		if (!trimmed || busy) return;
		submit.mutate();
	};

	return (
		<section className="panel ai-entry-panel">
			<div className="panel-title">
				<h3>
					<Sparkles size={16} /> 直接说一句
				</h3>
				<span>自然语言记账 · 结构化「记一笔」仍然可用</span>
			</div>
			<div className="ai-entry-box">
				<textarea
					value={text}
					onChange={(event) => setText(event.target.value)}
					onKeyDown={(event) => {
						if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
							event.preventDefault();
							send();
						}
					}}
					placeholder="例如：午饭28、工资18000、昨天打车35、星巴克32用招行…"
					maxLength={MAX_AI_TEXT}
					disabled={busy}
					aria-label="AI 记账输入"
					rows={2}
				/>
				<div className="ai-entry-actions">
					<span className="ai-entry-count">
						{text.length}/{MAX_AI_TEXT}
					</span>
					<button
						className="primary-small"
						onClick={send}
						disabled={busy || !trimmed}
						aria-busy={submit.isPending}
					>
						{submit.isPending ? <Loader2 className="spin" size={15} /> : <Send size={15} />}
						{submit.isPending ? "解析中…" : "发送"}
					</button>
				</div>
				{submit.error ? (
					<div className="ai-result ai-result-error" role="alert">
						<AlertTriangle size={16} />
						<p>{errorText(submit.error)}</p>
					</div>
				) : null}
			</div>
			{result && result.status !== "confirmation_required" ? (
				<ResultPanel result={result} onClear={() => setResult(null)} />
			) : null}
			{result?.status === "confirmation_required" && confirmationId ? (
				<ConfirmationDialog
					result={result}
					busy={busy}
					error={confirm.error || cancel.error ? errorText(confirm.error ?? cancel.error) : undefined}
					onConfirm={() => confirm.mutate(confirmationId)}
					onCancel={() => cancel.mutate(confirmationId)}
				/>
			) : null}
		</section>
	);
}
