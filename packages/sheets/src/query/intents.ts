export { aggregateForRank, aggregateIntent } from "./aggregate-intents.js";
export {
	wantsBottom,
	wantsComparison,
	wantsDistinct,
	wantsDistribution,
	wantsFirst,
	wantsLast,
	wantsLatest,
	wantsOldest,
	wantsRank,
	wantsTop,
} from "./intent-basics.js";
export { topLimit } from "./limit-intents.js";
export { isListIntent, listDistinctIntent, listOrder } from "./list-intents.js";
export { shouldUseRankedList } from "./rank-intents.js";
