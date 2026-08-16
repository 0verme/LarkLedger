import type { ReactNode } from "react";

// P45 — 全站统一的页面状态视觉语言：
// 页面 / 卡片 / 列表 / 表格加载 → skeleton；
// 空内容 → EmptyState（icon + title + description + 可选 action）。
// 所有状态组件复用 Design System 既有 CSS（.page-skeleton / .table-skeleton /
// .empty-ledger），不引入第二套 loading 组件。

/**
 * 页面级骨架屏。rows 控制 shimmer 块数量，
 * 与各页面内容密度匹配（例如 Dashboard 3 块、列表页 2 块）。
 * role="status" + aria-label 让加载态对 screen reader 可感知，
 * 骨架本身不承载真实内容。
 */
export function PageSkeleton({ rows = 2 }: { rows?: number }) {
	return (
		<div className="page-skeleton" role="status" aria-label="正在加载">
			{Array.from({ length: rows }, (_, index) => (
				<div key={index} />
			))}
		</div>
	);
}

/**
 * 表格 / 列表区域骨架屏：与 PageSkeleton 同一种 shimmer 语言，
 * 用细条模拟表格行，替换原来“正在加载…“文字占位。
 */
export function TableSkeleton({ rows = 3 }: { rows?: number }) {
	return (
		<div className="table-skeleton" role="status" aria-label="正在加载">
			{Array.from({ length: rows }, (_, index) => (
				<div key={index} />
			))}
		</div>
	);
}

/**
 * 统一的空状态。产品页语气为「还没有……」，Admin / Ops 页保留
 * 更偏系统的「没有匹配的……」文案；仅当有明确创建动作时传入 action。
 */
export function EmptyState({
	icon,
	title,
	description,
	action,
}: {
	icon?: ReactNode;
	title: ReactNode;
	description?: ReactNode;
	action?: ReactNode;
}) {
	return (
		<div className="empty-ledger">
			{icon}
			<h3>{title}</h3>
			{description ? <p>{description}</p> : null}
			{action ? <div className="empty-action">{action}</div> : null}
		</div>
	);
}
