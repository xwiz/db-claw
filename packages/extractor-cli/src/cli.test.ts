import { execFileSync } from "node:child_process";
import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import { createRequire } from "node:module";
import { tmpdir } from "node:os";
import path from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { CLI_VERSION } from "./version.js";

const CLI = path.resolve(__dirname, "..", "dist", "cli.js");
const requirePackage = createRequire(import.meta.url);
const packageJson = requirePackage("../package.json") as { version: string };

let tmp: string;

beforeEach(async () => {
	tmp = await mkdtemp(path.join(tmpdir(), "semsql-cli-"));
});

afterEach(async () => {
	await rm(tmp, { recursive: true, force: true });
});

async function setupLaravelProject(): Promise<void> {
	await writeFile(
		path.join(tmp, "composer.json"),
		JSON.stringify({ require: { "laravel/framework": "^11.0" } }),
		"utf8",
	);
	await mkdir(path.join(tmp, "app", "Filament", "Resources"), {
		recursive: true,
	});
	await writeFile(
		path.join(tmp, "app", "Filament", "Resources", "StudentResource.php"),
		`<?php
        namespace App\\Filament\\Resources;
        class StudentResource extends Resource {
            protected static ?string $model = User::class;
            protected static ?string $modelLabel = 'Student';
            protected static ?string $pluralModelLabel = 'Students';
        }`,
		"utf8",
	);
}

function runCli(args: string[]): string {
	return execFileSync("node", [CLI, ...args], {
		encoding: "utf8",
	});
}

describe("semsql-extract CLI", () => {
	it("uses package.json as the extractor version source", () => {
		expect(CLI_VERSION).toBe(packageJson.version);
		expect(runCli(["--version"]).trim()).toBe(packageJson.version);
	});

	it("auto-detects Laravel and emits fragments to stdout", async () => {
		await setupLaravelProject();
		const out = runCli([tmp]);
		const lines = out.trim().split(/\r?\n/);
		expect(lines.length).toBeGreaterThanOrEqual(2);
		const parsed = lines.map((l) => JSON.parse(l));
		const terms = parsed.map((p) => p.term as string).sort();
		expect(terms).toEqual(["student", "students"]);
	});

	it("emits authored metric definitions from semsql.metrics.json", async () => {
		await setupLaravelProject();
		await writeFile(
			path.join(tmp, "semsql.metrics.json"),
			JSON.stringify({
				metrics: [
					{
						name: "lead_to_customer_conversion_rate",
						displayLabel: "Lead to customer conversion rate",
						metricKind: "conditional_rate",
						subjectEntity: "leads",
						numeratorField: "leads.status",
						numeratorOperator: "=",
						numeratorValue: "converted",
						numeratorValueKind: "value_dictionary",
						denominatorField: "leads.id",
						scale: 100,
						requiredEntities: ["leads"],
						aliases: ["lead conversion rate"],
					},
				],
			}),
			"utf8",
		);
		const out = runCli([tmp]);
		const parsed = out
			.trim()
			.split(/\r?\n/)
			.map((l) => JSON.parse(l));
		const metric = parsed.find((p) => p.record_kind === "metric_definition");
		expect(metric?.name).toBe("lead_to_customer_conversion_rate");
		expect(metric?.aliases).toEqual(["lead conversion rate"]);
		expect(metric?.locator?.extractor).toBe("semsql:metrics");
		expect(parsed.find((p) => p.term === "student")).toBeDefined();
	});

	it("emits authored aggregate metric definitions", async () => {
		await setupLaravelProject();
		await writeFile(
			path.join(tmp, "semsql.metrics.json"),
			JSON.stringify({
				metrics: [
					{
						name: "average_transaction_score",
						displayLabel: "Average transaction score",
						metricKind: "aggregate",
						subjectEntity: "transactions",
						measureField: "transactions.score",
						aggregate: "avg",
						scale: 1,
						requiredEntities: ["transactions"],
						aliases: ["average score"],
					},
				],
			}),
			"utf8",
		);
		const out = runCli([tmp]);
		const parsed = out
			.trim()
			.split(/\r?\n/)
			.map((l) => JSON.parse(l));
		const metric = parsed.find((p) => p.record_kind === "metric_definition");
		expect(metric?.name).toBe("average_transaction_score");
		expect(metric?.metricKind).toBe("aggregate");
		expect(metric?.measureField).toBe("transactions.score");
		expect(metric?.aggregate).toBe("AVG");
	});

	it("emits authored distinct-count metric definitions", async () => {
		await setupLaravelProject();
		await writeFile(
			path.join(tmp, "semsql.metrics.json"),
			JSON.stringify({
				metrics: [
					{
						name: "unique_users",
						displayLabel: "Unique users",
						metricKind: "aggregate",
						subjectEntity: "users",
						measureField: "users.id",
						aggregate: "count",
						distinct: true,
						scale: 1,
						requiredEntities: ["users"],
						aliases: ["unique users"],
					},
				],
			}),
			"utf8",
		);
		const out = runCli([tmp]);
		const parsed = out
			.trim()
			.split(/\r?\n/)
			.map((l) => JSON.parse(l));
		const metric = parsed.find((p) => p.record_kind === "metric_definition");
		expect(metric?.name).toBe("unique_users");
		expect(metric?.metricKind).toBe("aggregate");
		expect(metric?.measureField).toBe("users.id");
		expect(metric?.aggregate).toBe("COUNT");
		expect(metric?.distinct).toBe(true);
	});

	it("writes to --output when provided", async () => {
		await setupLaravelProject();
		const outFile = path.join(tmp, "frags.jsonl");
		const stdoutText = runCli([tmp, "--output", outFile]);
		expect(stdoutText).toBe("");
		const { readFile } = await import("node:fs/promises");
		const body = await readFile(outFile, "utf8");
		expect(body.split(/\r?\n/).filter((l) => l).length).toBeGreaterThan(0);
	});

	it("exits non-zero when no adapter matches", async () => {
		await mkdir(tmp, { recursive: true });
		let caught: { status?: number } | null = null;
		try {
			runCli([tmp]);
		} catch (e) {
			caught = e as { status?: number };
		}
		expect(caught).not.toBeNull();
		expect(caught?.status).toBe(2);
	});

	it("auto-detects Next.js and emits Drizzle fragments", async () => {
		await writeFile(
			path.join(tmp, "package.json"),
			JSON.stringify({ dependencies: { next: "^14.0.0" } }),
			"utf8",
		);
		await mkdir(path.join(tmp, "src", "db"), { recursive: true });
		await writeFile(
			path.join(tmp, "src", "db", "schema.ts"),
			`
            import { pgTable, text, integer, boolean } from "drizzle-orm/pg-core";
            export const users = pgTable("users", {
              id: integer("id").primaryKey(),
              email: text("email").notNull(),
              isActive: boolean("is_active").default(false),
            });
            `,
			"utf8",
		);
		const out = runCli([tmp]);
		const lines = out.trim().split(/\r?\n/);
		expect(lines.length).toBeGreaterThan(0);
		const parsed = lines.map((l) => JSON.parse(l));
		const isActive = parsed.find((p) => p.term === "is active");
		expect(isActive?.canonical?.field).toBe("users.is_active");
		expect(isActive?.locator?.layer).toBe(2);
	});

	it("auto-detect picks every adapter that matches (multi-stack repos)", async () => {
		// A repo that ships both a Laravel API and a Next.js dashboard.
		await writeFile(
			path.join(tmp, "composer.json"),
			JSON.stringify({ require: { "laravel/framework": "^11" } }),
			"utf8",
		);
		await writeFile(
			path.join(tmp, "package.json"),
			JSON.stringify({ dependencies: { next: "^14" } }),
			"utf8",
		);
		await mkdir(path.join(tmp, "app", "Filament", "Resources"), {
			recursive: true,
		});
		await writeFile(
			path.join(tmp, "app", "Filament", "Resources", "StudentResource.php"),
			`<?php
            class StudentResource extends Resource {
                protected static ?string $model = User::class;
                protected static ?string $modelLabel = 'Student';
            }`,
			"utf8",
		);
		await mkdir(path.join(tmp, "src", "db"), { recursive: true });
		await writeFile(
			path.join(tmp, "src", "db", "schema.ts"),
			`
            import { pgTable, text } from "drizzle-orm/pg-core";
            export const users = pgTable("users", { email: text("email") });
            `,
			"utf8",
		);
		const out = runCli([tmp]);
		const parsed = out
			.trim()
			.split(/\r?\n/)
			.map((l) => JSON.parse(l));
		// Filament-derived fragment present.
		expect(parsed.find((p) => p.term === "student")).toBeDefined();
		// Drizzle-derived fragment present.
		expect(parsed.find((p) => p.term === "email")).toBeDefined();
	});
});
