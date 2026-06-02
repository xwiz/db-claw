import type { SheetColumn, SheetDataset } from "./types.js";

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

function isGroupable(column: SheetColumn): boolean {
	return (
		column.kind !== "date" &&
		(column.roles.includes("dimension") || column.roles.includes("display"))
	);
}

function displayDimension(dataset: SheetDataset): SheetColumn | undefined {
	return (
		dataset.columns.find(
			(column) =>
				isGroupable(column) &&
				column.roles.includes("display") &&
				column.uniqueCount > 1,
		) ?? dataset.columns.find(isGroupable)
	);
}

function categoryDimension(dataset: SheetDataset): SheetColumn | undefined {
	return (
		dataset.columns.find(
			(column) =>
				isGroupable(column) &&
				column.roles.includes("dimension") &&
				column.uniqueCount > 1 &&
				column.uniqueCount <= 20 &&
				(dataset.rowCount <= 2 || column.uniqueCount < dataset.rowCount),
		) ?? dataset.columns.find(isGroupable)
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
	const measure = dataset.columns.find(isMeasure);
	const secondMeasure = dataset.columns.filter(isMeasure)[1];
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
	if (secondMeasure && group) {
		pushUnique(
			out,
			`average ${questionLabel(secondMeasure)} by ${questionLabel(group)}`,
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

	return out.slice(0, limit);
}
