import { describe, expect, it } from "vitest";
import { money } from "../api";

// P45 — 全站金额展示统一走 money()：
// Intl.NumberFormat zh-CN、默认 CNY、preservation-only（不做运算）。
describe("money()", () => {
	it("formats CNY with two decimals and thousands separators", () => {
		expect(money("1234.5")).toBe("¥1,234.50");
		expect(money("300.00")).toBe("¥300.00");
	});

	it("accepts number input as well as string", () => {
		expect(money(300)).toBe("¥300.00");
	});

	it("formats zero", () => {
		expect(money("0")).toBe("¥0.00");
		expect(money(0)).toBe("¥0.00");
	});

	it("formats negative amounts", () => {
		expect(money("-42.1")).toBe("-¥42.10");
	});

	it("formats decimal amounts with rounding on presentation only", () => {
		expect(money("28.05")).toBe("¥28.05");
	});

	it("formats large values", () => {
		expect(money("1234567890.12")).toBe("¥1,234,567,890.12");
	});

	it("honors the currency parameter for models that carry their own", () => {
		const usd = money("100", "USD");
		expect(usd).toContain("100.00");
		expect(usd).not.toContain("¥");
	});
});
