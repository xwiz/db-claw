import type { SheetDataset, SheetFilter } from "../types.js";
import type { RouteContext } from "./context.js";
import { findDateRangeFilter, findMonthFilter } from "./date-filters.js";
import { findNumberFilters } from "./number-filters.js";
import { findEqualityFilters, findPresenceFilters } from "./value-filters.js";

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
