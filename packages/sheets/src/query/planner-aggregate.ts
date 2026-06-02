import type { SheetOrder, SheetQueryFrame } from "../types.js";
import { findImplicitRankGroupColumn } from "./columns.js";
import { withConfidence } from "./confidence.js";
import {
	aggregateForRank,
	wantsBottom,
	wantsComparison,
	wantsDistinct,
	wantsDistribution,
} from "./intents.js";
import type { FramePlanningState } from "./planner-state.js";

export function planDistinctAggregateFrame(
	state: FramePlanningState,
): SheetQueryFrame | undefined {
	const { question, ctx, filters, distinctColumn } = state;
	if (!wantsDistinct(ctx) || !distinctColumn) return undefined;
	return withConfidence({
		question,
		operation: "aggregate",
		aggregate: "distinctCount",
		measureColumn: distinctColumn.id,
		projectionColumns: [],
		filters,
		resultShape: "scalar_metric",
		routeReason: "aggregate_scalar",
	});
}

export function planAggregateFrame(
	state: FramePlanningState,
): SheetQueryFrame | undefined {
	const {
		dataset,
		question,
		ctx,
		limit,
		filters,
		rankIntent,
		explicitGroupBy,
		rankTargetGroup,
		orderMeasure,
		explicitAggregate,
	} = state;
	let aggregate =
		explicitAggregate ?? (wantsDistribution(ctx) ? "count" : undefined);
	let measure =
		aggregate && aggregate !== "count" && aggregate !== "distinctCount"
			? orderMeasure
			: undefined;
	let groupBy =
		explicitGroupBy ??
		rankTargetGroup ??
		(aggregate && wantsDistribution(ctx)
			? findImplicitRankGroupColumn(dataset, ctx)
			: undefined);

	if (!aggregate && groupBy && orderMeasure) {
		aggregate = "sum";
		measure = orderMeasure;
	}
	if (!aggregate && groupBy && wantsComparison(ctx)) {
		aggregate = "count";
	}

	if (!aggregate && rankIntent) {
		groupBy =
			explicitGroupBy ??
			rankTargetGroup ??
			findImplicitRankGroupColumn(dataset, ctx);
		if (orderMeasure) {
			aggregate = aggregateForRank(ctx);
			measure = orderMeasure;
		} else if (groupBy) {
			aggregate = "count";
		}
	}

	if (
		aggregate &&
		aggregate !== "count" &&
		aggregate !== "distinctCount" &&
		!measure
	) {
		return withConfidence({
			question,
			operation: "aggregate",
			aggregate,
			projectionColumns: [],
			filters,
			resultShape: "unknown",
			routeReason: "missing_measure_column",
		});
	}

	if (!aggregate) return undefined;
	const frame: Omit<SheetQueryFrame, "confidence"> = {
		question,
		operation: "aggregate",
		aggregate,
		projectionColumns: [],
		filters,
		resultShape: groupBy ? "categorical_chart" : "scalar_metric",
		routeReason: groupBy ? "aggregate_grouped" : "aggregate_scalar",
	};
	if (measure) frame.measureColumn = measure.id;
	if (groupBy) frame.groupByColumn = groupBy.id;
	if (limit !== undefined) frame.limit = limit;
	const order: SheetOrder = {
		column: measure?.id ?? groupBy?.id ?? "__count",
		direction: wantsBottom(ctx) ? "asc" : "desc",
	};
	if (rankIntent || groupBy) frame.orderBy = order;
	return withConfidence(frame);
}
