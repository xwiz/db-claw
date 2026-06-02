import { hasPhrase, normalizeText } from "../normalize.js";
import type { SheetColumn, SheetDataset, SheetFilter } from "../types.js";
import {
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
