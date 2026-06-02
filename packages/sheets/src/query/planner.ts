import type { SheetDataset, SheetQueryFrame } from "../types.js";
import {
	findColumnMention,
	findExplicitGroupByColumn,
	findMeasureColumn,
	findRankTargetGroupColumn,
} from "./columns.js";
import { contextForPhrase } from "./context.js";
import { buildFilters } from "./filters.js";
import {
	aggregateIntent,
	listDistinctIntent,
	listOrder,
	shouldUseRankedList,
	topLimit,
	wantsDistinct,
	wantsRank,
} from "./intents.js";
import {
	planAggregateFrame,
	planDistinctAggregateFrame,
} from "./planner-aggregate.js";
import {
	planDistinctListFrame,
	planFallbackListFrame,
	planPrimaryListFrame,
} from "./planner-list.js";
import type { FramePlanningState } from "./planner-state.js";

function planningState(
	dataset: SheetDataset,
	question: string,
): FramePlanningState | undefined {
	const ctx = contextForPhrase(question);
	if (ctx.normalized.length === 0) return undefined;

	const limit = topLimit(ctx);
	const filters = buildFilters(dataset, ctx);
	const rankIntent = wantsRank(ctx);
	const explicitGroupBy = findExplicitGroupByColumn(dataset, ctx);
	const rankTargetGroup = rankIntent
		? findRankTargetGroupColumn(dataset, ctx)
		: undefined;
	const orderMeasure = findMeasureColumn(dataset, ctx);
	const order = listOrder(dataset, ctx, orderMeasure);
	const explicitAggregate = aggregateIntent(ctx);
	const rankedList = shouldUseRankedList(ctx, rankTargetGroup, orderMeasure);
	const distinctColumn =
		wantsDistinct(ctx) || listDistinctIntent(ctx)
			? findColumnMention(dataset, ctx)
			: undefined;

	return {
		dataset,
		question,
		ctx,
		limit,
		filters,
		rankIntent,
		explicitGroupBy,
		rankTargetGroup,
		orderMeasure,
		order,
		explicitAggregate,
		rankedList,
		distinctColumn,
	};
}

export function buildFrame(
	dataset: SheetDataset,
	question: string,
): SheetQueryFrame | undefined {
	const state = planningState(dataset, question);
	if (!state) return undefined;

	return (
		planDistinctAggregateFrame(state) ??
		planPrimaryListFrame(state) ??
		planDistinctListFrame(state) ??
		planAggregateFrame(state) ??
		planFallbackListFrame(state)
	);
}
