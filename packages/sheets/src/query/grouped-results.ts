import { toChartJsConfig } from "../chartjs.js";
import type {
	ChartJsConfig,
	ChartSeries,
	SheetDataset,
	SheetQueryFrame,
	SheetRow,
} from "../types.js";
import { numberValue } from "./aggregate-values.js";
import { aggregateLabel, labelFor } from "./result-labels.js";

interface GroupAccumulator {
	label: string;
	count: number;
	sum: number;
	min: number;
	max: number;
	minRaw?: string;
	maxRaw?: string;
}

export function groupedRows(
	rows: SheetRow[],
	frame: SheetQueryFrame,
	dataset: SheetDataset,
): {
	rows: Record<string, string | number | boolean | null>[];
	chart: ChartSeries;
	chartJs: ChartJsConfig;
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
	const chart: ChartSeries = {
		labels: limited.map((row) => row.group),
		values: limited.map((row) => row.metric),
		label: metricLabel,
		groupLabel,
	};
	return {
		rows: outRows,
		chart,
		chartJs: toChartJsConfig(chart),
	};
}
