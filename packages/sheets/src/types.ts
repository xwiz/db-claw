export type ColumnKind = "number" | "date" | "boolean" | "text";

export type ColumnRole = "measure" | "dimension" | "date" | "display";

export type SheetPrimitive = string | number | boolean | Date | null;

export interface CsvData {
	headers: string[];
	rows: string[][];
}

export interface CellValue {
	raw: string;
	value: SheetPrimitive;
	normalized: string;
}

export interface SheetRow {
	index: number;
	cells: Record<string, CellValue>;
}

export interface SheetColumn {
	id: string;
	label: string;
	kind: ColumnKind;
	roles: ColumnRole[];
	nonEmptyCount: number;
	uniqueCount: number;
	examples: string[];
	confidence: number;
}

export interface SheetDataset {
	columns: SheetColumn[];
	rows: SheetRow[];
	rowCount: number;
	warnings: string[];
}

export type NumberOperator = "gt" | "gte" | "lt" | "lte" | "eq";

export type SheetFilter =
	| {
			kind: "equals";
			column: string;
			value: string | number | boolean;
	  }
	| {
			kind: "notEquals";
			column: string;
			value: string | number | boolean;
	  }
	| {
			kind: "number";
			column: string;
			operator: NumberOperator;
			value: number;
	  }
	| {
			kind: "month";
			column: string;
			month: number;
			year?: number;
	  }
	| {
			kind: "dateRange";
			column: string;
			start?: string;
			end?: string;
	  }
	| {
			kind: "presence";
			column: string;
			present: boolean;
	  };

export type AggregateFunction =
	| "count"
	| "distinctCount"
	| "sum"
	| "avg"
	| "min"
	| "max";

export type ResultShape =
	| "scalar_metric"
	| "categorical_chart"
	| "tabular"
	| "unknown";

export interface SheetOrder {
	column: string;
	direction: "asc" | "desc";
}

export type QueryConfidenceLevel = "high" | "medium" | "low";

export interface QueryConfidence {
	score: number;
	level: QueryConfidenceLevel;
	reasons: string[];
}

export interface SheetQueryFrame {
	question: string;
	operation: "aggregate" | "list";
	aggregate?: AggregateFunction;
	measureColumn?: string;
	groupByColumn?: string;
	projectionColumns: string[];
	filters: SheetFilter[];
	orderBy?: SheetOrder;
	limit?: number;
	resultShape: ResultShape;
	routeReason: string;
	confidence: QueryConfidence;
}

export interface ChartSeries {
	labels: string[];
	values: number[];
	label: string;
	groupLabel: string;
}

export type ChartJsType = "bar";

export interface ChartJsDataset {
	label: string;
	data: number[];
	backgroundColor?: string | string[];
	borderColor?: string | string[];
	borderWidth?: number;
}

export interface ChartJsConfig {
	type: ChartJsType;
	data: {
		labels: string[];
		datasets: ChartJsDataset[];
	};
	options: Record<string, unknown>;
}

export interface SheetQuerySuccess {
	ok: true;
	question: string;
	queryFrame: SheetQueryFrame;
	message: string;
	rows: Record<string, string | number | boolean | null>[];
	scalar?: number;
	chart?: ChartSeries;
	chartJs?: ChartJsConfig;
	confidence: QueryConfidence;
	warnings: string[];
}

export interface SheetQueryRejected {
	ok: false;
	question: string;
	rejectionReason: string;
	queryFrame?: SheetQueryFrame;
	confidence?: QueryConfidence;
	warnings: string[];
}

export type SheetQueryResult = SheetQuerySuccess | SheetQueryRejected;

export interface SheetUseCase {
	id: string;
	name: string;
	description: string;
	csv: string;
	questions: string[];
}
