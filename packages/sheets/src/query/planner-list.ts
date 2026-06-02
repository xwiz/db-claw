import { hasPhrase } from "../normalize.js";
import type { SheetQueryFrame } from "../types.js";
import {
	columnById,
	isLongTextColumn,
	isUsableDisplayColumn,
} from "./columns.js";
import { withConfidence } from "./confidence.js";
import {
	isListIntent,
	listDistinctIntent,
	wantsDistribution,
} from "./intents.js";
import type { FramePlanningState } from "./planner-state.js";
import {
	defaultProjection,
	explicitProjection,
	withProjectionColumns,
} from "./projection.js";

export function planPrimaryListFrame(
	state: FramePlanningState,
): SheetQueryFrame | undefined {
	const {
		dataset,
		question,
		ctx,
		limit,
		filters,
		explicitGroupBy,
		rankTargetGroup,
		orderMeasure,
		order,
		explicitAggregate,
		rankedList,
	} = state;
	if (
		!isListIntent(ctx) ||
		explicitAggregate ||
		wantsDistribution(ctx) ||
		listDistinctIntent(ctx) ||
		explicitGroupBy ||
		(rankTargetGroup && !rankedList)
	) {
		return undefined;
	}

	const explicit = explicitProjection(dataset, ctx);
	const filterColumns = new Set(filters.map((filter) => filter.column));
	const hasContextColumn = explicit.some((columnId) => {
		const column = columnById(dataset, columnId);
		return (
			columnId !== orderMeasure?.id &&
			columnId !== order?.column &&
			!filterColumns.has(columnId) &&
			column !== undefined &&
			!isLongTextColumn(column) &&
			isUsableDisplayColumn(dataset, column, { allowIdentifiers: true })
		);
	});
	const baseProjection = hasContextColumn
		? explicit
		: defaultProjection(dataset);
	const projection = withProjectionColumns(baseProjection, [
		rankedList ? rankTargetGroup?.id : undefined,
		orderMeasure?.id,
		order?.column === "__row_index" ? undefined : order?.column,
		...filters.map((filter) => filter.column),
	]);
	const frame: Omit<SheetQueryFrame, "confidence"> = {
		question,
		operation: "list",
		projectionColumns: projection,
		filters,
		resultShape: "tabular",
		routeReason: filters.length > 0 ? "filtered_list" : "list_projection",
	};
	if (limit !== undefined) frame.limit = limit;
	if (order) frame.orderBy = order;
	return withConfidence(frame);
}

export function planDistinctListFrame(
	state: FramePlanningState,
): SheetQueryFrame | undefined {
	const {
		question,
		ctx,
		filters,
		order,
		distinctColumn,
		explicitAggregate,
		explicitGroupBy,
		rankTargetGroup,
	} = state;
	if (
		!listDistinctIntent(ctx) ||
		!distinctColumn ||
		filters.length > 0 ||
		order ||
		explicitAggregate ||
		explicitGroupBy ||
		rankTargetGroup ||
		wantsDistribution(ctx)
	) {
		return undefined;
	}
	return withConfidence({
		question,
		operation: "list",
		projectionColumns: [distinctColumn.id],
		filters,
		resultShape: "tabular",
		routeReason: "distinct_list",
	});
}

export function planFallbackListFrame(
	state: FramePlanningState,
): SheetQueryFrame | undefined {
	const { dataset, question, ctx, limit, filters } = state;
	if (
		filters.length === 0 &&
		!hasPhrase(ctx.normalized, "show") &&
		!hasPhrase(ctx.normalized, "list") &&
		!hasPhrase(ctx.normalized, "find")
	) {
		return undefined;
	}

	const frame: Omit<SheetQueryFrame, "confidence"> = {
		question,
		operation: "list",
		projectionColumns: explicitProjection(dataset, ctx),
		filters,
		resultShape: "tabular",
		routeReason: filters.length > 0 ? "filtered_list" : "list_projection",
	};
	if (limit !== undefined) frame.limit = limit;
	return withConfidence(frame);
}
