#!/usr/bin/env node
/**
 * `semsql-extract` — Node-side orchestrator.
 *
 * Picks an adapter based on `--framework auto` (or an explicit name),
 * walks the project root, merges fragments via the priority cascade,
 * and emits JSONL to stdout (or a file via `--output`).
 *
 * The Rust `semsql extract --framework <name>` command can invoke this
 * binary directly and ingest its output into the DB-grounded graph. The
 * lower-level JSONL path remains useful for debugging:
 *
 *     semsql-extract <project>           → frags.jsonl
 *     semsql extract --vocab-jsonl …     → graph.semsql
 *     semsql query --graph graph.semsql  → SQL
 */

import { readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import { Command } from "commander";

import { DjangoExtractor } from "@semsql/extractor-django";
import { LaravelExtractor } from "@semsql/extractor-laravel";
import { NextjsExtractor } from "@semsql/extractor-nextjs";
import { RailsExtractor } from "@semsql/extractor-rails";
import {
	type Extractor,
	type MetricDefinitionFragment,
	type SemanticFragment,
	SourceLayer,
	type VocabFragment,
	mergeFragments,
} from "@semsql/extractor-sdk";
import { VueExtractor } from "@semsql/extractor-vue";

import { CLI_VERSION } from "./version.js";

const ADAPTERS: Record<string, () => Extractor> = {
	django: () => new DjangoExtractor(),
	laravel: () => new LaravelExtractor(),
	nextjs: () => new NextjsExtractor(),
	rails: () => new RailsExtractor(),
	vue: () => new VueExtractor(),
};

interface RunOptions {
	framework: string;
	output?: string;
	merge: boolean;
}

async function pickAdapter(
	framework: string,
	root: string,
): Promise<Extractor[]> {
	if (framework === "auto") {
		const found: Extractor[] = [];
		for (const make of Object.values(ADAPTERS)) {
			const ext = make();
			if (await ext.detect(root)) found.push(ext);
		}
		return found;
	}
	const make = ADAPTERS[framework];
	if (!make) {
		throw new Error(
			`unknown framework '${framework}'. Available: auto, ${Object.keys(ADAPTERS).join(", ")}`,
		);
	}
	return [make()];
}

async function collect(
	adapters: Extractor[],
	root: string,
): Promise<SemanticFragment[]> {
	const all: SemanticFragment[] = [];
	for (const ext of adapters) {
		for await (const frag of ext.extract({ root })) {
			all.push(frag);
		}
	}
	all.push(...(await loadAuthoredMetricDefinitions(root)));
	return all;
}

async function loadAuthoredMetricDefinitions(
	root: string,
): Promise<MetricDefinitionFragment[]> {
	const file = path.join(root, "semsql.metrics.json");
	let raw = "";
	try {
		raw = await readFile(file, "utf8");
	} catch (error) {
		if ((error as { code?: string }).code === "ENOENT") return [];
		throw error;
	}
	const parsed = JSON.parse(raw) as unknown;
	const metrics = Array.isArray(parsed)
		? parsed
		: typeof parsed === "object" && parsed !== null
			? (parsed as { metrics?: unknown }).metrics
			: undefined;
	if (!Array.isArray(metrics)) {
		throw new Error(
			"semsql.metrics.json must be an array or { metrics: [...] }",
		);
	}
	return metrics.map((metric, index) =>
		metricDefinitionFromObject(metric, file, index + 1),
	);
}

function metricDefinitionFromObject(
	raw: unknown,
	file: string,
	line: number,
): MetricDefinitionFragment {
	if (typeof raw !== "object" || raw === null) {
		throw new Error(`invalid metric definition at ${file}:${line}`);
	}
	const value = raw as Record<string, unknown>;
	const requiredEntities = stringArray(value.requiredEntities);
	const aliases = stringArray(value.aliases);
	const metricKind =
		value.metricKind === "aggregate" ? "aggregate" : "conditional_rate";
	const fragment: MetricDefinitionFragment = {
		record_kind: "metric_definition",
		name: requiredString(value.name, "name"),
		metricKind,
		subjectEntity: requiredString(value.subjectEntity, "subjectEntity"),
		scale:
			typeof value.scale === "number" && Number.isFinite(value.scale)
				? value.scale
				: metricKind === "conditional_rate"
					? 100
					: 1,
		requiredEntities,
		aliases,
		locator: {
			file: path.relative(path.dirname(file), file) || "semsql.metrics.json",
			line,
			layer: SourceLayer.AppConstant,
			extractor: "semsql:metrics",
		},
	};
	if (metricKind === "aggregate") {
		fragment.measureField = requiredString(value.measureField, "measureField");
		fragment.aggregate = requiredAggregate(value.aggregate);
		if (value.distinct === true && fragment.aggregate !== "COUNT") {
			throw new Error(
				"metric definition field distinct is only supported with aggregate COUNT",
			);
		}
		if (value.distinct === true) {
			fragment.distinct = true;
		}
		if (typeof value.denominatorField === "string") {
			fragment.denominatorField = value.denominatorField;
		}
	} else {
		fragment.numeratorField = requiredString(
			value.numeratorField,
			"numeratorField",
		);
		fragment.numeratorOperator = requiredString(
			value.numeratorOperator ?? "=",
			"numeratorOperator",
		);
		fragment.numeratorValue = requiredString(
			value.numeratorValue,
			"numeratorValue",
		);
		fragment.numeratorValueKind = requiredString(
			value.numeratorValueKind ?? "literal",
			"numeratorValueKind",
		);
		fragment.denominatorField = requiredString(
			value.denominatorField,
			"denominatorField",
		);
	}
	if (typeof value.displayLabel === "string") {
		fragment.displayLabel = value.displayLabel;
	}
	return fragment;
}

function requiredAggregate(
	value: unknown,
): NonNullable<MetricDefinitionFragment["aggregate"]> {
	if (typeof value !== "string") {
		throw new Error(
			"metric definition field aggregate must be one of AVG, COUNT, MAX, MIN, SUM",
		);
	}
	const aggregate = value.trim().toUpperCase();
	if (
		aggregate === "AVG" ||
		aggregate === "COUNT" ||
		aggregate === "MAX" ||
		aggregate === "MIN" ||
		aggregate === "SUM"
	) {
		return aggregate;
	}
	throw new Error(
		"metric definition field aggregate must be one of AVG, COUNT, MAX, MIN, SUM",
	);
}

function requiredString(value: unknown, field: string): string {
	if (typeof value !== "string" || value.trim() === "") {
		throw new Error(
			`metric definition field ${field} must be a non-empty string`,
		);
	}
	return value;
}

function stringArray(value: unknown): string[] {
	if (value === undefined) return [];
	if (!Array.isArray(value)) {
		throw new Error(
			"metric definition aliases/requiredEntities must be arrays",
		);
	}
	return value.filter((item): item is string => typeof item === "string");
}

async function emit(
	records: SemanticFragment[],
	output: string | undefined,
): Promise<void> {
	const lines =
		records.map((r) => JSON.stringify(r)).join("\n") +
		(records.length ? "\n" : "");
	if (output) {
		await writeFile(output, lines, "utf8");
	} else {
		process.stdout.write(lines);
	}
}

const program = new Command();

function isMetricDefinitionFragment(
	fragment: SemanticFragment,
): fragment is MetricDefinitionFragment {
	return (
		"record_kind" in fragment && fragment.record_kind === "metric_definition"
	);
}

program
	.name("semsql-extract")
	.description("Extract a vocabulary fragment stream from a project directory.")
	.version(CLI_VERSION);

program
	.argument("<path>", "project root")
	.option("-f, --framework <name>", "adapter to use; `auto` to detect", "auto")
	.option("-o, --output <file>", "write JSONL to file (defaults to stdout)")
	.option(
		"--no-merge",
		"emit raw fragments without applying the priority cascade",
	)
	.action(async (path: string, opts: RunOptions) => {
		const adapters = await pickAdapter(opts.framework, path);
		if (adapters.length === 0) {
			console.error(`no adapter matched. Pass --framework explicitly.`);
			process.exit(2);
		}
		const fragments = await collect(adapters, path);
		if (opts.merge !== false) {
			const vocabFragments = fragments.filter(
				(fragment): fragment is VocabFragment =>
					!isMetricDefinitionFragment(fragment),
			);
			const metricFragments = fragments.filter(isMetricDefinitionFragment);
			const merged = mergeFragments(vocabFragments);
			// Re-emit merged.entries as fragments for the Rust ingester.
			const out: SemanticFragment[] = merged.entries.map((m) => ({
				term: m.term,
				canonical: m.canonical,
				confidence: m.confidence,
				locator: m.locator,
			}));
			out.push(...metricFragments);
			await emit(out, opts.output);
			if (merged.conflicts.length > 0) {
				console.error(
					`${merged.conflicts.length} vocabulary conflicts — run \`semsql doctor\` to inspect.`,
				);
			}
		} else {
			await emit(fragments, opts.output);
		}
	});

await program.parseAsync(process.argv);
