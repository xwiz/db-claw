import { hasPhrase, normalizeText, singularize } from "../normalize.js";
import type { SheetColumn, SheetDataset } from "../types.js";
import {
	bestDisplayColumn,
	displayQualityScore,
	isIdentifierishColumn,
	isUsableDisplayColumn,
} from "./column-quality.js";
import { scoreColumn } from "./column-scoring.js";
import { findBestColumn } from "./column-search.js";
import { type RouteContext, contextForPhrase } from "./context.js";

const GROUP_BY_STOP_WORDS = new Set([
	"for",
	"where",
	"with",
	"among",
	"excluding",
	"except",
]);

function explicitGroupPhrase(ctx: RouteContext): string | undefined {
	const byMatch = ctx.normalized.match(/\b(?:by|per|for each)\s+(.+)$/);
	if (!byMatch?.[1]) return undefined;
	const terms = byMatch[1].split(/\s+/);
	const stopIndex = terms.findIndex((term) => GROUP_BY_STOP_WORDS.has(term));
	const phrase = (stopIndex >= 0 ? terms.slice(0, stopIndex) : terms).join(" ");
	return phrase.length > 0 ? phrase : undefined;
}

function explicitGroupScores(
	column: SheetColumn,
	ctx: RouteContext,
): { rank: number; lexical: number } {
	const lexical = scoreColumn(ctx, column);
	if (lexical === 0) return { rank: 0, lexical: 0 };
	const label = normalizeText(column.label);
	const singularLabel = singularize(label);
	const singularPhrase = singularize(ctx.normalized);
	const head = ctx.singularQuestionWords.at(-1);
	const direct =
		hasPhrase(ctx.normalized, label) || singularLabel === singularPhrase;
	const headMatch = head !== undefined && singularLabel === head;
	const rank =
		lexical +
		(column.roles.includes("dimension") ? 12 : 0) +
		(column.roles.includes("display") ? 3 : 0) +
		(direct ? 30 : 0) +
		(headMatch ? 24 : 0);
	return { rank, lexical: lexical + (direct ? 12 : 0) };
}

export function findExplicitGroupByColumn(
	dataset: SheetDataset,
	ctx: RouteContext,
): SheetColumn | undefined {
	const phrase = explicitGroupPhrase(ctx);
	if (!phrase) return undefined;
	const byCtx = contextForPhrase(phrase);
	let bestGroup: { column: SheetColumn; score: number } | undefined;
	let bestGroupLexical = 0;
	for (const column of dataset.columns) {
		if (
			!column.roles.includes("dimension") &&
			!column.roles.includes("display")
		) {
			continue;
		}
		const scores = explicitGroupScores(column, byCtx);
		if (scores.rank === 0) continue;
		if (!bestGroup || scores.rank > bestGroup.score) {
			bestGroup = { column, score: scores.rank };
			bestGroupLexical = scores.lexical;
		}
	}
	if (!bestGroup) return undefined;
	const byMeasure = findBestColumn(dataset, byCtx, (column) =>
		column.roles.includes("measure"),
	);
	const measureScore = byMeasure ? scoreColumn(byCtx, byMeasure) : 0;
	return bestGroupLexical >= measureScore ? bestGroup.column : undefined;
}

export function findImplicitRankGroupColumn(
	dataset: SheetDataset,
	ctx: RouteContext,
): SheetColumn | undefined {
	let best: { column: SheetColumn; score: number } | undefined;
	for (const column of dataset.columns) {
		if (!isUsableDisplayColumn(dataset, column, { allowIdentifiers: true })) {
			continue;
		}
		const lexical = scoreColumn(ctx, column);
		if (lexical === 0) continue;
		const quality = Math.max(0, displayQualityScore(dataset, column));
		const score = lexical * 4 + quality;
		if (!best || score > best.score) best = { column, score };
	}
	if (best) return best.column;

	return bestDisplayColumn(dataset);
}

export function findRankTargetGroupColumn(
	dataset: SheetDataset,
	ctx: RouteContext,
): SheetColumn | undefined {
	const target =
		ctx.normalized.match(
			/\b(?:top|bottom)\s+(?:\d{1,3}\s+)?(.+?)\s+by\b/,
		)?.[1] ??
		ctx.normalized.match(
			/\bwhich\s+(.+?)\s+(?:has|had|have|needs?|needed)\b/,
		)?.[1];
	if (!target) return undefined;
	const targetCtx = contextForPhrase(target);
	const choose = (allowIdentifiers: boolean) => {
		let best: { column: SheetColumn; score: number } | undefined;
		for (const column of dataset.columns) {
			if (!isUsableDisplayColumn(dataset, column, { allowIdentifiers })) {
				continue;
			}
			if (!allowIdentifiers && isIdentifierishColumn(column)) continue;
			const lexical = scoreColumn(targetCtx, column);
			if (lexical === 0) continue;
			const quality = Math.max(0, displayQualityScore(dataset, column));
			const columnLabel = normalizeText(column.label);
			const targetLabel = targetCtx.normalized;
			const direct =
				hasPhrase(columnLabel, targetLabel) ||
				hasPhrase(targetLabel, columnLabel) ||
				singularize(columnLabel) === singularize(targetLabel);
			const score = lexical * 4 + quality + (direct ? 80 : 0);
			if (!best || score > best.score) best = { column, score };
		}
		return best?.column;
	};
	return choose(false) ?? choose(true);
}
