import { executeFrame } from "./query/execute.js";
import { buildFrame } from "./query/planner.js";
import type { SheetDataset, SheetQueryResult } from "./types.js";

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

	return executeFrame(dataset, question, frame);
}
