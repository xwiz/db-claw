import type { SheetColumn, SheetDataset } from "./types.js";

const IDENTIFIER_LABEL =
	/\b(id|uuid|guid|message id|server id|url|uri|website|email|phone|password|token|secret|key)\b/;
const SENSITIVE_LABEL =
	/\b(password|token|secret|access key|api key|private key)\b/;
const LONG_TEXT_LABEL =
	/\b(description|reasoning|content|body|message|notes?|comment|email to send)\b/;
const PREFERRED_DISPLAY_LABEL =
	/\b(applicant|author|company|customer|client|account|contact|name|title|subject|component|product|part|campaign|channel|source|status|category|type|country|region|market|team|owner|rep|from)\b/;
const PREFERRED_MEASURE_LABEL =
	/\b(revenue|sales|amount|quantity|qty|cost|price|spend|budget|balance|likes?|retweets?|views?|requests?|roses?|held|count|score|total)\b/;
const LOW_PRIORITY_MEASURE_LABEL = /\b(age|year|month|day)\b/;

const MONTH_NAMES = [
	"January",
	"February",
	"March",
	"April",
	"May",
	"June",
	"July",
	"August",
	"September",
	"October",
	"November",
	"December",
];

function questionLabel(column: SheetColumn): string {
	return column.label.trim().toLowerCase();
}

function pluralize(label: string): string {
	const trimmed = label.trim().toLowerCase();
	if (trimmed.includes(" ") || trimmed.endsWith("s")) return trimmed;
	if (trimmed.endsWith("y")) return `${trimmed.slice(0, -1)}ies`;
	return `${trimmed}s`;
}

function isMeasure(column: SheetColumn): boolean {
	return column.roles.includes("measure");
}

function labelText(column: SheetColumn): string {
	return `${column.label} ${column.id.replace(/_/g, " ")}`.toLowerCase();
}

function coverage(dataset: SheetDataset, column: SheetColumn): number {
	if (dataset.rowCount === 0) return 0;
	return column.nonEmptyCount / dataset.rowCount;
}

function isGroupable(column: SheetColumn): boolean {
	return (
		column.kind !== "date" &&
		(column.roles.includes("dimension") || column.roles.includes("display"))
	);
}

function isLikelyLineNumberColumn(column: SheetColumn): boolean {
	const label = column.label.trim().toLowerCase();
	return (
		["item", "line", "line number", "line no", "no", "number"].includes(
			label,
		) &&
		column.examples.length > 0 &&
		column.examples.every((example) => /^\d+$/.test(example.trim()))
	);
}

function isSensitiveColumn(column: SheetColumn): boolean {
	return SENSITIVE_LABEL.test(labelText(column));
}

function isIdentifierishColumn(column: SheetColumn): boolean {
	const label = labelText(column);
	return (
		isLikelyLineNumberColumn(column) ||
		IDENTIFIER_LABEL.test(label) ||
		(column.nonEmptyCount > 20 &&
			column.uniqueCount === column.nonEmptyCount &&
			/\b(number|code)\b/.test(label))
	);
}

function isLongTextColumn(column: SheetColumn): boolean {
	const averageExampleLength =
		column.examples.length === 0
			? 0
			: column.examples.reduce((sum, value) => sum + value.length, 0) /
				column.examples.length;
	return LONG_TEXT_LABEL.test(labelText(column)) || averageExampleLength > 140;
}

function displayScore(dataset: SheetDataset, column: SheetColumn): number {
	if (
		!isGroupable(column) ||
		column.uniqueCount <= 1 ||
		column.nonEmptyCount === 0 ||
		coverage(dataset, column) < 0.35 ||
		isSensitiveColumn(column) ||
		isIdentifierishColumn(column) ||
		isLongTextColumn(column)
	) {
		return Number.NEGATIVE_INFINITY;
	}
	let score = 0;
	const label = labelText(column);
	if (PREFERRED_DISPLAY_LABEL.test(label)) score += 30;
	if (column.roles.includes("dimension")) score += 10;
	if (column.uniqueCount < column.nonEmptyCount) score += 6;
	if (column.uniqueCount <= 30) score += 4;
	score += Math.min(12, coverage(dataset, column) * 12);
	return score;
}

function rowDisplayScore(dataset: SheetDataset, column: SheetColumn): number {
	const base = displayScore(dataset, column);
	if (!Number.isFinite(base)) return base;
	let score = base;
	const label = labelText(column);
	if (column.uniqueCount / Math.max(1, column.nonEmptyCount) >= 0.5) {
		score += 18;
	}
	if (
		/\b(campaign|customer|client|company|applicant|author|component|product|part|name|title|subject)\b/.test(
			label,
		)
	) {
		score += 10;
	}
	if (
		/\b(channel|source|status|category|type|region|country|team)\b/.test(label)
	) {
		score -= 8;
	}
	return score;
}

function measureScore(column: SheetColumn): number {
	if (!isMeasure(column) || isSensitiveColumn(column)) {
		return Number.NEGATIVE_INFINITY;
	}
	const label = labelText(column);
	let score = 10;
	if (PREFERRED_MEASURE_LABEL.test(label)) score += 20;
	if (LOW_PRIORITY_MEASURE_LABEL.test(label)) score -= 12;
	score += Math.min(8, column.uniqueCount);
	return score;
}

function bestMeasure(dataset: SheetDataset): SheetColumn | undefined {
	return dataset.columns
		.filter(isMeasure)
		.map((column, index) => ({ column, index, score: measureScore(column) }))
		.filter((entry) => Number.isFinite(entry.score))
		.sort((a, b) => {
			const delta = b.score - a.score;
			return delta === 0 ? a.index - b.index : delta;
		})[0]?.column;
}

function secondMeasure(
	dataset: SheetDataset,
	first: SheetColumn | undefined,
): SheetColumn | undefined {
	return dataset.columns
		.filter((column) => isMeasure(column) && column.id !== first?.id)
		.map((column, index) => ({ column, index, score: measureScore(column) }))
		.filter((entry) => Number.isFinite(entry.score))
		.sort((a, b) => {
			const delta = b.score - a.score;
			return delta === 0 ? a.index - b.index : delta;
		})[0]?.column;
}

function displayDimension(dataset: SheetDataset): SheetColumn | undefined {
	return (
		dataset.columns
			.map((column, index) => ({
				column,
				index,
				score: rowDisplayScore(dataset, column),
			}))
			.filter((entry) => Number.isFinite(entry.score))
			.sort((a, b) => {
				const delta = b.score - a.score;
				return delta === 0 ? a.index - b.index : delta;
			})[0]?.column ?? dataset.columns.find(isGroupable)
	);
}

function categoryDimension(dataset: SheetDataset): SheetColumn | undefined {
	return (
		dataset.columns
			.map((column, index) => ({
				column,
				index,
				score:
					column.roles.includes("dimension") && column.uniqueCount <= 30
						? displayScore(dataset, column) +
							18 +
							(column.uniqueCount < column.nonEmptyCount ? 12 : -8)
						: displayScore(dataset, column),
			}))
			.filter((entry) => Number.isFinite(entry.score))
			.sort((a, b) => {
				const delta = b.score - a.score;
				return delta === 0 ? a.index - b.index : delta;
			})[0]?.column ?? dataset.columns.find(isGroupable)
	);
}

function firstMonth(dataset: SheetDataset): string | undefined {
	const dateColumn = dataset.columns.find((column) => column.kind === "date");
	if (!dateColumn) return undefined;
	for (const row of dataset.rows) {
		const value = row.cells[dateColumn.id]?.value;
		if (value instanceof Date) return MONTH_NAMES[value.getMonth()];
	}
	return undefined;
}

function statusValue(dataset: SheetDataset): string | undefined {
	const column =
		dataset.columns.find(
			(candidate) =>
				isGroupable(candidate) &&
				/status|state|priority|category|type/i.test(candidate.label),
		) ??
		dataset.columns.find(
			(candidate) =>
				isGroupable(candidate) &&
				candidate.uniqueCount > 1 &&
				candidate.uniqueCount <= 8,
		);
	return column?.examples[0];
}

function pushUnique(out: string[], question: string | undefined): void {
	if (!question) return;
	if (!out.includes(question)) out.push(question);
}

export function suggestSheetQuestions(
	dataset: SheetDataset,
	limit = 6,
): string[] {
	const out: string[] = [];
	const measure = bestMeasure(dataset);
	const nextMeasure = secondMeasure(dataset, measure);
	const group = categoryDimension(dataset);
	const display = displayDimension(dataset);
	const month = firstMonth(dataset);
	const value = statusValue(dataset);

	if (measure && group) {
		pushUnique(
			out,
			`total ${questionLabel(measure)} by ${questionLabel(group)}`,
		);
	}
	if (measure && display) {
		pushUnique(
			out,
			`top 5 ${pluralize(display.label)} by ${questionLabel(measure)}`,
		);
	}
	if (nextMeasure && group) {
		pushUnique(
			out,
			`average ${questionLabel(nextMeasure)} by ${questionLabel(group)}`,
		);
	}
	if (measure && month) {
		pushUnique(out, `average ${questionLabel(measure)} in ${month}`);
	}
	if (value && display) {
		pushUnique(out, `show ${value.toLowerCase()} ${pluralize(display.label)}`);
	}
	if (value && display) {
		pushUnique(
			out,
			`how many ${value.toLowerCase()} ${pluralize(display.label)}?`,
		);
	}
	if (display) {
		pushUnique(out, `show first 5 ${pluralize(display.label)}`);
	}
	pushUnique(out, "how many rows are there?");

	return out.slice(0, limit);
}
