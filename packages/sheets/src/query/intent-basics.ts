import { hasPhrase } from "../normalize.js";
import type { RouteContext } from "./context.js";

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
		hasPhrase(ctx.normalized, "break down") ||
		hasPhrase(ctx.normalized, "distribution") ||
		hasPhrase(ctx.normalized, "group by") ||
		hasPhrase(ctx.normalized, "count by") ||
		hasPhrase(ctx.normalized, "most common")
	);
}

export function wantsComparison(ctx: RouteContext): boolean {
	return (
		hasPhrase(ctx.normalized, "compare") ||
		hasPhrase(ctx.normalized, "comparison") ||
		hasPhrase(ctx.normalized, "breakdown") ||
		hasPhrase(ctx.normalized, "break down") ||
		hasPhrase(ctx.normalized, "distribution")
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

export function wantsDistinct(ctx: RouteContext): boolean {
	return (
		hasPhrase(ctx.normalized, "unique") ||
		hasPhrase(ctx.normalized, "distinct") ||
		hasPhrase(ctx.normalized, "different")
	);
}
