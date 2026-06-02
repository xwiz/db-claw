import type { CellValue, SheetDataset, SheetQueryFrame } from "../types.js";
import { columnById } from "./columns.js";

export function formatCell(
	cell: CellValue | undefined,
): string | number | boolean | null {
	if (!cell || cell.value === null) return null;
	if (cell.value instanceof Date) return cell.value.toISOString().slice(0, 10);
	return cell.value;
}

export function labelFor(dataset: SheetDataset, columnId: string): string {
	return columnById(dataset, columnId)?.label ?? columnId;
}

export function aggregateLabel(
	frame: SheetQueryFrame,
	dataset: SheetDataset,
): string {
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
