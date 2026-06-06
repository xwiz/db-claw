import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { scanLaravelConfig } from "./config.js";

let tmp: string;

beforeEach(async () => {
	tmp = await mkdtemp(path.join(tmpdir(), "semsql-laravel-config-"));
});

afterEach(async () => {
	await rm(tmp, { recursive: true, force: true });
});

async function write(rel: string, body: string): Promise<void> {
	const full = path.join(tmp, rel);
	await mkdir(path.dirname(full), { recursive: true });
	await writeFile(full, body, "utf8");
}

describe("scanLaravelConfig", () => {
	it("emits enum_value fragments from config/constants.php maps", async () => {
		await write(
			"config/constants.php",
			`<?php
return [
    'main_reasons' => [
        'A' => 'Amount (Overall)',
        'R' => 'Rate (STR)',
    ],
];
`,
		);

		const result = await scanLaravelConfig(tmp, ["transactions.main_reason"]);

		expect(result.fragments).toEqual(
			expect.arrayContaining([
				expect.objectContaining({
					term: "rate",
					canonical: {
						kind: "enum_value",
						enumName: "transactions.main_reason",
						rawValue: "R",
					},
				}),
				expect.objectContaining({
					term: "str",
					canonical: {
						kind: "enum_value",
						enumName: "transactions.main_reason",
						rawValue: "R",
					},
				}),
			]),
		);
	});

	it("connects prompt synonym aliases to compatible enum values", async () => {
		await write(
			"config/constants.php",
			`<?php
return [
    'main_reasons' => [
        'R' => 'Rate (STR)',
    ],
];
`,
		);
		await write(
			"config/ai.php",
			`<?php
return [
    'nl' => [
        'synonyms' => [
            '/\\b(bot\\-?like|scripted|automated)\\b/' => ' velocity ',
            '/\\b(rate\\s+of\\s+transactions?)\\b/' => ' velocity ',
        ],
    ],
];
`,
		);

		const result = await scanLaravelConfig(tmp, ["transactions.main_reason"]);

		expect(result.fragments).toEqual(
			expect.arrayContaining([
				expect.objectContaining({
					term: "bot like",
					canonical: {
						kind: "enum_value",
						enumName: "transactions.main_reason",
						rawValue: "R",
					},
				}),
				expect.objectContaining({
					term: "rate of transactions",
					canonical: {
						kind: "enum_value",
						enumName: "transactions.main_reason",
						rawValue: "R",
					},
				}),
			]),
		);
	});

	it("does not emit constants when no compatible field is known", async () => {
		await write(
			"config/constants.php",
			`<?php
return [
    'main_reasons' => [
        'R' => 'Rate (STR)',
    ],
];
`,
		);

		const result = await scanLaravelConfig(tmp, ["transactions.status"]);

		expect(result.fragments).toHaveLength(0);
	});

	it("connects prompt synonym aliases to compatible field vocabulary", async () => {
		await write(
			"config/ai.php",
			`<?php
return [
    'nl' => [
        'synonyms' => [
            '/\\bturnover\\b/' => ' total amount ',
            '/\\bthroughput\\b/' => ' total amount ',
            '/\\bpayee\\b/' => ' beneficiary ',
            '/\\bdestination\\s+account\\b/' => ' beneficiary ',
        ],
    ],
];
`,
		);

		const result = await scanLaravelConfig(tmp, [
			"transactions.amount",
			"transactions.recipient_account_no",
			"transactions.password_token",
		]);

		expect(result.fragments).toEqual(
			expect.arrayContaining([
				expect.objectContaining({
					term: "turnover",
					canonical: {
						kind: "field",
						field: "transactions.amount",
					},
				}),
				expect.objectContaining({
					term: "throughput",
					canonical: {
						kind: "field",
						field: "transactions.amount",
					},
				}),
				expect.objectContaining({
					term: "payee",
					canonical: {
						kind: "field",
						field: "transactions.recipient_account_no",
					},
				}),
				expect.objectContaining({
					term: "destination account",
					canonical: {
						kind: "field",
						field: "transactions.recipient_account_no",
					},
				}),
			]),
		);
		expect(
			result.fragments.some(
				(fragment) =>
					fragment.canonical.kind === "field" &&
					fragment.canonical.field === "transactions.password_token",
			),
		).toBe(false);
	});
});
