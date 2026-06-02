import { hasPhrase } from "../normalize.js";
import type { AggregateFunction } from "../types.js";
import type { RouteContext } from "./context.js";
import { wantsBottom } from "./intent-basics.js";

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
	if (hasPhrase(ctx.normalized, "total") || hasPhrase(ctx.normalized, "sum")) {
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

function wantsNumberedRank(ctx: RouteContext): boolean {
	return /\b(?:top|bottom)\s+\d{1,3}\b/.test(ctx.normalized);
}

export function aggregateForRank(ctx: RouteContext): AggregateFunction {
	if (wantsNumberedRank(ctx)) return "sum";
	return wantsBottom(ctx) ? "min" : "max";
}
