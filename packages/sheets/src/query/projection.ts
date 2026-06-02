import { hasPhrase } from "../normalize.js";
import type { SheetDataset } from "../types.js";
import {
	displayQualityScore,
	isIdentifierishColumn,
	isSensitiveColumn,
	scoreColumn,
} from "./columns.js";
import type { RouteContext } from "./context.js";

export function defaultProjection(dataset: SheetDataset): string[] {
	const leadingIdentifier = dataset.columns.find(
		(column) =>
			column.roles.includes("display") &&
			column.nonEmptyCount > 0 &&
			!isSensitiveColumn(column) &&
			isIdentifierishColumn(column),
	);
	const display = dataset.columns
		.map((column, index) => ({ column, index }))
		.filter(({ column }) => column.roles.includes("display"))
		.filter(({ column }) => column.nonEmptyCount > 0)
		.filter(({ column }) => !isSensitiveColumn(column))
		.filter(({ column }) => column.id !== leadingIdentifier?.id)
		.sort((a, b) => {
			const delta =
				displayQualityScore(dataset, b.column) -
				displayQualityScore(dataset, a.column);
			return delta === 0 ? a.index - b.index : delta;
		})
		.slice(0, leadingIdentifier ? 3 : 4)
		.map(({ column }) => column.id);
	if (leadingIdentifier) return [leadingIdentifier.id, ...display];
	if (display.length > 0) return display;
	return dataset.columns
		.filter((column) => !isSensitiveColumn(column))
		.slice(0, 4)
		.map((column) => column.id);
}

export function explicitProjection(
	dataset: SheetDataset,
	ctx: RouteContext,
): string[] {
	const matches = dataset.columns
		.filter((column) => scoreColumn(ctx, column) > 0)
		.filter((column) => !isSensitiveColumn(column))
		.filter(
			(column) =>
				!column.roles.includes("measure") ||
				hasPhrase(ctx.normalized, column.label),
		)
		.map((column) => column.id);
	return matches.length > 0 ? matches.slice(0, 6) : defaultProjection(dataset);
}

export function withProjectionColumns(
	projection: string[],
	columns: Array<string | undefined>,
	limit = 6,
): string[] {
	const out = [...projection];
	for (const column of columns) {
		if (!column || out.includes(column)) continue;
		out.push(column);
	}
	return out.slice(0, limit);
}
