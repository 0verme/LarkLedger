import Decimal from "decimal.js";

const MAX_EXPRESSION_LENGTH = 200;
const MAX_NUMBER_DIGITS = 30;
const MAX_TOKEN_COUNT = 100;
const MAX_PARENTHESIS_DEPTH = 50;
const MONEY_DECIMAL_PLACES = 2;
const MAX_MONEY = "999999999999.99";

// Keep the calculator independent from Decimal's global configuration. The
// precision is deliberately higher than the ledger's two decimal places so
// division is rounded only when the final amount is normalized.
const CalculatorDecimal = Decimal.clone({
	precision: 80,
	rounding: Decimal.ROUND_HALF_UP,
	maxE: 1000,
	minE: -1000,
});

type BinaryOperator = "+" | "-" | "*" | "/";

type Token =
	| { kind: "number"; value: string }
	| { kind: "operator"; value: BinaryOperator }
	| { kind: "leftParen" }
	| { kind: "rightParen" };

export type MoneyExpressionResult =
	| { valid: true; value: Decimal }
	| { valid: false; error: string };

class ExpressionError extends Error {}

function fail(message: string): never {
	throw new ExpressionError(message);
}

function isDigit(value: string): boolean {
	return value >= "0" && value <= "9";
}

function tokenize(expression: string): Token[] {
	if (expression.length > MAX_EXPRESSION_LENGTH) {
		fail("金额表达式过长");
	}

	const tokens: Token[] = [];
	let index = 0;
	while (index < expression.length) {
		const character = expression[index];
		if (character === " ") {
			index += 1;
			continue;
		}

		if (isDigit(character) || character === ".") {
			const start = index;
			let digitCount = 0;
			let hasDecimalPoint = false;
			while (index < expression.length) {
				const current = expression[index];
				if (isDigit(current)) {
					digitCount += 1;
					if (digitCount > MAX_NUMBER_DIGITS) {
						fail("金额数字位数过多");
					}
					index += 1;
					continue;
				}
				if (current === ".") {
					if (hasDecimalPoint) fail("金额格式不正确");
					hasDecimalPoint = true;
					index += 1;
					continue;
				}
				break;
			}
			if (digitCount === 0) fail("金额格式不正确");
			if (tokens.length >= MAX_TOKEN_COUNT) fail("金额表达式过于复杂");
			tokens.push({ kind: "number", value: expression.slice(start, index) });
			continue;
		}

		if (character === "+" || character === "-" || character === "*" || character === "/") {
			if (tokens.length >= MAX_TOKEN_COUNT) fail("金额表达式过于复杂");
			tokens.push({ kind: "operator", value: character });
			index += 1;
			continue;
		}
		if (character === "(") {
			if (tokens.length >= MAX_TOKEN_COUNT) fail("金额表达式过于复杂");
			tokens.push({ kind: "leftParen" });
			index += 1;
			continue;
		}
		if (character === ")") {
			if (tokens.length >= MAX_TOKEN_COUNT) fail("金额表达式过于复杂");
			tokens.push({ kind: "rightParen" });
			index += 1;
			continue;
		}

		fail("金额表达式包含不支持的字符");
	}
	return tokens;
}

class Parser {
	private index = 0;
	private parenthesisDepth = 0;

	constructor(private readonly tokens: Token[]) {}

	parse(): Decimal {
		if (this.tokens.length === 0) fail("请输入金额");
		const value = this.parseAdditive();
		const remaining = this.peek();
		if (!remaining) return value;
		if (remaining.kind === "rightParen") fail("括号不匹配");
		if (remaining.kind === "number" || remaining.kind === "leftParen") {
			fail("缺少运算符");
		}
		fail("表达式不完整");
	}

	private parseAdditive(): Decimal {
		let value = this.parseMultiplicative();
		while (this.isOperator("+") || this.isOperator("-")) {
			const operator = this.advance();
			if (operator.kind !== "operator") throw new Error("unreachable");
			const right = this.parseMultiplicative();
			value = operator.value === "+" ? value.plus(right) : value.minus(right);
			this.ensureFinite(value);
		}
		return value;
	}

	private parseMultiplicative(): Decimal {
		let value = this.parsePrimary();
		while (this.isOperator("*") || this.isOperator("/")) {
			const operator = this.advance();
			if (operator.kind !== "operator") throw new Error("unreachable");
			const right = this.parsePrimary();
			if (operator.value === "/" && right.isZero()) fail("不能除以 0");
			value = operator.value === "*" ? value.times(right) : value.dividedBy(right);
			this.ensureFinite(value);
		}
		return value;
	}

	private parsePrimary(): Decimal {
		const token = this.peek();
		if (!token) fail("表达式不完整");
		if (token.kind === "number") {
			this.advance();
			const value = new CalculatorDecimal(token.value);
			this.ensureFinite(value);
			return value;
		}
		if (token.kind === "leftParen") {
			if (this.parenthesisDepth >= MAX_PARENTHESIS_DEPTH) {
				fail("金额表达式嵌套过深");
			}
			this.advance();
			if (this.peek()?.kind === "rightParen") fail("括号内不能为空");
			this.parenthesisDepth += 1;
			const value = this.parseAdditive();
			this.parenthesisDepth -= 1;
			if (this.peek()?.kind !== "rightParen") fail("括号不匹配");
			this.advance();
			return value;
		}
		if (token.kind === "rightParen") fail("括号不匹配");
		fail("运算符位置不正确");
	}

	private ensureFinite(value: Decimal): void {
		if (!value.isFinite()) fail("计算结果无效");
	}

	private isOperator(value: BinaryOperator): boolean {
		const token = this.peek();
		return token?.kind === "operator" && token.value === value;
	}

	private peek(): Token | undefined {
		return this.tokens[this.index];
	}

	private advance(): Token {
		const token = this.tokens[this.index];
		if (!token) throw new Error("parser advanced past the end");
		this.index += 1;
		return token;
	}
}

export function parseMoneyExpression(expression: string): MoneyExpressionResult {
	if (typeof expression !== "string") return { valid: false, error: "请输入金额" };

	try {
		const value = new Parser(tokenize(expression)).parse();
		if (!value.isFinite()) return { valid: false, error: "计算结果无效" };
		const normalized = value.toDecimalPlaces(
			MONEY_DECIMAL_PLACES,
			CalculatorDecimal.ROUND_HALF_UP,
		);
		if (!normalized.isFinite()) return { valid: false, error: "计算结果无效" };
		if (normalized.lte(0)) return { valid: false, error: "金额必须大于 0" };
		if (normalized.gt(new CalculatorDecimal(MAX_MONEY))) {
			return { valid: false, error: "金额超出支持范围" };
		}
		return { valid: true, value: normalized };
	} catch (error) {
		if (error instanceof ExpressionError) {
			return { valid: false, error: error.message };
		}
		return { valid: false, error: "计算结果无效" };
	}
}
