import type { AggregateFunction, CellValue, SheetRow } from "../types.js";

function numberValue(cell: CellValue | undefined): number | undefined {
	return typeof cell?.value === "number" ? cell.value : undefined;
}

export function aggregateValues(
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

export { numberValue };
