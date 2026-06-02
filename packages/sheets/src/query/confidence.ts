import type { QueryConfidence, SheetQueryFrame } from "../types.js";

function confidenceLevel(score: number): QueryConfidence["level"] {
	if (score >= 0.8) return "high";
	if (score >= 0.55) return "medium";
	return "low";
}

export function confidenceForFrame(
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

export function withConfidence(
	frame: Omit<SheetQueryFrame, "confidence">,
): SheetQueryFrame {
	return {
		...frame,
		confidence: confidenceForFrame(frame),
	};
}
