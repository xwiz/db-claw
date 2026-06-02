import { hasPhrase } from "../normalize.js";
import type { SheetDataset, SheetOrder, SheetQueryFrame } from "../types.js";
import {
	columnById,
	findColumnMention,
	findExplicitGroupByColumn,
	findImplicitRankGroupColumn,
	findMeasureColumn,
	findRankTargetGroupColumn,
	isLongTextColumn,
	isUsableDisplayColumn,
} from "./columns.js";
import { withConfidence } from "./confidence.js";
import { contextForPhrase } from "./context.js";
import { buildFilters } from "./filters.js";
import {
	aggregateForRank,
	aggregateIntent,
	isListIntent,
	listDistinctIntent,
	listOrder,
	shouldUseRankedList,
	topLimit,
	wantsBottom,
	wantsDistinct,
	wantsDistribution,
	wantsRank,
} from "./intents.js";
import {
	defaultProjection,
	explicitProjection,
	withProjectionColumns,
} from "./projection.js";

export function buildFrame(
	dataset: SheetDataset,
	question: string,
): SheetQueryFrame | undefined {
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

	if (wantsDistinct(ctx) && distinctColumn) {
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

	if (
		isListIntent(ctx) &&
		!explicitAggregate &&
		!wantsDistribution(ctx) &&
		!listDistinctIntent(ctx) &&
		!explicitGroupBy &&
		(!rankTargetGroup || rankedList)
	) {
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

	if (
		listDistinctIntent(ctx) &&
		distinctColumn &&
		filters.length === 0 &&
		!order
	) {
		return withConfidence({
			question,
			operation: "list",
			projectionColumns: [distinctColumn.id],
			filters,
			resultShape: "tabular",
			routeReason: "distinct_list",
		});
	}

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

	if (aggregate) {
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
