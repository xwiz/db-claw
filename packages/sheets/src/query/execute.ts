import { normalizeText } from "../normalize.js";
import type {
	AggregateFunction,
	CellValue,
	ChartSeries,
	SheetDataset,
	SheetFilter,
	SheetQueryFrame,
	SheetQueryResult,
	SheetRow,
} from "../types.js";
import { columnById } from "./columns.js";
import { defaultProjection } from "./projection.js";

interface GroupAccumulator {
	label: string;
	count: number;
	sum: number;
	min: number;
	max: number;
	minRaw?: string;
	maxRaw?: string;
}

function matchesFilter(row: SheetRow, filter: SheetFilter): boolean {
	const cell = row.cells[filter.column];
	if (!cell) return false;
	if (filter.kind === "equals") {
		if (
			(typeof filter.value === "boolean" || typeof filter.value === "number") &&
			cell.value === filter.value
		) {
			return true;
		}
		return cell.normalized === normalizeText(String(filter.value));
	}
	if (filter.kind === "notEquals") {
		if (
			(typeof filter.value === "boolean" || typeof filter.value === "number") &&
			cell.value === filter.value
		) {
			return false;
		}
		return cell.normalized !== normalizeText(String(filter.value));
	}
	if (filter.kind === "number") {
		if (typeof cell.value !== "number") return false;
		if (filter.operator === "gt") return cell.value > filter.value;
		if (filter.operator === "gte") return cell.value >= filter.value;
		if (filter.operator === "lt") return cell.value < filter.value;
		if (filter.operator === "lte") return cell.value <= filter.value;
		return cell.value === filter.value;
	}
	if (filter.kind === "dateRange") {
		if (!(cell.value instanceof Date)) return false;
		const timestamp = cell.value.getTime();
		const start = filter.start ? new Date(filter.start).getTime() : undefined;
		const end = filter.end ? new Date(filter.end).getTime() : undefined;
		if (start !== undefined && timestamp < start) return false;
		if (end !== undefined && timestamp >= end) return false;
		return true;
	}
	if (filter.kind === "presence") {
		const present = cell.raw.trim().length > 0 && cell.value !== null;
		return filter.present ? present : !present;
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
	if (aggregate === "distinctCount") {
		if (!measureColumn) return 0;
		const seen = new Set<string>();
		for (const row of rows) {
			const cell = row.cells[measureColumn];
			if (!cell || cell.normalized.length === 0) continue;
			seen.add(cell.normalized);
		}
		return seen.size;
	}
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
	if (frame.aggregate === "distinctCount") {
		const measure = frame.measureColumn
			? labelFor(dataset, frame.measureColumn)
			: "Value";
		return `Distinct ${measure}`;
	}
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
	const ordered = [...rows];
	if (frame.orderBy) {
		const orderBy = frame.orderBy;
		const direction = orderBy.direction === "asc" ? 1 : -1;
		ordered.sort((a, b) => {
			if (orderBy.column === "__row_index") {
				return (a.index - b.index) * direction;
			}
			const left = a.cells[orderBy.column]?.value;
			const right = b.cells[orderBy.column]?.value;
			if (left === null || left === undefined) return 1;
			if (right === null || right === undefined) return -1;
			if (typeof left === "number" && typeof right === "number") {
				return (left - right) * direction;
			}
			if (left instanceof Date && right instanceof Date) {
				return (left.getTime() - right.getTime()) * direction;
			}
			return String(left).localeCompare(String(right)) * direction;
		});
	}
	const sourceRows =
		frame.routeReason === "distinct_list"
			? ordered
			: ordered.slice(0, frame.limit ?? 50);
	const outRows = sourceRows.map((row) => {
		const out: Record<string, string | number | boolean | null> = {};
		for (const columnId of projection) {
			out[labelFor(dataset, columnId)] = formatCell(row.cells[columnId]);
		}
		return out;
	});
	if (frame.routeReason !== "distinct_list") return outRows;
	const seen = new Set<string>();
	const distinctRows: Record<string, string | number | boolean | null>[] = [];
	for (const row of outRows) {
		if (Object.values(row).every((value) => value === null || value === "")) {
			continue;
		}
		const key = JSON.stringify(row);
		if (seen.has(key)) continue;
		seen.add(key);
		distinctRows.push(row);
	}
	return distinctRows.slice(0, frame.limit ?? 50);
}

export function executeFrame(
	dataset: SheetDataset,
	question: string,
	frame: SheetQueryFrame,
): SheetQueryResult {
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
