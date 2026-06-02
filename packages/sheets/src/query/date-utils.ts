import type { SheetColumn, SheetDataset, SheetFilter } from "../types.js";

export function latestDateInColumn(
	dataset: SheetDataset,
	column: SheetColumn,
): Date | undefined {
	let latest: Date | undefined;
	for (const row of dataset.rows) {
		const value = row.cells[column.id]?.value;
		if (!(value instanceof Date) || Number.isNaN(value.getTime())) continue;
		if (!latest || value.getTime() > latest.getTime()) latest = value;
	}
	return latest;
}

export function startOfDay(date: Date): Date {
	return new Date(
		Date.UTC(date.getUTCFullYear(), date.getUTCMonth(), date.getUTCDate()),
	);
}

export function addDays(date: Date, days: number): Date {
	const out = new Date(date);
	out.setUTCDate(out.getUTCDate() + days);
	return out;
}

export function startOfMonth(date: Date): Date {
	return new Date(Date.UTC(date.getUTCFullYear(), date.getUTCMonth(), 1));
}

export function addMonths(date: Date, months: number): Date {
	return new Date(
		Date.UTC(date.getUTCFullYear(), date.getUTCMonth() + months, 1),
	);
}

export function startOfYear(date: Date): Date {
	return new Date(Date.UTC(date.getUTCFullYear(), 0, 1));
}

export function addYears(date: Date, years: number): Date {
	return new Date(Date.UTC(date.getUTCFullYear() + years, 0, 1));
}

export function dateRangeFilter(
	column: SheetColumn,
	start?: Date,
	end?: Date,
): SheetFilter {
	const filter: SheetFilter = { kind: "dateRange", column: column.id };
	if (start) filter.start = start.toISOString();
	if (end) filter.end = end.toISOString();
	return filter;
}

export function parseQuestionDate(raw: string): Date | undefined {
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
