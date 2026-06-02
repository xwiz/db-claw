import { hasPhrase, normalizeText, singularize, words } from "./normalize.js";
import type {
	AggregateFunction,
	CellValue,
	ChartSeries,
	NumberOperator,
	QueryConfidence,
	SheetColumn,
	SheetDataset,
	SheetFilter,
	SheetOrder,
	SheetQueryFrame,
	SheetQueryResult,
	SheetRow,
} from "./types.js";

const MONTHS = [
	"january",
	"february",
	"march",
	"april",
	"may",
	"june",
	"july",
	"august",
	"september",
	"october",
	"november",
	"december",
];

const MEASURE_SYNONYMS: Record<string, string[]> = {
	revenue: ["revenue", "sales", "amount", "invoice amount", "order value"],
	sales: ["sales", "revenue", "amount", "invoice amount", "order value"],
	amount: ["amount", "invoice amount", "order value", "revenue", "sales"],
	value: ["value", "order value", "amount"],
	cost: ["cost", "price", "unit cost", "spend", "budget"],
	spend: ["spend", "cost", "budget", "amount"],
	units: ["units", "quantity", "qty", "stock", "inventory"],
	clicks: ["clicks", "click", "visits", "views", "impressions"],
};

const DIMENSION_SYNONYMS: Record<string, string[]> = {
	customer: [
		"customer",
		"customers",
		"client",
		"clients",
		"account",
		"accounts",
		"company",
	],
	account: [
		"account",
		"accounts",
		"customer",
		"customers",
		"client",
		"clients",
	],
	region: [
		"region",
		"regions",
		"market",
		"markets",
		"territory",
		"territories",
	],
	product: [
		"product",
		"products",
		"component",
		"components",
		"part",
		"parts",
		"item",
		"items",
	],
	campaign: ["campaign", "campaigns", "initiative", "initiatives"],
	channel: ["channel", "channels", "source", "sources"],
	status: ["status", "state"],
	rep: ["rep", "sales rep", "owner"],
	team: ["team", "teams", "department", "departments"],
	warehouse: ["warehouse", "warehouses", "location", "locations"],
};

const LINE_NUMBER_LABELS = new Set([
	"item",
	"line",
	"line number",
	"line no",
	"no",
	"number",
]);

interface RouteContext {
	normalized: string;
	questionWords: string[];
	singularQuestionWords: string[];
}

interface GroupAccumulator {
	label: string;
	count: number;
	sum: number;
	min: number;
	max: number;
	minRaw?: string;
	maxRaw?: string;
}

function columnById(
	dataset: SheetDataset,
	id: string,
): SheetColumn | undefined {
	return dataset.columns.find((column) => column.id === id);
}

function aliasesFor(column: SheetColumn): string[] {
	const label = normalizeText(column.label);
	const id = normalizeText(column.id.replace(/_/g, " "));
	const aliases = new Set([label, id, singularize(label), singularize(id)]);
	for (const [concept, terms] of Object.entries(DIMENSION_SYNONYMS)) {
		if (terms.some((term) => hasPhrase(label, term) || hasPhrase(id, term))) {
			aliases.add(concept);
			for (const term of terms) aliases.add(term);
		}
	}
	return [...aliases].filter((alias) => alias.length > 0);
}

function synonymScore(ctx: RouteContext, column: SheetColumn): number {
	const columnText = normalizeText(
		`${column.label} ${column.id.replace(/_/g, " ")}`,
	);
	let score = 0;
	if (column.roles.includes("measure")) {
		for (const [concept, terms] of Object.entries(MEASURE_SYNONYMS)) {
			const questionMatches =
				hasPhrase(ctx.normalized, concept) ||
				terms.some((term) => hasPhrase(ctx.normalized, term));
			if (!questionMatches) continue;
			for (const term of terms) {
				if (hasPhrase(columnText, term)) {
					if (term === concept) score += 8;
					else if (term === "revenue" || term === "sales") score += 6;
					else score += 2;
				}
			}
		}
	}
	return score;
}

function scoreColumn(ctx: RouteContext, column: SheetColumn): number {
	let score = synonymScore(ctx, column);
	for (const alias of aliasesFor(column)) {
		if (hasPhrase(ctx.normalized, alias)) score += alias.includes(" ") ? 12 : 8;
		const aliasWords = words(alias);
		if (
			aliasWords.length > 1 &&
			aliasWords.every(
				(word) =>
					ctx.questionWords.includes(word) ||
					ctx.singularQuestionWords.includes(word),
			)
		) {
			score += aliasWords.length * 2;
		}
		if (
			aliasWords.length === 1 &&
			ctx.singularQuestionWords.includes(aliasWords[0]!)
		) {
			score += 6;
		}
	}
	return score;
}

function findBestColumn(
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

function findMeasureColumn(
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

function findExplicitGroupByColumn(
	dataset: SheetDataset,
	ctx: RouteContext,
): SheetColumn | undefined {
	const byMatch = ctx.normalized.match(/\b(?:by|per|for each)\s+(.+)$/);
	if (byMatch?.[1]) {
		const byCtx = {
			normalized: normalizeText(byMatch[1]),
			questionWords: words(byMatch[1]),
			singularQuestionWords: words(byMatch[1]).map((word) => singularize(word)),
		};
		const byColumn = findBestColumn(
			dataset,
			byCtx,
			(column) =>
				column.roles.includes("dimension") || column.roles.includes("display"),
		);
		if (byColumn) return byColumn;
	}

	return undefined;
}

function findImplicitRankGroupColumn(
	dataset: SheetDataset,
	ctx: RouteContext,
): SheetColumn | undefined {
	const nonIdentifier = findBestColumn(dataset, ctx, (column) => {
		return (
			(column.roles.includes("dimension") ||
				column.roles.includes("display")) &&
			column.kind !== "date" &&
			!isLikelyLineNumberColumn(column)
		);
	});
	if (nonIdentifier) return nonIdentifier;
	return findBestColumn(dataset, ctx, (column) => {
		return (
			(column.roles.includes("dimension") ||
				column.roles.includes("display")) &&
			column.kind !== "date"
		);
	});
}

function aggregateIntent(ctx: RouteContext): AggregateFunction | undefined {
	if (
		hasPhrase(ctx.normalized, "how many") ||
		hasPhrase(ctx.normalized, "count")
	) {
		return "count";
	}
	if (
		hasPhrase(ctx.normalized, "average") ||
		hasPhrase(ctx.normalized, "avg") ||
		hasPhrase(ctx.normalized, "mean")
	) {
		return "avg";
	}
	if (
		hasPhrase(ctx.normalized, "total") ||
		hasPhrase(ctx.normalized, "sum") ||
		hasPhrase(ctx.normalized, "revenue")
	) {
		return "sum";
	}
	if (
		hasPhrase(ctx.normalized, "highest") ||
		hasPhrase(ctx.normalized, "maximum") ||
		hasPhrase(ctx.normalized, "max")
	) {
		return "max";
	}
	if (
		hasPhrase(ctx.normalized, "lowest") ||
		hasPhrase(ctx.normalized, "minimum") ||
		hasPhrase(ctx.normalized, "min")
	) {
		return "min";
	}
	return undefined;
}

function topLimit(ctx: RouteContext): number | undefined {
	const top = ctx.normalized.match(/\btop\s+(\d{1,3})\b/);
	if (top?.[1]) return Number(top[1]);
	const which = ctx.normalized.match(/\bwhich\s+(\d{1,3})\b/);
	if (which?.[1]) return Number(which[1]);
	if (wantsTop(ctx) || wantsBottom(ctx)) return 1;
	return undefined;
}

function wantsTop(ctx: RouteContext): boolean {
	return (
		hasPhrase(ctx.normalized, "most") ||
		hasPhrase(ctx.normalized, "highest") ||
		hasPhrase(ctx.normalized, "maximum") ||
		hasPhrase(ctx.normalized, "largest") ||
		hasPhrase(ctx.normalized, "greatest")
	);
}

function wantsBottom(ctx: RouteContext): boolean {
	return (
		hasPhrase(ctx.normalized, "bottom") ||
		hasPhrase(ctx.normalized, "lowest") ||
		hasPhrase(ctx.normalized, "least")
	);
}

function isLikelyLineNumberColumn(column: SheetColumn): boolean {
	const label = normalizeText(column.label);
	return (
		LINE_NUMBER_LABELS.has(label) &&
		column.examples.length > 0 &&
		column.examples.every((example) => /^\d+$/.test(example.trim()))
	);
}

function findMonthFilter(
	dataset: SheetDataset,
	ctx: RouteContext,
): SheetFilter | undefined {
	const monthIdx = MONTHS.findIndex((month) =>
		hasPhrase(ctx.normalized, month),
	);
	if (monthIdx < 0) return undefined;
	const dateColumn =
		findBestColumn(dataset, ctx, (column) => column.kind === "date") ??
		dataset.columns.find((column) => column.kind === "date");
	if (!dateColumn) return undefined;
	const yearMatch = ctx.normalized.match(/\b(20\d{2}|19\d{2})\b/);
	const filter: SheetFilter = {
		kind: "month",
		column: dateColumn.id,
		month: monthIdx + 1,
	};
	if (yearMatch?.[1]) filter.year = Number(yearMatch[1]);
	return filter;
}

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

function findEqualityFilters(
	dataset: SheetDataset,
	ctx: RouteContext,
): SheetFilter[] {
	const filters: SheetFilter[] = [];
	const filteredColumns = new Set<string>();
	for (const column of dataset.columns) {
		if (!column.roles.includes("dimension")) continue;
		for (const raw of uniqueRawValues(dataset, column)) {
			const normalized = normalizeText(raw);
			if (normalized.length === 0) continue;
			if (!hasPhrase(ctx.normalized, normalized)) continue;
			if (filteredColumns.has(column.id)) continue;
			filters.push({ kind: "equals", column: column.id, value: raw });
			filteredColumns.add(column.id);
		}
	}
	return filters;
}

function parseNumberComparison(ctx: RouteContext):
	| {
			operator: NumberOperator;
			value: number;
	  }
	| undefined {
	const match = ctx.normalized.match(
		/\b(over|greater than|above|more than|at least|under|less than|below|at most|equal to|equals)\s+(-?\d+(?:\.\d+)?)\b/,
	);
	if (!match?.[1] || !match[2]) return undefined;
	const phrase = match[1];
	const value = Number(match[2]);
	if (!Number.isFinite(value)) return undefined;
	if (["over", "greater than", "above", "more than"].includes(phrase)) {
		return { operator: "gt", value };
	}
	if (phrase === "at least") return { operator: "gte", value };
	if (["under", "less than", "below"].includes(phrase)) {
		return { operator: "lt", value };
	}
	if (phrase === "at most") return { operator: "lte", value };
	return { operator: "eq", value };
}

function findNumberFilter(
	dataset: SheetDataset,
	ctx: RouteContext,
): SheetFilter | undefined {
	const comparison = parseNumberComparison(ctx);
	if (!comparison) return undefined;
	const column = findMeasureColumn(dataset, ctx);
	if (!column) return undefined;
	return {
		kind: "number",
		column: column.id,
		operator: comparison.operator,
		value: comparison.value,
	};
}

function dedupeFilters(filters: SheetFilter[]): SheetFilter[] {
	const seen = new Set<string>();
	const out: SheetFilter[] = [];
	for (const filter of filters) {
		const key = JSON.stringify(filter);
		if (seen.has(key)) continue;
		seen.add(key);
		out.push(filter);
	}
	return out;
}

function defaultProjection(dataset: SheetDataset): string[] {
	const display = dataset.columns
		.filter((column) => column.roles.includes("display"))
		.slice(0, 4)
		.map((column) => column.id);
	if (display.length > 0) return display;
	return dataset.columns.slice(0, 4).map((column) => column.id);
}

function explicitProjection(
	dataset: SheetDataset,
	ctx: RouteContext,
): string[] {
	const matches = dataset.columns
		.filter((column) => scoreColumn(ctx, column) > 0)
		.filter(
			(column) =>
				!column.roles.includes("measure") ||
				hasPhrase(ctx.normalized, column.label),
		)
		.map((column) => column.id);
	return matches.length > 0 ? matches.slice(0, 6) : defaultProjection(dataset);
}

function confidenceLevel(score: number): QueryConfidence["level"] {
	if (score >= 0.8) return "high";
	if (score >= 0.55) return "medium";
	return "low";
}

function confidenceForFrame(
	frame: Omit<SheetQueryFrame, "confidence">,
): QueryConfidence {
	let score = 0.45;
	const reasons: string[] = [];

	if (frame.operation === "aggregate") {
		score += 0.15;
		reasons.push(`recognized ${frame.aggregate ?? "aggregate"} question`);
		if (frame.aggregate === "count") {
			score += 0.18;
			reasons.push("count does not need a measure column");
		} else if (frame.measureColumn) {
			score += 0.18;
			reasons.push(`matched measure column ${frame.measureColumn}`);
		}
		if (frame.groupByColumn) {
			score += 0.12;
			reasons.push(`matched group column ${frame.groupByColumn}`);
		}
		if (frame.routeReason === "missing_measure_column") {
			score = 0.2;
			reasons.push("no measure column matched the question");
		}
	} else {
		score += 0.12;
		reasons.push("recognized list/filter question");
		if (frame.projectionColumns.length > 0) {
			score += 0.08;
			reasons.push("selected display columns");
		}
	}

	if (frame.filters.length > 0) {
		score += Math.min(0.16, frame.filters.length * 0.08);
		reasons.push(`matched ${frame.filters.length} filter(s)`);
	}
	if (frame.limit !== undefined) {
		score += 0.04;
		reasons.push(`matched limit ${frame.limit}`);
	}
	if (frame.routeReason === "list_projection" && frame.filters.length === 0) {
		score -= 0.08;
		reasons.push("no filter matched; showing projected rows");
	}

	const bounded = Math.max(0, Math.min(0.98, score));
	return {
		score: Number(bounded.toFixed(2)),
		level: confidenceLevel(bounded),
		reasons,
	};
}

function withConfidence(
	frame: Omit<SheetQueryFrame, "confidence">,
): SheetQueryFrame {
	return {
		...frame,
		confidence: confidenceForFrame(frame),
	};
}

function buildFrame(
	dataset: SheetDataset,
	question: string,
): SheetQueryFrame | undefined {
	const ctx: RouteContext = {
		normalized: normalizeText(question),
		questionWords: words(question),
		singularQuestionWords: words(question).map((word) => singularize(word)),
	};
	if (ctx.normalized.length === 0) return undefined;

	const limit = topLimit(ctx);
	const topOrBottom = limit !== undefined || wantsBottom(ctx);
	const aggregate =
		aggregateIntent(ctx) ??
		(topOrBottom
			? wantsTop(ctx) || wantsBottom(ctx)
				? "max"
				: "sum"
			: undefined);
	const measure =
		aggregate && aggregate !== "count"
			? findMeasureColumn(dataset, ctx)
			: undefined;
	const explicitGroupBy = aggregate
		? findExplicitGroupByColumn(dataset, ctx)
		: undefined;
	const groupBy =
		explicitGroupBy ??
		(aggregate && topOrBottom
			? findImplicitRankGroupColumn(dataset, ctx)
			: undefined);
	const filters = dedupeFilters(
		[
			...findEqualityFilters(dataset, ctx),
			findMonthFilter(dataset, ctx),
			findNumberFilter(dataset, ctx),
		].filter((filter): filter is SheetFilter => filter !== undefined),
	);

	if (aggregate && aggregate !== "count" && !measure) {
		return withConfidence({
			question,
			operation: "aggregate",
			aggregate,
			projectionColumns: [],
			filters,
			resultShape: "unknown",
			routeReason: "missing_measure_column",
		});
	}

	if (aggregate) {
		const frame: Omit<SheetQueryFrame, "confidence"> = {
			question,
			operation: "aggregate",
			aggregate,
			projectionColumns: [],
			filters,
			resultShape: groupBy ? "categorical_chart" : "scalar_metric",
			routeReason: groupBy ? "aggregate_grouped" : "aggregate_scalar",
		};
		if (measure) frame.measureColumn = measure.id;
		if (groupBy) frame.groupByColumn = groupBy.id;
		if (limit !== undefined) frame.limit = limit;
		const order: SheetOrder = {
			column: measure?.id ?? groupBy?.id ?? "__count",
			direction: wantsBottom(ctx) ? "asc" : "desc",
		};
		if (topOrBottom || groupBy) frame.orderBy = order;
		return withConfidence(frame);
	}

	if (
		filters.length === 0 &&
		!hasPhrase(ctx.normalized, "show") &&
		!hasPhrase(ctx.normalized, "list") &&
		!hasPhrase(ctx.normalized, "find")
	) {
		return undefined;
	}

	const frame: Omit<SheetQueryFrame, "confidence"> = {
		question,
		operation: "list",
		projectionColumns: explicitProjection(dataset, ctx),
		filters,
		resultShape: "tabular",
		routeReason: filters.length > 0 ? "filtered_list" : "list_projection",
	};
	if (limit !== undefined) frame.limit = limit;
	return withConfidence(frame);
}

function matchesFilter(row: SheetRow, filter: SheetFilter): boolean {
	const cell = row.cells[filter.column];
	if (!cell) return false;
	if (filter.kind === "equals") {
		return cell.normalized === normalizeText(String(filter.value));
	}
	if (filter.kind === "number") {
		if (typeof cell.value !== "number") return false;
		if (filter.operator === "gt") return cell.value > filter.value;
		if (filter.operator === "gte") return cell.value >= filter.value;
		if (filter.operator === "lt") return cell.value < filter.value;
		if (filter.operator === "lte") return cell.value <= filter.value;
		return cell.value === filter.value;
	}
	if (!(cell.value instanceof Date)) return false;
	const month = cell.value.getMonth() + 1;
	const year = cell.value.getFullYear();
	return (
		month === filter.month &&
		(filter.year === undefined || year === filter.year)
	);
}

function filteredRows(
	dataset: SheetDataset,
	filters: SheetFilter[],
): SheetRow[] {
	return dataset.rows.filter((row) =>
		filters.every((filter) => matchesFilter(row, filter)),
	);
}

function numberValue(cell: CellValue | undefined): number | undefined {
	return typeof cell?.value === "number" ? cell.value : undefined;
}

function aggregateValues(
	rows: SheetRow[],
	aggregate: AggregateFunction,
	measureColumn: string | undefined,
): number {
	if (aggregate === "count") return rows.length;
	const values = rows
		.map((row) =>
			numberValue(measureColumn ? row.cells[measureColumn] : undefined),
		)
		.filter((value): value is number => value !== undefined);
	if (values.length === 0) return 0;
	if (aggregate === "sum") return values.reduce((sum, value) => sum + value, 0);
	if (aggregate === "avg") {
		return values.reduce((sum, value) => sum + value, 0) / values.length;
	}
	if (aggregate === "min") return Math.min(...values);
	return Math.max(...values);
}

function formatCell(
	cell: CellValue | undefined,
): string | number | boolean | null {
	if (!cell || cell.value === null) return null;
	if (cell.value instanceof Date) return cell.value.toISOString().slice(0, 10);
	return cell.value;
}

function labelFor(dataset: SheetDataset, columnId: string): string {
	return columnById(dataset, columnId)?.label ?? columnId;
}

function aggregateLabel(frame: SheetQueryFrame, dataset: SheetDataset): string {
	if (frame.aggregate === "count") return "Count";
	const measure = frame.measureColumn
		? labelFor(dataset, frame.measureColumn)
		: "Value";
	return `${frame.aggregate?.toUpperCase()} ${measure}`;
}

function groupedRows(
	rows: SheetRow[],
	frame: SheetQueryFrame,
	dataset: SheetDataset,
): {
	rows: Record<string, string | number | boolean | null>[];
	chart: ChartSeries;
} {
	const groupColumn = frame.groupByColumn!;
	const aggregate = frame.aggregate ?? "count";
	const groups = new Map<string, GroupAccumulator>();
	for (const row of rows) {
		const cell = row.cells[groupColumn];
		const label = cell?.raw && cell.raw.length > 0 ? cell.raw : "(blank)";
		const current = groups.get(label) ?? {
			label,
			count: 0,
			sum: 0,
			min: Number.POSITIVE_INFINITY,
			max: Number.NEGATIVE_INFINITY,
		};
		current.count += 1;
		const measureCell = frame.measureColumn
			? row.cells[frame.measureColumn]
			: undefined;
		const numeric = numberValue(measureCell);
		if (numeric !== undefined) {
			current.sum += numeric;
			if (numeric < current.min) {
				current.min = numeric;
				if (measureCell?.raw) current.minRaw = measureCell.raw;
			}
			if (numeric > current.max) {
				current.max = numeric;
				if (measureCell?.raw) current.maxRaw = measureCell.raw;
			}
		}
		groups.set(label, current);
	}

	const metricLabel = aggregateLabel(frame, dataset);
	const groupLabel = labelFor(dataset, groupColumn);
	const materialized = [...groups.values()].map((group) => {
		let metric = group.count;
		let raw: string | undefined;
		if (aggregate === "sum") metric = group.sum;
		else if (aggregate === "avg")
			metric = group.count === 0 ? 0 : group.sum / group.count;
		else if (aggregate === "min") {
			metric = Number.isFinite(group.min) ? group.min : 0;
			raw = group.minRaw;
		} else if (aggregate === "max") {
			metric = Number.isFinite(group.max) ? group.max : 0;
			raw = group.maxRaw;
		}
		return { group: group.label, metric, raw };
	});

	const direction = frame.orderBy?.direction ?? "desc";
	materialized.sort((a, b) =>
		direction === "asc" ? a.metric - b.metric : b.metric - a.metric,
	);
	const limited =
		frame.limit !== undefined
			? materialized.slice(0, frame.limit)
			: materialized;
	const outRows = limited.map((row) => {
		const out: Record<string, string | number | boolean | null> = {
			[groupLabel]: row.group,
			[metricLabel]: row.metric,
		};
		if (row.raw && frame.measureColumn) {
			out[labelFor(dataset, frame.measureColumn)] = row.raw;
		}
		return out;
	});
	return {
		rows: outRows,
		chart: {
			labels: limited.map((row) => row.group),
			values: limited.map((row) => row.metric),
		},
	};
}

function listRows(
	rows: SheetRow[],
	frame: SheetQueryFrame,
	dataset: SheetDataset,
): Record<string, string | number | boolean | null>[] {
	const projection =
		frame.projectionColumns.length > 0
			? frame.projectionColumns
			: defaultProjection(dataset);
	const limited = rows.slice(0, frame.limit ?? 50);
	return limited.map((row) => {
		const out: Record<string, string | number | boolean | null> = {};
		for (const columnId of projection) {
			out[labelFor(dataset, columnId)] = formatCell(row.cells[columnId]);
		}
		return out;
	});
}

export function querySheet(
	dataset: SheetDataset,
	question: string,
): SheetQueryResult {
	const frame = buildFrame(dataset, question);
	if (!frame) {
		return {
			ok: false,
			question,
			rejectionReason: "No supported spreadsheet query shape was found.",
			warnings: dataset.warnings,
		};
	}
	if (frame.routeReason === "missing_measure_column") {
		return {
			ok: false,
			question,
			rejectionReason:
				"The question asks for a metric, but no measure column matched.",
			queryFrame: frame,
			confidence: frame.confidence,
			warnings: dataset.warnings,
		};
	}

	const rows = filteredRows(dataset, frame.filters);
	if (frame.operation === "aggregate") {
		if (frame.groupByColumn) {
			const grouped = groupedRows(rows, frame, dataset);
			return {
				ok: true,
				question,
				queryFrame: frame,
				message: `${grouped.rows.length} grouped result${grouped.rows.length === 1 ? "" : "s"}.`,
				rows: grouped.rows,
				chart: grouped.chart,
				confidence: frame.confidence,
				warnings: dataset.warnings,
			};
		}
		const scalar = aggregateValues(
			rows,
			frame.aggregate ?? "count",
			frame.measureColumn,
		);
		return {
			ok: true,
			question,
			queryFrame: frame,
			message: `${aggregateLabel(frame, dataset)}: ${scalar}`,
			rows: [{ [aggregateLabel(frame, dataset)]: scalar }],
			scalar,
			confidence: frame.confidence,
			warnings: dataset.warnings,
		};
	}

	const outRows = listRows(rows, frame, dataset);
	return {
		ok: true,
		question,
		queryFrame: frame,
		message: `${outRows.length} row${outRows.length === 1 ? "" : "s"}.`,
		rows: outRows,
		confidence: frame.confidence,
		warnings: dataset.warnings,
	};
}
