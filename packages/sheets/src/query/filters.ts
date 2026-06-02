import { hasPhrase, normalizeText } from "../normalize.js";
import type {
	NumberOperator,
	SheetColumn,
	SheetDataset,
	SheetFilter,
} from "../types.js";
import {
	findBestColumn,
	findDateColumn,
	findMeasureColumn,
	findMentionedColumn,
	isLikelyLineNumberColumn,
	isLongTextColumn,
	isSensitiveColumn,
} from "./columns.js";
import type { RouteContext } from "./context.js";
import { topLimit } from "./intents.js";

const MONTHS = [
	"january",
	"february",
	"march",
	"april",
	"may",
	"june",
	"july",
	"august",
	"september",
	"october",
	"november",
	"december",
];

function findMonthFilter(
	dataset: SheetDataset,
	ctx: RouteContext,
): SheetFilter | undefined {
	const monthIdx = MONTHS.findIndex((month) =>
		hasPhrase(ctx.normalized, month),
	);
	if (monthIdx < 0) return undefined;
	const dateColumn =
		findBestColumn(dataset, ctx, (column) => column.kind === "date") ??
		dataset.columns.find((column) => column.kind === "date");
	if (!dateColumn) return undefined;
	const yearMatch = ctx.normalized.match(/\b(20\d{2}|19\d{2})\b/);
	const filter: SheetFilter = {
		kind: "month",
		column: dateColumn.id,
		month: monthIdx + 1,
	};
	if (yearMatch?.[1]) filter.year = Number(yearMatch[1]);
	return filter;
}

function dateValues(dataset: SheetDataset, column: SheetColumn): Date[] {
	const values: Date[] = [];
	for (const row of dataset.rows) {
		const value = row.cells[column.id]?.value;
		if (value instanceof Date && !Number.isNaN(value.getTime())) {
			values.push(value);
		}
	}
	return values;
}

function latestDateInColumn(
	dataset: SheetDataset,
	column: SheetColumn,
): Date | undefined {
	let latest: Date | undefined;
	for (const value of dateValues(dataset, column)) {
		if (!latest || value.getTime() > latest.getTime()) latest = value;
	}
	return latest;
}

function startOfDay(date: Date): Date {
	return new Date(
		Date.UTC(date.getUTCFullYear(), date.getUTCMonth(), date.getUTCDate()),
	);
}

function addDays(date: Date, days: number): Date {
	const out = new Date(date);
	out.setUTCDate(out.getUTCDate() + days);
	return out;
}

function startOfMonth(date: Date): Date {
	return new Date(Date.UTC(date.getUTCFullYear(), date.getUTCMonth(), 1));
}

function addMonths(date: Date, months: number): Date {
	return new Date(
		Date.UTC(date.getUTCFullYear(), date.getUTCMonth() + months, 1),
	);
}

function startOfYear(date: Date): Date {
	return new Date(Date.UTC(date.getUTCFullYear(), 0, 1));
}

function addYears(date: Date, years: number): Date {
	return new Date(Date.UTC(date.getUTCFullYear() + years, 0, 1));
}

function isoDate(date: Date): string {
	return date.toISOString();
}

function dateRangeFilter(
	column: SheetColumn,
	start?: Date,
	end?: Date,
): SheetFilter {
	const filter: SheetFilter = { kind: "dateRange", column: column.id };
	if (start) filter.start = isoDate(start);
	if (end) filter.end = isoDate(end);
	return filter;
}

function parseQuestionDate(raw: string): Date | undefined {
	const month =
		"(?:jan|january|feb|february|mar|march|apr|april|may|jun|june|jul|july|aug|august|sep|sept|september|oct|october|nov|november|dec|december)";
	const match =
		raw.match(/\b(\d{4}-\d{1,2}-\d{1,2})\b/) ??
		raw.match(/\b(\d{1,2}\/\d{1,2}\/\d{2,4})\b/) ??
		raw.match(new RegExp(`\\b(${month}\\s+\\d{1,2},?\\s+\\d{4})\\b`, "i"));
	if (!match?.[1]) return undefined;
	const parsed = new Date(match[1]);
	if (Number.isNaN(parsed.getTime())) return undefined;
	return startOfDay(parsed);
}

function findDateRangeFilter(
	dataset: SheetDataset,
	ctx: RouteContext,
): SheetFilter | undefined {
	const column = findDateColumn(dataset, ctx);
	if (!column) return undefined;

	const anchor = latestDateInColumn(dataset, column);
	if (!anchor) return undefined;
	const anchorDay = startOfDay(anchor);

	const lastDays = ctx.normalized.match(
		/\b(?:last|past)\s+(\d{1,3})\s+days?\b/,
	);
	if (lastDays?.[1]) {
		const days = Number(lastDays[1]);
		if (Number.isFinite(days) && days > 0) {
			return dateRangeFilter(
				column,
				addDays(anchorDay, -(days - 1)),
				addDays(anchorDay, 1),
			);
		}
	}

	if (hasPhrase(ctx.normalized, "today")) {
		return dateRangeFilter(column, anchorDay, addDays(anchorDay, 1));
	}
	if (hasPhrase(ctx.normalized, "yesterday")) {
		return dateRangeFilter(column, addDays(anchorDay, -1), anchorDay);
	}
	if (
		hasPhrase(ctx.normalized, "this month") ||
		hasPhrase(ctx.normalized, "current month")
	) {
		const start = startOfMonth(anchorDay);
		return dateRangeFilter(column, start, addMonths(start, 1));
	}
	if (hasPhrase(ctx.normalized, "last month")) {
		const end = startOfMonth(anchorDay);
		return dateRangeFilter(column, addMonths(end, -1), end);
	}
	if (
		hasPhrase(ctx.normalized, "this year") ||
		hasPhrase(ctx.normalized, "current year")
	) {
		const start = startOfYear(anchorDay);
		return dateRangeFilter(column, start, addYears(start, 1));
	}
	if (hasPhrase(ctx.normalized, "last year")) {
		const end = startOfYear(anchorDay);
		return dateRangeFilter(column, addYears(end, -1), end);
	}

	const yearMatch = ctx.normalized.match(
		/\b(?:in|during|for)\s+(20\d{2}|19\d{2})\b/,
	);
	if (yearMatch?.[1]) {
		const year = Number(yearMatch[1]);
		const start = new Date(Date.UTC(year, 0, 1));
		return dateRangeFilter(column, start, addYears(start, 1));
	}

	const explicitDate = parseQuestionDate(ctx.raw);
	if (explicitDate) {
		if (
			hasPhrase(ctx.normalized, "before") ||
			hasPhrase(ctx.normalized, "older than")
		) {
			return dateRangeFilter(column, undefined, explicitDate);
		}
		if (
			hasPhrase(ctx.normalized, "after") ||
			hasPhrase(ctx.normalized, "since") ||
			hasPhrase(ctx.normalized, "newer than")
		) {
			return dateRangeFilter(column, explicitDate, undefined);
		}
		if (hasPhrase(ctx.normalized, "on")) {
			return dateRangeFilter(column, explicitDate, addDays(explicitDate, 1));
		}
	}

	return undefined;
}

function uniqueRawValues(dataset: SheetDataset, column: SheetColumn): string[] {
	const seen = new Set<string>();
	const out: string[] = [];
	for (const row of dataset.rows) {
		const cell = row.cells[column.id];
		if (!cell || cell.normalized.length === 0 || seen.has(cell.normalized))
			continue;
		seen.add(cell.normalized);
		out.push(cell.raw);
	}
	return out;
}

function booleanPresenceFilter(
	column: SheetColumn,
	present: boolean,
): SheetFilter {
	if (column.kind === "boolean") {
		return { kind: "equals", column: column.id, value: present };
	}
	const yesNo = column.examples
		.map((example) => normalizeText(example))
		.filter((example) => example.length > 0);
	if (
		yesNo.length > 0 &&
		yesNo.every((example) =>
			["yes", "no", "true", "false", "y", "n"].includes(example),
		)
	) {
		const value = present
			? column.examples.find((example) =>
					["yes", "true", "y"].includes(normalizeText(example)),
				)
			: column.examples.find((example) =>
					["no", "false", "n"].includes(normalizeText(example)),
				);
		if (value) return { kind: "equals", column: column.id, value };
	}
	return { kind: "presence", column: column.id, present };
}

function findPresenceFilters(
	dataset: SheetDataset,
	ctx: RouteContext,
): SheetFilter[] {
	const filters: SheetFilter[] = [];
	const missingPatterns = [
		/\b(?:where\s+)?(.+?)\s+(?:is|are)\s+(?:missing|blank|empty|null)\b/,
		/\b(?:missing|blank|empty|null)\s+(.+?)\b/,
	];
	const presentPatterns = [
		/\b(?:where\s+)?(.+?)\s+(?:is|are)\s+not\s+(?:missing|blank|empty|null)\b/,
		/\b(?:where\s+)?(.+?)\s+(?:is|are)\s+(?:present|filled|set|available)\b/,
		/\b(?:with|has|have)\s+(.+?)\b/,
	];
	const withoutMatch = ctx.normalized.match(/\bwithout\s+(.+?)\b/);
	if (withoutMatch?.[1]) {
		const column = findMentionedColumn(dataset, withoutMatch[1]);
		if (column) filters.push(booleanPresenceFilter(column, false));
	}
	for (const pattern of missingPatterns) {
		const match = ctx.normalized.match(pattern);
		if (!match?.[1]) continue;
		const column = findMentionedColumn(dataset, match[1]);
		if (column)
			filters.push({ kind: "presence", column: column.id, present: false });
	}
	for (const pattern of presentPatterns) {
		const match = ctx.normalized.match(pattern);
		if (!match?.[1]) continue;
		const column = findMentionedColumn(dataset, match[1]);
		if (column) filters.push(booleanPresenceFilter(column, true));
	}
	return filters;
}

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

function comparedNumberValues(ctx: RouteContext): number[] {
	const values: number[] = [];
	const comparison = parseNumberComparison(ctx);
	if (comparison) values.push(comparison.value);
	const range = parseNumberRange(ctx);
	if (range) values.push(range.min, range.max);
	return values;
}

function isNegativeValueMention(
	normalizedQuestion: string,
	normalizedValue: string,
): boolean {
	const escaped = normalizedValue.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
	const patterns = [
		new RegExp(
			`\\bnot\\s+(?:in\\s+|from\\s+|equal\\s+to\\s+|equals\\s+)?${escaped}\\b`,
		),
		new RegExp(`\\b(?:except|excluding|exclude)\\s+${escaped}\\b`),
		new RegExp(`\\b(?:not|without)\\s+.*\\b${escaped}\\b`),
	];
	return patterns.some((pattern) => pattern.test(normalizedQuestion));
}

function findEqualityFilters(
	dataset: SheetDataset,
	ctx: RouteContext,
): SheetFilter[] {
	const filters: SheetFilter[] = [];
	const filteredColumns = new Set<string>();
	const requestedLimit = topLimit(ctx);
	const comparedNumbers = comparedNumberValues(ctx);
	for (const column of dataset.columns) {
		const filterable =
			column.roles.includes("dimension") ||
			(column.roles.includes("display") &&
				column.uniqueCount <= 1000 &&
				!isLongTextColumn(column));
		if (
			!filterable ||
			isSensitiveColumn(column) ||
			isLikelyLineNumberColumn(column)
		) {
			continue;
		}
		for (const raw of uniqueRawValues(dataset, column)) {
			const normalized = normalizeText(raw);
			if (normalized.length === 0) continue;
			if (normalized.length > 80) continue;
			if (
				requestedLimit !== undefined &&
				/^\d+$/.test(normalized) &&
				Number(normalized) === requestedLimit
			) {
				continue;
			}
			if (
				/^-?\d+(?:\.\d+)?$/.test(normalized) &&
				comparedNumbers.some((value) => Number(normalized) === value)
			) {
				continue;
			}
			if (!hasPhrase(ctx.normalized, normalized)) continue;
			if (filteredColumns.has(column.id)) continue;
			filters.push({
				kind: isNegativeValueMention(ctx.normalized, normalized)
					? "notEquals"
					: "equals",
				column: column.id,
				value: raw,
			});
			filteredColumns.add(column.id);
		}
	}
	return filters;
}

function findNumberFilters(
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

function dedupeFilters(filters: SheetFilter[]): SheetFilter[] {
	const seen = new Set<string>();
	const out: SheetFilter[] = [];
	for (const filter of filters) {
		const key = JSON.stringify(filter);
		if (seen.has(key)) continue;
		seen.add(key);
		out.push(filter);
	}
	return out;
}

export function buildFilters(
	dataset: SheetDataset,
	ctx: RouteContext,
): SheetFilter[] {
	return dedupeFilters(
		[
			...findEqualityFilters(dataset, ctx),
			...findPresenceFilters(dataset, ctx),
			findDateRangeFilter(dataset, ctx),
			findMonthFilter(dataset, ctx),
			...findNumberFilters(dataset, ctx),
		].filter((filter): filter is SheetFilter => filter !== undefined),
	);
}
