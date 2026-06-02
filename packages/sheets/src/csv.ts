import type { CsvData } from "./types.js";

function trimBom(value: string): string {
	return value.replace(/^\uFEFF/, "");
}

function isBlankRow(row: string[]): boolean {
	return row.every((cell) => cell.trim().length === 0);
}

export function parseCsv(input: string): CsvData {
	const rows: string[][] = [];
	let row: string[] = [];
	let cell = "";
	let inQuotes = false;

	for (let i = 0; i < input.length; i += 1) {
		const ch = input[i];
		if (ch === undefined) continue;

		if (inQuotes) {
			if (ch === '"') {
				const next = input[i + 1];
				if (next === '"') {
					cell += '"';
					i += 1;
				} else {
					inQuotes = false;
				}
			} else {
				cell += ch;
			}
			continue;
		}

		if (ch === '"') {
			inQuotes = true;
			continue;
		}
		if (ch === ",") {
			row.push(cell);
			cell = "";
			continue;
		}
		if (ch === "\n") {
			row.push(cell);
			rows.push(row);
			row = [];
			cell = "";
			continue;
		}
		if (ch === "\r") {
			continue;
		}
		cell += ch;
	}

	if (cell.length > 0 || row.length > 0 || input.length > 0) {
		row.push(cell);
		rows.push(row);
	}

	while (rows.length > 0 && isBlankRow(rows[rows.length - 1]!)) {
		rows.pop();
	}

	const [headerRow, ...bodyRows] = rows;
	if (!headerRow) {
		return { headers: [], rows: [] };
	}

	const headers = headerRow.map((header, idx) => {
		const trimmed = trimBom(header).trim();
		return trimmed.length > 0 ? trimmed : `Column ${idx + 1}`;
	});
	return {
		headers,
		rows: bodyRows.filter((bodyRow) => !isBlankRow(bodyRow)),
	};
}
