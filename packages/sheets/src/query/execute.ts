import type {
	SheetDataset,
	SheetQueryFrame,
	SheetQueryResult,
} from "../types.js";
import { aggregateValues, groupedRows } from "./aggregate-results.js";
import { listRows } from "./list-results.js";
import { aggregateLabel } from "./result-labels.js";
import { filteredRows } from "./row-filters.js";

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
				chartJs: grouped.chartJs,
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
