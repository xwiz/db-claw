import type {
	AggregateFunction,
	SheetColumn,
	SheetDataset,
	SheetFilter,
	SheetOrder,
} from "../types.js";
import type { RouteContext } from "./context.js";

export interface FramePlanningState {
	dataset: SheetDataset;
	question: string;
	ctx: RouteContext;
	limit: number | undefined;
	filters: SheetFilter[];
	rankIntent: boolean;
	explicitGroupBy: SheetColumn | undefined;
	rankTargetGroup: SheetColumn | undefined;
	orderMeasure: SheetColumn | undefined;
	order: SheetOrder | undefined;
	explicitAggregate: AggregateFunction | undefined;
	rankedList: boolean;
	distinctColumn: SheetColumn | undefined;
}
