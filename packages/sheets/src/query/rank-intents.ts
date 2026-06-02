import { hasPhrase } from "../normalize.js";
import type { SheetColumn } from "../types.js";
import { isIdentifierishColumn } from "./columns.js";
import type { RouteContext } from "./context.js";
import { wantsRank } from "./intent-basics.js";
import { isListIntent } from "./list-intents.js";

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
