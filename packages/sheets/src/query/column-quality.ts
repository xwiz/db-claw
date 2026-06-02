import { normalizeText } from "../normalize.js";
import type { SheetColumn, SheetDataset } from "../types.js";

const LINE_NUMBER_LABELS = new Set([
	"item",
	"line",
	"line number",
	"line no",
	"no",
	"number",
]);

const IDENTIFIER_LABEL =
	/\b(id|uuid|guid|message id|server id|url|uri|website|email|phone|password|token|secret|key)\b/;
const SENSITIVE_LABEL =
	/\b(password|token|secret|access key|api key|private key)\b/;
const LONG_TEXT_LABEL =
	/\b(description|reasoning|content|body|message|notes?|comment|email to send)\b/;
const PREFERRED_DISPLAY_LABEL =
	/\b(applicant|author|company|customer|client|account|contact|name|title|subject|component|product|part|campaign|country|region|market|team|owner|rep)\b/;

interface ColumnQualityOptions {
	allowIdentifiers?: boolean;
	allowLongText?: boolean;
}

export function columnById(
	dataset: SheetDataset,
	id: string,
): SheetColumn | undefined {
	return dataset.columns.find((column) => column.id === id);
}

function labelText(column: SheetColumn): string {
	return normalizeText(`${column.label} ${column.id.replace(/_/g, " ")}`);
}

function coverage(dataset: SheetDataset, column: SheetColumn): number {
	if (dataset.rowCount === 0) return 0;
	return column.nonEmptyCount / dataset.rowCount;
}

export function isSensitiveColumn(column: SheetColumn): boolean {
	return SENSITIVE_LABEL.test(labelText(column));
}

export function isLikelyLineNumberColumn(column: SheetColumn): boolean {
	const label = normalizeText(column.label);
	return (
		LINE_NUMBER_LABELS.has(label) &&
		column.examples.length > 0 &&
		column.examples.every((example) => /^\d+$/.test(example.trim()))
	);
}

export function isIdentifierishColumn(column: SheetColumn): boolean {
	const label = labelText(column);
	return (
		isLikelyLineNumberColumn(column) ||
		IDENTIFIER_LABEL.test(label) ||
		(column.nonEmptyCount > 20 &&
			column.uniqueCount === column.nonEmptyCount &&
			/\b(number|code)\b/.test(label))
	);
}

export function isLongTextColumn(column: SheetColumn): boolean {
	const averageExampleLength =
		column.examples.length === 0
			? 0
			: column.examples.reduce((sum, value) => sum + value.length, 0) /
				column.examples.length;
	return LONG_TEXT_LABEL.test(labelText(column)) || averageExampleLength > 140;
}

export function isUsableDisplayColumn(
	dataset: SheetDataset,
	column: SheetColumn,
	options: ColumnQualityOptions = {},
): boolean {
	if (
		!(column.roles.includes("dimension") || column.roles.includes("display")) ||
		column.kind === "date" ||
		column.uniqueCount <= 1 ||
		column.nonEmptyCount === 0 ||
		isSensitiveColumn(column)
	) {
		return false;
	}
	if (!options.allowIdentifiers && isIdentifierishColumn(column)) return false;
	if (!options.allowLongText && isLongTextColumn(column)) return false;
	return coverage(dataset, column) >= 0.35;
}

export function displayQualityScore(
	dataset: SheetDataset,
	column: SheetColumn,
): number {
	if (!isUsableDisplayColumn(dataset, column)) return Number.NEGATIVE_INFINITY;
	const label = labelText(column);
	let score = 0;
	if (PREFERRED_DISPLAY_LABEL.test(label)) score += 25;
	if (column.roles.includes("dimension")) score += 8;
	if (column.roles.includes("display")) score += 4;
	score += Math.min(12, coverage(dataset, column) * 12);
	if (column.uniqueCount < column.nonEmptyCount) score += 4;
	if (column.uniqueCount <= 30) score += 3;
	if (isIdentifierishColumn(column)) score -= 30;
	if (isLongTextColumn(column)) score -= 20;
	return score;
}

export function bestDisplayColumn(
	dataset: SheetDataset,
	predicate: (column: SheetColumn) => boolean = () => true,
): SheetColumn | undefined {
	let best: { column: SheetColumn; score: number } | undefined;
	for (const column of dataset.columns) {
		if (!predicate(column)) continue;
		const score = displayQualityScore(dataset, column);
		if (!Number.isFinite(score)) continue;
		if (!best || score > best.score) best = { column, score };
	}
	if (best) return best.column;

	return dataset.columns.find((column) =>
		isUsableDisplayColumn(dataset, column, {
			allowIdentifiers: true,
			allowLongText: false,
		}),
	);
}
