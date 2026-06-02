import type { SheetDataset, SheetQueryFrame, SheetRow } from "../types.js";
import { defaultProjection } from "./projection.js";
import { formatCell, labelFor } from "./result-labels.js";

export function listRows(
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
