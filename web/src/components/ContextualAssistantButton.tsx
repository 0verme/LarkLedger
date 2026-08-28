import { useEffect, useState } from "react";
import { MessageCircle, X } from "lucide-react";
import { AIEntryPanel } from "./AIEntryPanel";
import type { AssistantPageContext } from "../api";

export function ContextualAssistantButton({
	context,
	label = "问问助手",
}: {
	context: AssistantPageContext;
	label?: string;
}) {
	const [open, setOpen] = useState(false);
	useEffect(() => {
		if (!open) return;
		const closeOnEscape = (event: KeyboardEvent) => {
			if (event.key === "Escape") setOpen(false);
		};
		window.addEventListener("keydown", closeOnEscape);
		return () => window.removeEventListener("keydown", closeOnEscape);
	}, [open]);

	return (
		<>
			<button
				type="button"
				className="quiet-button contextual-assistant-button"
				onClick={() => setOpen(true)}
				aria-haspopup="dialog"
			>
				<MessageCircle size={16} /> {label}
			</button>
			{open && (
				<div
					className="modal-layer contextual-assistant-layer"
					role="presentation"
					onMouseDown={(event) => {
						if (event.target === event.currentTarget) setOpen(false);
					}}
				>
					<div className="assistant-dialog" role="dialog" aria-modal="true" aria-label="智能助手">
						<button
							type="button"
							className="assistant-dialog-close"
							onClick={() => setOpen(false)}
							aria-label="关闭智能助手"
						>
							<X size={17} />
						</button>
						<div className="assistant-context-note">
							已带入当前页面的时间与筛选条件；账本和权限仍以服务器会话为准。
						</div>
						<AIEntryPanel
							pageContext={context}
							onDone={() => setOpen(false)}
						/>
					</div>
				</div>
			)}
		</>
	);
}
