import { hasPhrase } from "../normalize.js";
import type { SheetColumn, SheetDataset, SheetOrder } from "../types.js";
import { findDateColumn } from "./columns.js";
import type { RouteContext } from "./context.js";
import {
	wantsBottom,
	wantsFirst,
	wantsLast,
	wantsLatest,
	wantsOldest,
	wantsRank,
} from "./intent-basics.js";

export function isListIntent(ctx: RouteContext): boolean {
	return (
		hasPhrase(ctx.normalized, "show") ||
		hasPhrase(ctx.normalized, "list") ||
		hasPhrase(ctx.normalized, "find") ||
		hasPhrase(ctx.normalized, "rows") ||
		hasPhrase(ctx.normalized, "records") ||
		hasPhrase(ctx.normalized, "entries") ||
		wantsFirst(ctx) ||
		wantsLast(ctx) ||
		wantsLatest(ctx) ||
		wantsOldest(ctx)
	);
}

export function listOrder(
	dataset: SheetDataset,
	ctx: RouteContext,
	measure: SheetColumn | undefined,
): SheetOrder | undefined {
	if (measure && wantsRank(ctx)) {
		return {
			column: measure.id,
			direction: wantsBottom(ctx) ? "asc" : "desc",
		};
	}
	const dateColumn = findDateColumn(dataset, ctx);
	if (dateColumn && (wantsLatest(ctx) || wantsOldest(ctx))) {
		return {
			column: dateColumn.id,
			direction: wantsOldest(ctx) ? "asc" : "desc",
		};
	}
	if (wantsLast(ctx)) return { column: "__row_index", direction: "desc" };
	if (wantsFirst(ctx)) return { column: "__row_index", direction: "asc" };
	return undefined;
}

export function listDistinctIntent(ctx: RouteContext): boolean {
	return (
		/^\s*(?:list|show)\b/.test(ctx.normalized) &&
		!wantsRank(ctx) &&
		!wantsFirst(ctx) &&
		!wantsLast(ctx) &&
		!wantsLatest(ctx) &&
		!wantsOldest(ctx)
	);
}
