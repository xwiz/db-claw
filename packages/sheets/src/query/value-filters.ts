import { hasPhrase, normalizeText } from "../normalize.js";
import type { SheetColumn, SheetDataset, SheetFilter } from "../types.js";
import {
	findMentionedColumn,
	isLikelyLineNumberColumn,
	isLongTextColumn,
	isSensitiveColumn,
} from "./columns.js";
import type { RouteContext } from "./context.js";
import { topLimit } from "./intents.js";
import { comparedNumberValues } from "./number-filters.js";

function uniqueRawValues(dataset: SheetDataset, column: SheetColumn): string[] {
	const seen = new Set<string>();
	const out: string[] = [];
	for (const row of dataset.rows) {
		const cell = row.cells[column.id];
		if (!cell || cell.normalized.length === 0 || seen.has(cell.normalized))
			continue;
		seen.add(cell.normalized);
		out.push(cell.raw);
	}
	return out;
}

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

function isNegativeValueMention(
	normalizedQuestion: string,
	normalizedValue: string,
): boolean {
	const escaped = normalizedValue.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
	const patterns = [
		new RegExp(
			`\\bnot\\s+(?:in\\s+|from\\s+|equal\\s+to\\s+|equals\\s+)?${escaped}\\b`,
		),
		new RegExp(`\\b(?:except|excluding|exclude)\\s+${escaped}\\b`),
		new RegExp(`\\b(?:not|without)\\s+.*\\b${escaped}\\b`),
	];
	return patterns.some((pattern) => pattern.test(normalizedQuestion));
}

export function findEqualityFilters(
	dataset: SheetDataset,
	ctx: RouteContext,
): SheetFilter[] {
	const filters: SheetFilter[] = [];
	const filteredColumns = new Set<string>();
	const requestedLimit = topLimit(ctx);
	const comparedNumbers = comparedNumberValues(ctx);
	for (const column of dataset.columns) {
		const filterable =
			column.roles.includes("dimension") ||
			(column.roles.includes("display") &&
				column.uniqueCount <= 1000 &&
				!isLongTextColumn(column));
		if (
			!filterable ||
			isSensitiveColumn(column) ||
			isLikelyLineNumberColumn(column)
		) {
			continue;
		}
		for (const raw of uniqueRawValues(dataset, column)) {
			const normalized = normalizeText(raw);
			if (normalized.length === 0) continue;
			if (normalized.length > 80) continue;
			if (
				requestedLimit !== undefined &&
				/^\d+$/.test(normalized) &&
				Number(normalized) === requestedLimit
			) {
				continue;
			}
			if (
				/^-?\d+(?:\.\d+)?$/.test(normalized) &&
				comparedNumbers.some((value) => Number(normalized) === value)
			) {
				continue;
			}
			if (!hasPhrase(ctx.normalized, normalized)) continue;
			if (filteredColumns.has(column.id)) continue;
			filters.push({
				kind: isNegativeValueMention(ctx.normalized, normalized)
					? "notEquals"
					: "equals",
				column: column.id,
				value: raw,
			});
			filteredColumns.add(column.id);
		}
	}
	return filters;
}
