import { dedupeIds, normalizeText, slugId } from "./normalize.js";
import type {
	CellValue,
	ColumnKind,
	ColumnRole,
	CsvData,
	SheetColumn,
	SheetDataset,
	SheetPrimitive,
	SheetRow,
} from "./types.js";

const IDISH = /\b(id|uuid|guid|code|zip|postal|phone|account number)\b/;
const DIMENSIONISH =
	/\b(customer|client|account|company|campaign|project|name|title|region|market|territory|status|category|type|product|component|part|item|channel|source|warehouse|location|rep|owner|team|department|country|city)\b/;
const BOOLEAN_LABEL = /^(is|has|can|should|enabled|disabled)\b/;
const LINE_NUMBER_LABEL = /^(item|line|line number|line no|no|number|#)$/;
const QUANTITY_LABEL = /\b(quantity|qty|count|unit|units|amount)\b/;
const SPEC_NUMBER_WORDS =
	/\b(kv|v|dc|ac|amp|amps|current|input|output|voltage|rated|watt|watts|hz)\b/;
const ISO_DATE_VALUE =
	/^\d{4}-\d{1,2}-\d{1,2}(?:[t\s]\d{1,2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?(?:z|[+-]\d{2}:?\d{2})?)?$/i;
const SLASH_DATE_VALUE = /^\d{1,2}\/\d{1,2}\/\d{2,4}$/;
const MONTH_DATE_VALUE =
	/^(?:jan|january|feb|february|mar|march|apr|april|may|jun|june|jul|july|aug|august|sep|sept|september|oct|october|nov|november|dec|december)\s+\d{1,2},?\s+\d{4}$/i;

function valueAt(row: string[], idx: number): string {
	return row[idx]?.trim() ?? "";
}

function parseNumber(raw: string): number | null {
	const trimmed = raw.trim();
	if (trimmed.length === 0) return null;
	const negative = /^\(.*\)$/.test(trimmed);
	const cleaned = trimmed
		.replace(/^\(/, "")
		.replace(/\)$/, "")
		.replace(/[$£€,\s]/g, "")
		.replace(/%$/, "");
	if (!/^-?\d+(\.\d+)?$/.test(cleaned)) return null;
	const parsed = Number(cleaned);
	if (!Number.isFinite(parsed)) return null;
	return negative ? -parsed : parsed;
}

function parseSpreadsheetNumber(raw: string): number | null {
	const trimmed = raw.trim();
	if (trimmed.length === 0) return null;
	const fallback = parseNumber(raw);
	if (!/^\s*\(?[-$Â£â‚¬]?\d/.test(trimmed)) return fallback;
	const negative = /^\(.*\)$/.test(trimmed);
	const cleaned = trimmed
		.replace(/^\(/, "")
		.replace(/\)$/, "")
		.replace(/%$/, "")
		.replace(/,/g, "")
		.replace(/\s/g, "")
		.replace(/[^0-9.-]/g, "");
	if (!/^-?\d+(\.\d+)?$/.test(cleaned)) return fallback;
	const parsed = Number(cleaned);
	if (!Number.isFinite(parsed)) return fallback;
	return negative ? -parsed : parsed;
}

function parseQuantityNumber(raw: string): number | null {
	const trimmed = raw.trim();
	if (trimmed.length === 0) return null;
	const withoutParenthetical = trimmed.replace(/\([^)]*\)/g, "").trim();
	const normalized = normalizeText(withoutParenthetical);
	if (SPEC_NUMBER_WORDS.test(normalized)) return null;

	const range = withoutParenthetical.match(
		/^\s*(-?\d+(?:\.\d+)?)(?:\s*[-–]\s*(-?\d+(?:\.\d+)?))?/,
	);
	if (!range?.[1]) return parseSpreadsheetNumber(raw);
	const lower = Number(range[1]);
	const upper = range[2] ? Number(range[2]) : lower;
	if (!Number.isFinite(lower) || !Number.isFinite(upper)) return null;
	return Math.max(lower, upper);
}

function looksDateLike(raw: string): boolean {
	const trimmed = raw.trim();
	return (
		ISO_DATE_VALUE.test(trimmed) ||
		SLASH_DATE_VALUE.test(trimmed) ||
		MONTH_DATE_VALUE.test(trimmed)
	);
}

function parseDate(raw: string): Date | null {
	if (!looksDateLike(raw)) return null;
	const parsed = new Date(raw);
	if (Number.isNaN(parsed.getTime())) return null;
	return parsed;
}

function parseBoolean(raw: string): boolean | null {
	const normalized = normalizeText(raw);
	if (["true", "yes", "y"].includes(normalized)) return true;
	if (["false", "no", "n"].includes(normalized)) return false;
	return null;
}

function unique(values: string[]): string[] {
	const out: string[] = [];
	const seen = new Set<string>();
	for (const value of values) {
		const normalized = normalizeText(value);
		if (normalized.length === 0 || seen.has(normalized)) continue;
		seen.add(normalized);
		out.push(value);
	}
	return out;
}

function inferKind(
	label: string,
	values: string[],
): { kind: ColumnKind; confidence: number } {
	const nonEmpty = values.filter((value) => value.trim().length > 0);
	if (nonEmpty.length === 0) return { kind: "text", confidence: 0 };

	const normalizedLabel = normalizeText(label);
	const protectedText =
		IDISH.test(normalizedLabel) || LINE_NUMBER_LABEL.test(normalizedLabel);
	const dateCount = nonEmpty.filter(
		(value) => parseDate(value) !== null,
	).length;
	const booleanCount = nonEmpty.filter(
		(value) => parseBoolean(value) !== null,
	).length;
	const spreadsheetNumberCount = nonEmpty.filter(
		(value) => parseSpreadsheetNumber(value) !== null,
	).length;
	const quantityNumberCount = nonEmpty.filter(
		(value) => parseQuantityNumber(value) !== null,
	).length;

	if (!protectedText && dateCount / nonEmpty.length >= 0.7) {
		return { kind: "date", confidence: dateCount / nonEmpty.length };
	}
	if (
		QUANTITY_LABEL.test(normalizedLabel) &&
		quantityNumberCount / nonEmpty.length >= 0.65
	) {
		return {
			kind: "number",
			confidence: quantityNumberCount / nonEmpty.length,
		};
	}
	if (!protectedText && spreadsheetNumberCount / nonEmpty.length >= 0.8) {
		return {
			kind: "number",
			confidence: spreadsheetNumberCount / nonEmpty.length,
		};
	}
	if (
		BOOLEAN_LABEL.test(normalizedLabel) &&
		booleanCount === nonEmpty.length &&
		unique(nonEmpty).length <= 2
	) {
		return { kind: "boolean", confidence: 1 };
	}
	return { kind: "text", confidence: 0.8 };
}

function rolesFor(
	label: string,
	kind: ColumnKind,
	values: string[],
): ColumnRole[] {
	const normalizedLabel = normalizeText(label);
	if (LINE_NUMBER_LABEL.test(normalizedLabel)) {
		return ["display"];
	}
	if (kind === "number" && !IDISH.test(normalizedLabel)) return ["measure"];
	if (kind === "date") return ["date", "dimension"];
	if (kind === "boolean") return ["dimension"];

	const uniqueCount = unique(values).length;
	const nonEmptyCount = values.filter(
		(value) => value.trim().length > 0,
	).length;
	const roles: ColumnRole[] = ["display"];
	if (
		DIMENSIONISH.test(normalizedLabel) ||
		uniqueCount <= 20 ||
		(nonEmptyCount > 0 && uniqueCount / nonEmptyCount <= 0.5)
	) {
		roles.unshift("dimension");
	}
	return roles;
}

function cellFor(kind: ColumnKind, label: string, raw: string): CellValue {
	const trimmed = raw.trim();
	const normalizedLabel = normalizeText(label);
	let value: SheetPrimitive = trimmed.length === 0 ? null : trimmed;
	if (trimmed.length > 0) {
		if (kind === "number") {
			value = QUANTITY_LABEL.test(normalizedLabel)
				? parseQuantityNumber(trimmed)
				: parseSpreadsheetNumber(trimmed);
		} else if (kind === "date") value = parseDate(trimmed);
		else if (kind === "boolean") value = parseBoolean(trimmed);
	}
	return {
		raw: trimmed,
		value,
		normalized: normalizeText(trimmed),
	};
}

export function buildSheetDataset(csv: CsvData): SheetDataset {
	const baseIds = csv.headers.map((header, idx) =>
		slugId(header, `column_${idx + 1}`),
	);
	const ids = dedupeIds(baseIds);
	const warnings: string[] = [];
	if (csv.headers.length === 0) {
		warnings.push("CSV has no header row.");
	}

	const columns: SheetColumn[] = csv.headers.map((label, idx) => {
		const values = csv.rows.map((row) => valueAt(row, idx));
		const nonEmpty = values.filter((value) => value.trim().length > 0);
		const inferred = inferKind(label, values);
		return {
			id: ids[idx]!,
			label,
			kind: inferred.kind,
			roles: rolesFor(label, inferred.kind, values),
			nonEmptyCount: nonEmpty.length,
			uniqueCount: unique(nonEmpty).length,
			examples: unique(nonEmpty).slice(0, 5),
			confidence: inferred.confidence,
		};
	});

	const rows: SheetRow[] = csv.rows.map((row, rowIdx) => {
		const cells: Record<string, CellValue> = {};
		for (const [colIdx, column] of columns.entries()) {
			cells[column.id] = cellFor(
				column.kind,
				column.label,
				valueAt(row, colIdx),
			);
		}
		return { index: rowIdx, cells };
	});

	return {
		columns,
		rows,
		rowCount: rows.length,
		warnings,
	};
}
