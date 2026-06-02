import { normalizeText } from "../normalize.js";
import type { SheetColumn, SheetDataset, SheetFilter } from "../types.js";
import { findMentionedColumn } from "./columns.js";
import type { RouteContext } from "./context.js";

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

export function findPresenceFilters(
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
