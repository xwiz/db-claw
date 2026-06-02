import { hasPhrase } from "../normalize.js";
import type {
	AggregateFunction,
	SheetColumn,
	SheetDataset,
	SheetOrder,
} from "../types.js";
import { findDateColumn, isIdentifierishColumn } from "./columns.js";
import type { RouteContext } from "./context.js";

export function aggregateIntent(
	ctx: RouteContext,
): AggregateFunction | undefined {
	if (
		hasPhrase(ctx.normalized, "how many") ||
		hasPhrase(ctx.normalized, "count")
	) {
		return "count";
	}
	if (
		hasPhrase(ctx.normalized, "average") ||
		hasPhrase(ctx.normalized, "avg") ||
		hasPhrase(ctx.normalized, "mean")
	) {
		return "avg";
	}
	if (
		hasPhrase(ctx.normalized, "total") ||
		hasPhrase(ctx.normalized, "sum") ||
		hasPhrase(ctx.normalized, "revenue")
	) {
		return "sum";
	}
	if (
		hasPhrase(ctx.normalized, "highest") ||
		hasPhrase(ctx.normalized, "maximum") ||
		hasPhrase(ctx.normalized, "max")
	) {
		return "max";
	}
	if (
		hasPhrase(ctx.normalized, "lowest") ||
		hasPhrase(ctx.normalized, "minimum") ||
		hasPhrase(ctx.normalized, "min")
	) {
		return "min";
	}
	return undefined;
}

export function topLimit(ctx: RouteContext): number | undefined {
	const top = ctx.normalized.match(/\btop\s+(\d{1,3})\b/);
	if (top?.[1]) return Number(top[1]);
	const which = ctx.normalized.match(/\bwhich\s+(\d{1,3})\b/);
	if (which?.[1]) return Number(which[1]);
	const firstLast = ctx.normalized.match(
		/\b(?:first|last|latest|recent|newest|oldest)\s+(\d{1,3})\b/,
	);
	if (firstLast?.[1]) return Number(firstLast[1]);
	const limit = ctx.normalized.match(/\b(?:limit|show|list)\s+(\d{1,3})\b/);
	if (limit?.[1]) return Number(limit[1]);
	if (wantsTop(ctx) || wantsBottom(ctx)) return 1;
	return undefined;
}

export function wantsTop(ctx: RouteContext): boolean {
	return (
		hasPhrase(ctx.normalized, "most") ||
		hasPhrase(ctx.normalized, "highest") ||
		hasPhrase(ctx.normalized, "maximum") ||
		hasPhrase(ctx.normalized, "largest") ||
		hasPhrase(ctx.normalized, "greatest")
	);
}

export function wantsBottom(ctx: RouteContext): boolean {
	return (
		hasPhrase(ctx.normalized, "bottom") ||
		hasPhrase(ctx.normalized, "lowest") ||
		hasPhrase(ctx.normalized, "least")
	);
}

export function wantsFirst(ctx: RouteContext): boolean {
	return hasPhrase(ctx.normalized, "first");
}

export function wantsLast(ctx: RouteContext): boolean {
	return hasPhrase(ctx.normalized, "last");
}

export function wantsLatest(ctx: RouteContext): boolean {
	return (
		hasPhrase(ctx.normalized, "latest") ||
		hasPhrase(ctx.normalized, "recent") ||
		hasPhrase(ctx.normalized, "newest")
	);
}

export function wantsOldest(ctx: RouteContext): boolean {
	return hasPhrase(ctx.normalized, "oldest");
}

export function wantsDistribution(ctx: RouteContext): boolean {
	return (
		hasPhrase(ctx.normalized, "breakdown") ||
		hasPhrase(ctx.normalized, "distribution") ||
		hasPhrase(ctx.normalized, "group by") ||
		hasPhrase(ctx.normalized, "count by") ||
		hasPhrase(ctx.normalized, "most common")
	);
}

export function wantsRank(ctx: RouteContext): boolean {
	return (
		/\btop\b/.test(ctx.normalized) ||
		/\bbottom\b/.test(ctx.normalized) ||
		wantsTop(ctx) ||
		wantsBottom(ctx)
	);
}

function wantsNumberedRank(ctx: RouteContext): boolean {
	return /\b(?:top|bottom)\s+\d{1,3}\b/.test(ctx.normalized);
}

export function aggregateForRank(ctx: RouteContext): AggregateFunction {
	if (wantsNumberedRank(ctx)) return "sum";
	return wantsBottom(ctx) ? "min" : "max";
}

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

export function shouldUseRankedList(
	ctx: RouteContext,
	target: SheetColumn | undefined,
	measure: SheetColumn | undefined,
): boolean {
	if (!target || !measure || !isListIntent(ctx) || !wantsRank(ctx))
		return false;
	const explicitlyListLike =
		hasPhrase(ctx.normalized, "show") ||
		hasPhrase(ctx.normalized, "list") ||
		hasPhrase(ctx.normalized, "find");
	if (!explicitlyListLike) return false;
	const uniqueRatio =
		target.nonEmptyCount === 0 ? 0 : target.uniqueCount / target.nonEmptyCount;
	return (
		target.uniqueCount > 30 ||
		uniqueRatio >= 0.7 ||
		isIdentifierishColumn(target)
	);
}

export function wantsDistinct(ctx: RouteContext): boolean {
	return (
		hasPhrase(ctx.normalized, "unique") ||
		hasPhrase(ctx.normalized, "distinct") ||
		hasPhrase(ctx.normalized, "different")
	);
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
