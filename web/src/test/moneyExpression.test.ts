import { describe, expect, it } from "vitest";
import { parseMoneyExpression } from "../moneyExpression";

function expectAmount(expression: string, expected: string): void {
	const result = parseMoneyExpression(expression);
	expect(result.valid).toBe(true);
	if (result.valid) expect(result.value.toFixed(2)).toBe(expected);
}

function expectInvalid(expression: string, error?: string): void {
	const result = parseMoneyExpression(expression);
	expect(result.valid).toBe(false);
	if (!result.valid && error) expect(result.error).toBe(error);
}

describe("parseMoneyExpression()", () => {
	it.each([
		["32", "32.00"],
		["32.00", "32.00"],
		["32+1-20", "13.00"],
		["100 / 4", "25.00"],
		["12.5 * 2", "25.00"],
		["(10+2)*3", "36.00"],
	])("calculates %s exactly", (expression, expected) => {
		expectAmount(expression, expected);
	});

	it("does not introduce binary floating-point errors", () => {
		expectAmount("0.1+0.2", "0.30");
	});

	it.each([
		["1/0", "不能除以 0"],
		["abc", undefined],
		["1+", undefined],
		["(1+2", "括号不匹配"],
		["", "请输入金额"],
		["--", undefined],
		["Infinity", undefined],
		["1%2", undefined],
	])("rejects invalid expression %s", (expression, error) => {
		expectInvalid(expression, error);
	});

	it.each([
		["1-1", "金额必须大于 0"],
		["1-2", "金额必须大于 0"],
	])("rejects non-positive result %s", (expression, error) => {
		expectInvalid(expression, error);
	});

	it("rounds only the final result to the ledger precision", () => {
		expectAmount("100/3", "33.33");
	});

	it("rejects extreme input length", () => {
		expectInvalid("1".repeat(201), "金额表达式过长");
	});
});
