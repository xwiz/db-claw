import type { NumberOperator, SheetDataset, SheetFilter } from "../types.js";
import { findMeasureColumn } from "./columns.js";
import type { RouteContext } from "./context.js";

function parseNumberComparison(ctx: RouteContext):
	| {
			operator: NumberOperator;
			value: number;
	  }
	| undefined {
	const match = ctx.normalized.match(
		/\b(over|greater than|above|more than|at least|under|less than|below|at most|equal to|equals)\s+(-?\d+(?:\.\d+)?)\b/,
	);
	if (!match?.[1] || !match[2]) return undefined;
	const phrase = match[1];
	const value = Number(match[2]);
	if (!Number.isFinite(value)) return undefined;
	if (["over", "greater than", "above", "more than"].includes(phrase)) {
		return { operator: "gt", value };
	}
	if (phrase === "at least") return { operator: "gte", value };
	if (["under", "less than", "below"].includes(phrase)) {
		return { operator: "lt", value };
	}
	if (phrase === "at most") return { operator: "lte", value };
	return { operator: "eq", value };
}

function parseNumberRange(
	ctx: RouteContext,
): { min: number; max: number } | undefined {
	const match = ctx.normalized.match(
		/\bbetween\s+(-?\d+(?:\.\d+)?)\s+(?:and|to)\s+(-?\d+(?:\.\d+)?)\b/,
	);
	if (!match?.[1] || !match[2]) return undefined;
	const left = Number(match[1]);
	const right = Number(match[2]);
	if (!Number.isFinite(left) || !Number.isFinite(right)) return undefined;
	return { min: Math.min(left, right), max: Math.max(left, right) };
}

export function comparedNumberValues(ctx: RouteContext): number[] {
	const values: number[] = [];
	const comparison = parseNumberComparison(ctx);
	if (comparison) values.push(comparison.value);
	const range = parseNumberRange(ctx);
	if (range) values.push(range.min, range.max);
	return values;
}

export function findNumberFilters(
	dataset: SheetDataset,
	ctx: RouteContext,
): SheetFilter[] {
	const filters: SheetFilter[] = [];
	const range = parseNumberRange(ctx);
	if (range) {
		const column = findMeasureColumn(dataset, ctx);
		if (!column) return filters;
		filters.push({
			kind: "number",
			column: column.id,
			operator: "gte",
			value: range.min,
		});
		filters.push({
			kind: "number",
			column: column.id,
			operator: "lte",
			value: range.max,
		});
		return filters;
	}
	const comparison = parseNumberComparison(ctx);
	if (!comparison) return filters;
	const column = findMeasureColumn(dataset, ctx);
	if (!column) return filters;
	filters.push({
		kind: "number",
		column: column.id,
		operator: comparison.operator,
		value: comparison.value,
	});
	return filters;
}
