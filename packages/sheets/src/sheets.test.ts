import { describe, expect, it } from "vitest";

import { parseCsv } from "./csv.js";
import { toGoogleSheetsCsvUrl } from "./google.js";
import { buildSheetDataset } from "./infer.js";
import { querySheet } from "./query.js";
import { SAMPLE_CSV, SHEET_USE_CASES } from "./sample.js";
import { suggestSheetQuestions } from "./suggest.js";

function sampleDataset() {
	return buildSheetDataset(parseCsv(SAMPLE_CSV));
}

const MARKETING_CSV = `Campaign,Channel,Status,Spend,Clicks,Start Date
Launch A,Search,active,"$1,200",300,2024-05-01
Launch B,Social,active,$800,220,2024-05-04
Evergreen,Search,paused,$500,150,2024-04-20
Partner Push,Email,active,$300,90,2024-05-08
`;

describe("parseCsv", () => {
	it("handles quoted commas and escaped quotes", () => {
		const parsed = parseCsv('Name,Note\n"Acme, Inc","said ""yes"""\n');
		expect(parsed.headers).toEqual(["Name", "Note"]);
		expect(parsed.rows).toEqual([["Acme, Inc", 'said "yes"']]);
	});
});

describe("toGoogleSheetsCsvUrl", () => {
	it("converts a normal public sheet URL to a CSV export URL", () => {
		const out = toGoogleSheetsCsvUrl(
			"https://docs.google.com/spreadsheets/d/abc123/edit#gid=987",
		);
		expect(out).toBe(
			"https://docs.google.com/spreadsheets/d/abc123/export?format=csv&gid=987",
		);
	});

	it("leaves non-Google URLs intact", () => {
		expect(toGoogleSheetsCsvUrl("https://example.com/data.csv")).toBe(
			"https://example.com/data.csv",
		);
	});
});

describe("buildSheetDataset", () => {
	it("infers measures, dates, and dimensions", () => {
		const dataset = sampleDataset();
		const revenue = dataset.columns.find((column) => column.id === "revenue");
		const orderDate = dataset.columns.find(
			(column) => column.id === "order_date",
		);
		const region = dataset.columns.find((column) => column.id === "region");
		expect(revenue?.roles).toContain("measure");
		expect(orderDate?.kind).toBe("date");
		expect(region?.roles).toContain("dimension");
		expect(dataset.rowCount).toBe(12);
	});
});

describe("querySheet", () => {
	it("answers grouped revenue questions", () => {
		const result = querySheet(sampleDataset(), "total revenue by region");
		expect(result.ok).toBe(true);
		if (!result.ok) return;
		expect(result.queryFrame.resultShape).toBe("categorical_chart");
		expect(result.confidence.level).toBe("high");
		expect(result.confidence.reasons).toEqual(
			expect.arrayContaining([
				"recognized sum question",
				"matched measure column revenue",
				"matched group column region",
			]),
		);
		expect(result.rows[0]).toEqual({ Region: "LATAM", "SUM Revenue": 10500 });
	});

	it("answers top-N grouped questions", () => {
		const result = querySheet(sampleDataset(), "top 5 customers by sales");
		expect(result.ok).toBe(true);
		if (!result.ok) return;
		expect(result.rows).toHaveLength(5);
		expect(result.rows[0]?.Customer).toBe("Lumen Goods");
	});

	it("answers average questions with month filters", () => {
		const result = querySheet(sampleDataset(), "average order value in May");
		expect(result.ok).toBe(true);
		if (!result.ok) return;
		expect(result.scalar).toBeCloseTo(1650, 3);
		expect(result.queryFrame.filters).toContainEqual({
			kind: "month",
			column: "order_date",
			month: 5,
		});
	});

	it("answers count questions with multiple value filters", () => {
		const result = querySheet(
			sampleDataset(),
			"how many paid invoices are overdue?",
		);
		expect(result.ok).toBe(true);
		if (!result.ok) return;
		expect(result.scalar).toBe(4);
	});

	it("keeps scalar counts scalar when a dimension name appears", () => {
		const useCase = SHEET_USE_CASES.find(
			(candidate) => candidate.id === "support_queue",
		);
		expect(useCase).toBeDefined();
		if (!useCase) return;
		const result = querySheet(
			buildSheetDataset(parseCsv(useCase.csv)),
			"how many open tickets are high priority?",
		);
		expect(result.ok).toBe(true);
		if (!result.ok) return;
		expect(result.queryFrame.resultShape).toBe("scalar_metric");
		expect(result.scalar).toBe(3);
	});

	it("runs every packaged practical use-case query without custom demo code", () => {
		for (const useCase of SHEET_USE_CASES) {
			const dataset = buildSheetDataset(parseCsv(useCase.csv));
			for (const question of useCase.questions) {
				const result = querySheet(dataset, question);
				expect(result.ok, `${useCase.id}: ${question}`).toBe(true);
			}
		}
	});

	it("returns expected results for every packaged practical use-case query", () => {
		const expectations: Record<string, Record<string, unknown>> = {
			"revenue_ops::total revenue by region": {
				first: { Region: "LATAM", "SUM Revenue": 10500 },
			},
			"revenue_ops::top 5 customers by sales": {
				first: { Customer: "Lumen Goods", "SUM Revenue": 4000 },
				length: 5,
			},
			"revenue_ops::average order value in May": { scalar: 1650 },
			"revenue_ops::how many paid invoices are overdue?": { scalar: 4 },
			"revenue_ops::show active accounts in LATAM": {
				first: { Customer: "Acme 4744" },
				length: 4,
			},
			"support_queue::how many open tickets are high priority?": {
				scalar: 3,
			},
			"support_queue::average response time hours by team": {
				first: {
					Team: "Billing",
					"AVG Response Time Hours": 6.333333333333333,
				},
			},
			"support_queue::show open tickets for Platform": {
				first: { "Ticket ID": "T-1003" },
				length: 2,
			},
			"support_queue::average satisfaction score in May": {
				scalar: 3.8333333333333335,
			},
			"inventory::total units on hand by warehouse": {
				first: { Warehouse: "Central", "SUM Units On Hand": 117 },
			},
			"inventory::show low stock products": {
				first: { Product: "Cloud Backup Starter" },
				length: 3,
			},
			"inventory::average unit cost by category": {
				first: { Category: "Analytics", "AVG Unit Cost": 204 },
			},
			"inventory::how many products are backordered?": { scalar: 1 },
		};

		for (const useCase of SHEET_USE_CASES) {
			const dataset = buildSheetDataset(parseCsv(useCase.csv));
			for (const question of useCase.questions) {
				const result = querySheet(dataset, question);
				expect(result.ok, `${useCase.id}: ${question}`).toBe(true);
				if (!result.ok) continue;
				const expected = expectations[`${useCase.id}::${question}`];
				expect(expected, `${useCase.id}: ${question}`).toBeDefined();
				if (!expected) continue;
				if ("scalar" in expected) {
					expect(result.scalar).toBeCloseTo(expected.scalar as number, 6);
				}
				if ("length" in expected) {
					expect(result.rows).toHaveLength(expected.length as number);
				}
				if ("first" in expected) {
					expect(result.rows[0]).toMatchObject(
						expected.first as Record<string, unknown>,
					);
				}
			}
		}
	});

	it("answers filtered list questions", () => {
		const result = querySheet(sampleDataset(), "show active accounts in LATAM");
		expect(result.ok).toBe(true);
		if (!result.ok) return;
		expect(result.rows).toHaveLength(4);
		expect(result.rows[0]?.Customer).toBe("Acme 4744");
	});

	it("fails closed for vague unsupported prompts", () => {
		const result = querySheet(sampleDataset(), "what is going on?");
		expect(result.ok).toBe(false);
		if (result.ok) return;
		expect(result.rejectionReason).toContain("No supported");
	});

	it("returns low-confidence diagnostics when a metric cannot be grounded", () => {
		const result = querySheet(sampleDataset(), "total margin by region");
		expect(result.ok).toBe(false);
		if (result.ok) return;
		expect(result.queryFrame?.routeReason).toBe("missing_measure_column");
		expect(result.confidence?.level).toBe("low");
		expect(result.confidence?.reasons).toContain(
			"no measure column matched the question",
		);
	});

	it("handles an uploaded-style CSV without custom use-case code", () => {
		const dataset = buildSheetDataset(parseCsv(MARKETING_CSV));
		expect(suggestSheetQuestions(dataset)).toEqual(
			expect.arrayContaining([
				"total spend by channel",
				"top 5 campaigns by spend",
			]),
		);

		const byChannel = querySheet(dataset, "total spend by channel");
		expect(byChannel.ok).toBe(true);
		if (!byChannel.ok) return;
		expect(byChannel.rows[0]).toEqual({
			Channel: "Search",
			"SUM Spend": 1700,
		});

		const topCampaigns = querySheet(dataset, "top 5 campaigns by clicks");
		expect(topCampaigns.ok).toBe(true);
		if (!topCampaigns.ok) return;
		expect(topCampaigns.rows[0]).toEqual({
			Campaign: "Launch A",
			"SUM Clicks": 300,
		});

		const activeCampaigns = querySheet(dataset, "show active campaigns");
		expect(activeCampaigns.ok).toBe(true);
		if (!activeCampaigns.ok) return;
		expect(activeCampaigns.rows).toHaveLength(3);
		expect(activeCampaigns.rows[0]).toMatchObject({
			Campaign: "Launch A",
		});
	});
});
