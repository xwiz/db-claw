import { normalizeText } from "../normalize.js";
import type { SheetDataset, SheetFilter, SheetRow } from "../types.js";

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

export function filteredRows(
	dataset: SheetDataset,
	filters: SheetFilter[],
): SheetRow[] {
	return dataset.rows.filter((row) =>
		filters.every((filter) => matchesFilter(row, filter)),
	);
}
