import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { EmptyState, PageSkeleton, TableSkeleton } from "../components/States";

// P45 — 页面状态统一视觉语言：EmptyState / PageSkeleton / TableSkeleton。
describe("EmptyState", () => {
	it("renders title, description and action", () => {
		render(
			<EmptyState
				icon={<span aria-hidden>📦</span>}
				title="还没有账目"
				description="记下你的第一笔收支吧。"
				action={<button>记一笔</button>}
			/>,
		);
		expect(
			screen.getByRole("heading", { name: "还没有账目" }),
		).toBeInTheDocument();
		expect(screen.getByText("记下你的第一笔收支吧。")).toBeInTheDocument();
		expect(screen.getByRole("button", { name: "记一笔" })).toBeInTheDocument();
	});

	it("works without description or action", () => {
		render(
			<EmptyState icon={<span aria-hidden>⚙️</span>} title="没有匹配的事件" />,
		);
		expect(
			screen.getByRole("heading", { name: "没有匹配的事件" }),
		).toBeInTheDocument();
		expect(screen.queryByRole("button")).not.toBeInTheDocument();
	});
});

describe("PageSkeleton", () => {
	it("renders a status region with the requested number of blocks", () => {
		const { container } = render(<PageSkeleton rows={3} />);
		expect(
			screen.getByRole("status", { name: "正在加载" }),
		).toBeInTheDocument();
		expect(container.querySelectorAll(".page-skeleton > div")).toHaveLength(3);
	});

	it("defaults to two blocks", () => {
		const { container } = render(<PageSkeleton />);
		expect(container.querySelectorAll(".page-skeleton > div")).toHaveLength(2);
	});
});

describe("TableSkeleton", () => {
	it("renders a status region with skeleton rows", () => {
		const { container } = render(<TableSkeleton rows={4} />);
		expect(
			screen.getByRole("status", { name: "正在加载" }),
		).toBeInTheDocument();
		expect(container.querySelectorAll(".table-skeleton > div")).toHaveLength(4);
	});
});
