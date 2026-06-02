import type { RouteContext } from "./context.js";
import { wantsBottom, wantsTop } from "./intent-basics.js";

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
