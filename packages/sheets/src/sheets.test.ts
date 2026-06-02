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

const BOM_CSV = `Item,Subsystem,Component,Specification,Quantity,Sourcing,Cost
1,EHD Subsystem,HV Power Module,12V DC input,20-30kV DC output,minimum 3A input current,1
2,EHD Subsystem,Needle Emitter,Sharp steel needle,1,Local market,100-500
6,EHD Subsystem,High-Voltage Silicone Wire,20kV rated silicone insulation,2m,Online,2000-5000
19,Water Cooling Loop,Hose Clamps,10-12mm stainless steel,4,Hardware store,500-1000
22,Ozone Catalyst Subsystem,Catalyst Material,Manganese dioxide powder,2-3 batteries (yield ~10g powder),Recycled,0
23,Ozone Catalyst Subsystem,Catalyst Binder (optional),Small amount of water,10-20mL,N/A,0
30,Power System,Battery Cables (solar panel side),12 AWG stranded copper,4-6m,Hardware store,1000-2000
50,Assembly / Maintenance,M3 Stainless Screws,Assorted 8-16mm screws,24,Hardware store,1000
47,Assembly / Maintenance,Distilled Water,For reservoir filling,5L,Supermarket,500-1000
`;

const SOCIAL_CSV = `URL,Title,Author,Likes,Published Date,Status
https://x.example/a,"@ana on X, Nov 21, 2025",ana,7,2025-11-21,Yes
https://x.example/ben,"@ben on X, Dec 03, 2025",ben,15,2025-12-03,Yes
https://x.example/cam,"@cam on X, Dec 06, 2025",cam,2,2025-12-06,No
`;

const TRIP_LIKE_CSV = `ID,Applicant,Gender,Partner Name,Wallet Balance,Created At
1,Ada Lovelace,Female,,249.90,2025-10-19
2,Grace Hopper,Female,,319.90,2025-10-18
3,Alan Turing,Male,Chris,221.90,2025-10-17
4,Katherine Johnson,Female,,800.00,2025-10-16
`;

const CREDENTIALISH_CSV = `email,password,role
john@example.com,secret-1,admin
jane@example.com,secret-2,user
`;

const PEOPLE_CSV = `ID,Applicant,Email,Has Passport,Wallet Balance,Created At
1000,Ada Lovelace,,No,1200,2025-10-19
1001,Grace Hopper,grace@example.com,Yes,900,2025-10-18
1002,Katherine Johnson,,Yes,3000,2025-10-17
`;

const COUNTRY_CSV = `Applicant,Country,Wallet Balance
Ada Lovelace,Nigeria,1200
Grace Hopper,Ghana,900
Katherine Johnson,Nigeria,3000
Alan Turing,,1800
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
		expect(dataset.rowCount).toBe(18);
	});

	it("does not infer ordinary titles as dates when they contain date text", () => {
		const dataset = buildSheetDataset(parseCsv(SOCIAL_CSV));
		const title = dataset.columns.find((column) => column.id === "title");
		const published = dataset.columns.find(
			(column) => column.id === "published_date",
		);
		expect(title?.kind).toBe("text");
		expect(published?.kind).toBe("date");
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
		expect(result.rows[0]).toEqual({ Region: "LATAM", "SUM Revenue": 13700 });
		expect(result.chart).toMatchObject({
			label: "SUM Revenue",
			groupLabel: "Region",
			labels: ["LATAM", "NA", "EMEA", "APAC"],
			values: [13700, 9050, 8300, 5250],
		});
		expect(result.chartJs).toMatchObject({
			type: "bar",
			data: {
				labels: ["LATAM", "NA", "EMEA", "APAC"],
				datasets: [
					{
						label: "SUM Revenue",
						data: [13700, 9050, 8300, 5250],
					},
				],
			},
		});
	});

	it("keeps realistic show-total prompts on the aggregate route", () => {
		const result = querySheet(
			sampleDataset(),
			"Show total revenue by region for the leadership review",
		);
		expect(result.ok).toBe(true);
		if (!result.ok) return;
		expect(result.queryFrame.routeReason).toBe("aggregate_grouped");
		expect(result.rows[0]).toEqual({ Region: "LATAM", "SUM Revenue": 13700 });
	});

	it("grounds deeper analyst prompts without losing filters or grouping", () => {
		const byRep = querySheet(
			sampleDataset(),
			"Compare overdue revenue by sales rep",
		);
		expect(byRep.ok).toBe(true);
		if (!byRep.ok) return;
		expect(byRep.queryFrame.groupByColumn).toBe("sales_rep");
		expect(byRep.queryFrame.filters).toContainEqual({
			kind: "equals",
			column: "due_status",
			value: "overdue",
		});
		expect(byRep.rows[0]).toEqual({ "Sales Rep": "Ada", "SUM Revenue": 12900 });
		expect(byRep.chartJs).toBeDefined();

		const highRisk = querySheet(
			sampleDataset(),
			"Show high renewal risk LATAM accounts with revenue over 2000",
		);
		expect(highRisk.ok).toBe(true);
		if (!highRisk.ok) return;
		expect(highRisk.queryFrame.operation).toBe("list");
		expect(highRisk.scalar).toBeUndefined();
		expect(highRisk.queryFrame.filters).toEqual(
			expect.arrayContaining([
				{ kind: "equals", column: "region", value: "LATAM" },
				{ kind: "equals", column: "renewal_risk", value: "high" },
				{ kind: "number", column: "revenue", operator: "gt", value: 2000 },
			]),
		);
		expect(highRisk.rows[0]).toMatchObject({
			Customer: "Futura",
			Revenue: 3000,
		});

		const inventory = SHEET_USE_CASES.find(
			(candidate) => candidate.id === "inventory",
		);
		expect(inventory).toBeDefined();
		if (!inventory) return;
		const byCategory = querySheet(
			buildSheetDataset(parseCsv(inventory.csv)),
			"Compare average unit cost by product category",
		);
		expect(byCategory.ok).toBe(true);
		if (!byCategory.ok) return;
		expect(byCategory.queryFrame.groupByColumn).toBe("category");
		expect(byCategory.rows[0]).toEqual({
			Category: "Security",
			"AVG Unit Cost": 165,
		});
	});

	it("answers top-N grouped questions", () => {
		const result = querySheet(sampleDataset(), "top 5 customers by sales");
		expect(result.ok).toBe(true);
		if (!result.ok) return;
		expect(result.rows).toHaveLength(5);
		expect(result.rows[0]?.Customer).toBe("Northstar Energy");
	});

	it("lists top rows by a metric when no target grouping column exists", () => {
		const dataset = buildSheetDataset(parseCsv(SOCIAL_CSV));
		const result = querySheet(dataset, "show top 2 posts by likes");
		expect(result.ok).toBe(true);
		if (!result.ok) return;
		expect(result.queryFrame.operation).toBe("list");
		expect(result.queryFrame.orderBy).toEqual({
			column: "likes",
			direction: "desc",
		});
		expect(result.rows).toHaveLength(2);
		expect(result.rows[0]).toMatchObject({
			Author: "ben",
			Likes: 15,
		});
	});

	it("anchors relative date filters to the latest date in the sheet", () => {
		const dataset = buildSheetDataset(parseCsv(SOCIAL_CSV));
		const result = querySheet(dataset, "total likes this month");
		expect(result.ok).toBe(true);
		if (!result.ok) return;
		expect(result.scalar).toBe(17);
		expect(result.queryFrame.filters).toEqual(
			expect.arrayContaining([
				expect.objectContaining({
					kind: "dateRange",
					column: "published_date",
				}),
			]),
		);
	});

	it("answers average questions with month filters", () => {
		const result = querySheet(sampleDataset(), "average order value in May");
		expect(result.ok).toBe(true);
		if (!result.ok) return;
		expect(result.scalar).toBeCloseTo(1800, 3);
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
		expect(result.scalar).toBe(6);
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
		expect(result.scalar).toBe(5);
	});

	it("runs every packaged practical use-case query without custom demo code", () => {
		for (const useCase of SHEET_USE_CASES) {
			const dataset = buildSheetDataset(parseCsv(useCase.csv));
			expect(dataset.rowCount, `${useCase.id} row count`).toBeGreaterThan(12);
			expect(
				dataset.columns.length,
				`${useCase.id} column count`,
			).toBeGreaterThanOrEqual(8);
			for (const question of useCase.questions) {
				const result = querySheet(dataset, question);
				expect(result.ok, `${useCase.id}: ${question}`).toBe(true);
			}
		}
	});

	it("returns expected results for every packaged practical use-case query", () => {
		const expectations: Record<string, Record<string, unknown>> = {
			"revenue_ops::Compare total revenue by region for the pipeline review": {
				first: { Region: "LATAM", "SUM Revenue": 13700 },
			},
			"revenue_ops::Compare unpaid overdue revenue by region": {
				first: { Region: "NA", "SUM Revenue": 5900 },
				length: 2,
			},
			"revenue_ops::Compare average order value by segment for active accounts":
				{
					first: {
						Segment: "Enterprise",
						"AVG Order Value": 3216.6666666666665,
					},
					length: 3,
				},
			"revenue_ops::What are the top 5 customers by revenue?": {
				first: { Customer: "Northstar Energy", "SUM Revenue": 5200 },
				length: 5,
			},
			"revenue_ops::Show high renewal risk LATAM accounts with revenue over 2000":
				{
					first: { Customer: "Futura", Revenue: 3000 },
					length: 2,
				},
			"revenue_ops::How many active annual accounts are high renewal risk?": {
				scalar: 3,
			},
			"support_queue::Which team has the most open high priority tickets?": {
				first: { Team: "Billing", Count: 3 },
				length: 1,
			},
			"support_queue::Compare average response time hours by team for breached SLA tickets":
				{
					first: {
						Team: "Platform",
						"AVG Response Time Hours": 10,
					},
					length: 3,
				},
			"support_queue::Show open high priority tickets with response time over 8":
				{
					first: { "Ticket ID": "T-1003", Priority: "high" },
					length: 3,
				},
			"support_queue::Compare average satisfaction score by product area for closed tickets":
				{
					first: {
						"Product Area": "API",
						"AVG Satisfaction Score": 5,
					},
					length: 6,
				},
			"support_queue::How many open tickets breached first reply SLA?": {
				scalar: 7,
			},
			"inventory::Compare low stock units on hand by warehouse": {
				first: { Warehouse: "East", "SUM Units On Hand": 30 },
				length: 3,
			},
			"inventory::Compare total units on hand by category for East warehouse": {
				first: {
					Category: "Backup",
					"SUM Units On Hand": 178,
				},
				length: 3,
			},
			"inventory::Show low stock products with units on hand below 15": {
				first: { Product: "Security Shield", "Units On Hand": 12 },
				length: 4,
			},
			"inventory::Which supplier has the highest average unit cost?": {
				first: { Supplier: "IronGate", "AVG Unit Cost": 165 },
				length: 1,
			},
			"inventory::How many security products are backordered?": { scalar: 1 },
			"marketing_campaigns::Compare active campaign spend by channel": {
				first: { Channel: "Search", "SUM Spend": 4750 },
				length: 4,
			},
			"marketing_campaigns::Compare average leads by region for active campaigns":
				{
					first: { Region: "APAC", "AVG Leads": 48.5 },
					length: 4,
				},
			"marketing_campaigns::Show active campaigns with conversions over 10": {
				first: { Campaign: "Launch A", Conversions: 11 },
				length: 6,
			},
			"marketing_campaigns::Compare total conversions by owner": {
				first: { Owner: "Max", "SUM Conversions": 44 },
				length: 4,
			},
			"marketing_campaigns::How many paused Search campaigns are there?": {
				scalar: 1,
			},
			"applicant_kyc::Compare total wallet balance by country for approved applicants":
				{
					first: { Country: "United States", "SUM Wallet Balance": 4580 },
					length: 5,
				},
			"applicant_kyc::How many high risk applicants by reviewer?": {
				first: { Reviewer: "Mina", Count: 3 },
				length: 1,
			},
			"applicant_kyc::Show review applicants with wallet balance over 1500": {
				first: { Applicant: "Hedy Lamarr", "Wallet Balance": 1750 },
				length: 2,
			},
			"applicant_kyc::How many approved applicants have passport?": {
				scalar: 5,
			},
			"applicant_kyc::Show partner applicants not from Nigeria": {
				first: { Applicant: "Alan Turing" },
				length: 4,
			},
			"applicant_kyc::What is the maximum wallet balance in the applicant pool?":
				{
					scalar: 4100,
				},
			"engineering_bom::Compare total cost by subsystem for high criticality components":
				{
					first: { Subsystem: "EHD Subsystem", "SUM Cost": 22000 },
					length: 4,
				},
			"engineering_bom::Compare average lead time days by sourcing option": {
				first: { Sourcing: "Machine shop", "AVG Lead Time Days": 18 },
				length: 9,
			},
			"engineering_bom::Show high criticality components with lead time days over 10":
				{
					first: {
						Item: "1",
						Component: "HV Power Module",
						"Lead Time Days": 21,
					},
					length: 1,
				},
			"engineering_bom::Compare total quantity by subsystem for hardware store components":
				{
					first: {
						Subsystem: "Assembly / Maintenance",
						"SUM Quantity": 24,
					},
					length: 3,
				},
			"engineering_bom::How many online components are high criticality?": {
				scalar: 3,
			},
			"engineering_bom::Which component is needed in the highest quantity?": {
				first: {
					Component: "M3 Stainless Screws",
					"MAX Quantity": 24,
					Quantity: "24",
				},
				length: 1,
			},
			"clinic_visits::Compare completed visit cost by department": {
				first: { Department: "Orthopedics", "SUM Visit Cost": 930 },
				length: 4,
			},
			"clinic_visits::Compare average wait time minutes by department": {
				first: {
					Department: "Orthopedics",
					"AVG Wait Time Minutes": 54,
				},
				length: 4,
			},
			"clinic_visits::Show completed visits with wait time minutes over 40": {
				first: { "Wait Time Minutes": 45, "Visit Date": "2024-05-08" },
				length: 4,
			},
			"clinic_visits::Which provider has the highest average wait time minutes?":
				{
					first: { Provider: "Dr Stone", "AVG Wait Time Minutes": 54 },
					length: 1,
				},
			"clinic_visits::Compare no-show patients by department": {
				first: { Department: "Pediatrics", Count: 1 },
				length: 3,
			},
			"clinic_visits::How many private insurance appointments were completed?":
				{
					scalar: 7,
				},
		};

		for (const useCase of SHEET_USE_CASES) {
			const dataset = buildSheetDataset(parseCsv(useCase.csv));
			for (const question of useCase.questions) {
				const result = querySheet(dataset, question);
				expect(result.ok, `${useCase.id}: ${question}`).toBe(true);
				if (!result.ok) continue;
				if (result.queryFrame.resultShape === "categorical_chart") {
					expect(result.chartJs, `${useCase.id}: ${question}`).toBeDefined();
					expect(result.chartJs?.data.labels.length).toBe(result.rows.length);
				}
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
		expect(result.rows).toHaveLength(6);
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
		const suggestions = suggestSheetQuestions(dataset);
		expect(suggestions).toEqual(
			expect.arrayContaining([
				"Compare total spend by channel",
				"What are the top 5 campaigns by spend?",
			]),
		);
		for (const question of suggestions) {
			expect(querySheet(dataset, question).ok, question).toBe(true);
		}

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

	it("avoids sparse display columns in suggestions", () => {
		const dataset = buildSheetDataset(parseCsv(TRIP_LIKE_CSV));
		expect(suggestSheetQuestions(dataset)).toEqual(
			expect.arrayContaining([
				"What are the top 5 applicants by wallet balance?",
			]),
		);
		expect(suggestSheetQuestions(dataset)).not.toEqual(
			expect.arrayContaining([
				"What are the top 5 partner names by wallet balance?",
			]),
		);
	});

	it("does not project sensitive columns by default", () => {
		const dataset = buildSheetDataset(parseCsv(CREDENTIALISH_CSV));
		const result = querySheet(dataset, "show first 2 rows");
		expect(result.ok).toBe(true);
		if (!result.ok) return;
		expect(result.rows[0]).toMatchObject({ email: "john@example.com" });
		expect(result.rows[0]).not.toHaveProperty("password");
	});

	it("filters rows by missing fields with useful row context", () => {
		const dataset = buildSheetDataset(parseCsv(PEOPLE_CSV));
		const result = querySheet(
			dataset,
			"show applicants where email is missing",
		);
		expect(result.ok).toBe(true);
		if (!result.ok) return;
		expect(result.queryFrame.filters).toEqual(
			expect.arrayContaining([
				{ kind: "presence", column: "email", present: false },
			]),
		);
		expect(result.rows).toHaveLength(2);
		expect(result.rows[0]).toMatchObject({
			Applicant: "Ada Lovelace",
			Email: null,
		});
	});

	it("maps without/with wording to boolean and numeric filters", () => {
		const dataset = buildSheetDataset(parseCsv(PEOPLE_CSV));
		const withoutPassport = querySheet(
			dataset,
			"show applicants without passport",
		);
		expect(withoutPassport.ok).toBe(true);
		if (!withoutPassport.ok) return;
		expect(withoutPassport.queryFrame.filters).toContainEqual({
			kind: "equals",
			column: "has_passport",
			value: false,
		});
		expect(withoutPassport.rows[0]).toMatchObject({
			Applicant: "Ada Lovelace",
		});

		const overBalance = querySheet(
			dataset,
			"show applicants with wallet balance over 1000",
		);
		expect(overBalance.ok).toBe(true);
		if (!overBalance.ok) return;
		expect(overBalance.queryFrame.filters).toContainEqual({
			kind: "number",
			column: "wallet_balance",
			operator: "gt",
			value: 1000,
		});
		expect(overBalance.queryFrame.filters).not.toContainEqual({
			kind: "equals",
			column: "id",
			value: "1000",
		});
		expect(overBalance.rows).toHaveLength(2);
	});

	it("uses sorted row lists for explicit high-cardinality show-top prompts", () => {
		const dataset = buildSheetDataset(parseCsv(PEOPLE_CSV));
		const result = querySheet(
			dataset,
			"show top 2 applicants by wallet balance",
		);
		expect(result.ok).toBe(true);
		if (!result.ok) return;
		expect(result.queryFrame.operation).toBe("list");
		expect(result.queryFrame.orderBy).toEqual({
			column: "wallet_balance",
			direction: "desc",
		});
		expect(result.rows[0]).toMatchObject({
			Applicant: "Katherine Johnson",
			"Wallet Balance": 3000,
		});
	});

	it("counts and lists distinct non-empty column values", () => {
		const dataset = buildSheetDataset(parseCsv(COUNTRY_CSV));
		const count = querySheet(dataset, "how many unique countries are there?");
		expect(count.ok).toBe(true);
		if (!count.ok) return;
		expect(count.queryFrame.aggregate).toBe("distinctCount");
		expect(count.scalar).toBe(2);

		const list = querySheet(dataset, "list countries");
		expect(list.ok).toBe(true);
		if (!list.ok) return;
		expect(list.queryFrame.routeReason).toBe("distinct_list");
		expect(list.rows).toEqual([{ Country: "Nigeria" }, { Country: "Ghana" }]);
	});

	it("supports negative equality filters and numeric ranges", () => {
		const dataset = buildSheetDataset(parseCsv(COUNTRY_CSV));
		const notNigeria = querySheet(dataset, "show applicants not from Nigeria");
		expect(notNigeria.ok).toBe(true);
		if (!notNigeria.ok) return;
		expect(notNigeria.queryFrame.filters).toContainEqual({
			kind: "notEquals",
			column: "country",
			value: "Nigeria",
		});
		expect(notNigeria.rows).toEqual(
			expect.arrayContaining([
				expect.objectContaining({ Applicant: "Grace Hopper" }),
			]),
		);
		expect(notNigeria.rows).not.toEqual(
			expect.arrayContaining([
				expect.objectContaining({ Applicant: "Ada Lovelace" }),
			]),
		);
		expect(notNigeria.rows).not.toEqual(
			expect.arrayContaining([
				expect.objectContaining({ Applicant: "Alan Turing" }),
			]),
		);

		const between = querySheet(
			dataset,
			"show applicants with wallet balance between 1000 and 2000",
		);
		expect(between.ok).toBe(true);
		if (!between.ok) return;
		expect(between.queryFrame.filters).toEqual(
			expect.arrayContaining([
				{
					kind: "number",
					column: "wallet_balance",
					operator: "gte",
					value: 1000,
				},
				{
					kind: "number",
					column: "wallet_balance",
					operator: "lte",
					value: 2000,
				},
			]),
		);
		expect(between.rows).toHaveLength(2);
	});

	it("keeps scalar max/min questions scalar when no target column is named", () => {
		const dataset = buildSheetDataset(parseCsv(COUNTRY_CSV));
		const result = querySheet(dataset, "what is the maximum wallet balance?");
		expect(result.ok).toBe(true);
		if (!result.ok) return;
		expect(result.queryFrame.resultShape).toBe("scalar_metric");
		expect(result.scalar).toBe(3000);
	});

	it("answers BOM quantity superlatives with mixed quantity units", () => {
		const dataset = buildSheetDataset(parseCsv(BOM_CSV));
		const item = dataset.columns.find((column) => column.id === "item");
		const quantity = dataset.columns.find((column) => column.id === "quantity");
		expect(item?.roles).not.toContain("measure");
		expect(quantity?.roles).toContain("measure");
		expect(suggestSheetQuestions(dataset)).toEqual(
			expect.arrayContaining(["What are the top 5 components by quantity?"]),
		);

		const result = querySheet(
			dataset,
			"what is the item needed in most quantity?",
		);
		expect(result.ok).toBe(true);
		if (!result.ok) return;
		expect(result.queryFrame.measureColumn).toBe("quantity");
		expect(result.queryFrame.groupByColumn).toBe("component");
		expect(result.queryFrame.limit).toBe(1);
		expect(result.rows[0]).toEqual({
			Component: "M3 Stainless Screws",
			"MAX Quantity": 24,
			Quantity: "24",
		});
	});
});
