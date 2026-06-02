import { hasPhrase } from "../normalize.js";
import type { SheetColumn, SheetDataset, SheetFilter } from "../types.js";
import { findBestColumn, findDateColumn } from "./columns.js";
import type { RouteContext } from "./context.js";

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

export function findMonthFilter(
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

export function findDateRangeFilter(
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
