#!/usr/bin/env node
/**
 * `semsql-extract` — Node-side orchestrator.
 *
 * Picks an adapter based on `--framework auto` (or an explicit name),
 * walks the project root, merges fragments via the priority cascade,
 * and emits JSONL to stdout (or a file via `--output`).
 *
 * The Rust `semsql extract --framework none --vocab-jsonl <file>` ingests
 * this output. Together they form the full pipeline:
 *
 *     semsql-extract <project>           → frags.jsonl
 *     semsql extract --vocab-jsonl …     → graph.semsql
 *     semsql query --graph graph.semsql  → SQL
 */

import { writeFile } from "node:fs/promises";
import { Command } from "commander";

import { mergeFragments, type Extractor, type VocabFragment } from "@semsql/extractor-sdk";
import { DjangoExtractor } from "@semsql/extractor-django";
import { LaravelExtractor } from "@semsql/extractor-laravel";
import { NextjsExtractor } from "@semsql/extractor-nextjs";
import { RailsExtractor } from "@semsql/extractor-rails";
import { VueExtractor } from "@semsql/extractor-vue";

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

async function pickAdapter(framework: string, root: string): Promise<Extractor[]> {
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

async function collect(adapters: Extractor[], root: string): Promise<VocabFragment[]> {
    const all: VocabFragment[] = [];
    for (const ext of adapters) {
        for await (const frag of ext.extract({ root })) {
            all.push(frag);
        }
    }
    return all;
}

async function emit(records: VocabFragment[], output: string | undefined): Promise<void> {
    const lines = records.map((r) => JSON.stringify(r)).join("\n") + (records.length ? "\n" : "");
    if (output) {
        await writeFile(output, lines, "utf8");
    } else {
        process.stdout.write(lines);
    }
}

const program = new Command();

program
    .name("semsql-extract")
    .description("Extract a vocabulary fragment stream from a project directory.")
    .version("0.1.0-dev");

program
    .argument("<path>", "project root")
    .option("-f, --framework <name>", "adapter to use; `auto` to detect", "auto")
    .option("-o, --output <file>", "write JSONL to file (defaults to stdout)")
    .option("--no-merge", "emit raw fragments without applying the priority cascade")
    .action(async (path: string, opts: RunOptions) => {
        const adapters = await pickAdapter(opts.framework, path);
        if (adapters.length === 0) {
            console.error(`no adapter matched. Pass --framework explicitly.`);
            process.exit(2);
        }
        const fragments = await collect(adapters, path);
        if (opts.merge !== false) {
            const merged = mergeFragments(fragments);
            // Re-emit merged.entries as fragments for the Rust ingester.
            const out: VocabFragment[] = merged.entries.map((m) => ({
                term: m.term,
                canonical: m.canonical,
                confidence: m.confidence,
                locator: m.locator,
            }));
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
