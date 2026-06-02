import { hasPhrase, normalizeText } from "../normalize.js";
import type { SheetColumn, SheetDataset } from "../types.js";
import { isIdentifierishColumn, isSensitiveColumn } from "./column-quality.js";
import { aliasesFor, scoreColumn } from "./column-scoring.js";
import { type RouteContext, contextForPhrase } from "./context.js";

export function findBestColumn(
	dataset: SheetDataset,
	ctx: RouteContext,
	predicate: (column: SheetColumn) => boolean,
): SheetColumn | undefined {
	let best: { column: SheetColumn; score: number } | undefined;
	for (const column of dataset.columns) {
		if (!predicate(column)) continue;
		const score = scoreColumn(ctx, column);
		if (score === 0) continue;
		if (!best || score > best.score) best = { column, score };
	}
	return best?.column;
}

export function findMeasureColumn(
	dataset: SheetDataset,
	ctx: RouteContext,
): SheetColumn | undefined {
	const exact = dataset.columns.find((column) => {
		if (!column.roles.includes("measure")) return false;
		const label = normalizeText(column.label);
		const id = normalizeText(column.id.replace(/_/g, " "));
		return hasPhrase(ctx.normalized, label) || hasPhrase(ctx.normalized, id);
	});
	if (exact) return exact;

	const explicit = findBestColumn(dataset, ctx, (column) =>
		column.roles.includes("measure"),
	);
	if (explicit) return explicit;

	const measureColumns = dataset.columns.filter((column) =>
		column.roles.includes("measure"),
	);
	if (measureColumns.length === 1) return measureColumns[0];

	if (
		hasPhrase(ctx.normalized, "revenue") ||
		hasPhrase(ctx.normalized, "sales")
	) {
		return measureColumns.find((column) =>
			aliasesFor(column).some((alias) =>
				["revenue", "sales", "amount"].includes(alias),
			),
		);
	}
	return undefined;
}

export function findDateColumn(
	dataset: SheetDataset,
	ctx: RouteContext,
): SheetColumn | undefined {
	return (
		findBestColumn(dataset, ctx, (column) => column.kind === "date") ??
		dataset.columns.find((column) => column.kind === "date")
	);
}

export function findMentionedColumn(
	dataset: SheetDataset,
	phrase: string,
	predicate: (column: SheetColumn) => boolean = () => true,
): SheetColumn | undefined {
	const phraseCtx = contextForPhrase(phrase);
	return findBestColumn(
		dataset,
		phraseCtx,
		(column) => !isSensitiveColumn(column) && predicate(column),
	);
}

export function findColumnMention(
	dataset: SheetDataset,
	ctx: RouteContext,
): SheetColumn | undefined {
	let best: { column: SheetColumn; score: number } | undefined;
	for (const column of dataset.columns) {
		if (isSensitiveColumn(column) || column.nonEmptyCount === 0) continue;
		const score = scoreColumn(ctx, column);
		if (score === 0) continue;
		const adjusted =
			score +
			(column.roles.includes("dimension") || column.roles.includes("display")
				? 8
				: 0) -
			(isIdentifierishColumn(column) ? 12 : 0);
		if (!best || adjusted > best.score) best = { column, score: adjusted };
	}
	return best?.column;
}
