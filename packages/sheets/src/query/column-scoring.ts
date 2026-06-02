import { hasPhrase, normalizeText, singularize, words } from "../normalize.js";
import type { SheetColumn } from "../types.js";
import type { RouteContext } from "./context.js";

const MEASURE_SYNONYMS: Record<string, string[]> = {
	revenue: ["revenue", "sales", "amount", "invoice amount", "order value"],
	sales: ["sales", "revenue", "amount", "invoice amount", "order value"],
	amount: ["amount", "invoice amount", "order value", "revenue", "sales"],
	value: ["value", "order value", "amount"],
	cost: ["cost", "price", "unit cost", "spend", "budget"],
	spend: ["spend", "cost", "budget", "amount"],
	units: ["units", "quantity", "qty", "stock", "inventory"],
	clicks: ["clicks", "click", "visits", "views", "impressions"],
};

const DIMENSION_SYNONYMS: Record<string, string[]> = {
	customer: [
		"customer",
		"customers",
		"client",
		"clients",
		"account",
		"accounts",
		"company",
	],
	account: [
		"account",
		"accounts",
		"customer",
		"customers",
		"client",
		"clients",
	],
	region: [
		"region",
		"regions",
		"market",
		"markets",
		"territory",
		"territories",
	],
	product: [
		"product",
		"products",
		"component",
		"components",
		"part",
		"parts",
		"item",
		"items",
	],
	campaign: ["campaign", "campaigns", "initiative", "initiatives"],
	channel: ["channel", "channels", "source", "sources"],
	status: ["status", "state"],
	rep: ["rep", "sales rep", "owner"],
	team: ["team", "teams", "department", "departments"],
	warehouse: ["warehouse", "warehouses", "location", "locations"],
};

const LABEL_WORD_STOPWORDS = new Set([
	"has",
	"have",
	"is",
	"are",
	"requires",
	"required",
	"total",
	"count",
	"sum",
	"avg",
	"average",
]);

export function aliasesFor(column: SheetColumn): string[] {
	const label = normalizeText(column.label);
	const id = normalizeText(column.id.replace(/_/g, " "));
	const aliases = new Set([label, id, singularize(label), singularize(id)]);
	for (const token of [...words(label), ...words(id)]) {
		if (token.length > 2 && !LABEL_WORD_STOPWORDS.has(token)) {
			aliases.add(token);
			aliases.add(singularize(token));
		}
	}
	for (const [concept, terms] of Object.entries(DIMENSION_SYNONYMS)) {
		if (terms.some((term) => hasPhrase(label, term) || hasPhrase(id, term))) {
			aliases.add(concept);
			for (const term of terms) aliases.add(term);
		}
	}
	return [...aliases].filter((alias) => alias.length > 0);
}

function synonymScore(ctx: RouteContext, column: SheetColumn): number {
	const columnText = normalizeText(
		`${column.label} ${column.id.replace(/_/g, " ")}`,
	);
	let score = 0;
	if (column.roles.includes("measure")) {
		for (const [concept, terms] of Object.entries(MEASURE_SYNONYMS)) {
			const questionMatches =
				hasPhrase(ctx.normalized, concept) ||
				terms.some((term) => hasPhrase(ctx.normalized, term));
			if (!questionMatches) continue;
			for (const term of terms) {
				if (hasPhrase(columnText, term)) {
					if (term === concept) score += 8;
					else if (term === "revenue" || term === "sales") score += 6;
					else score += 2;
				}
			}
		}
	}
	return score;
}

export function scoreColumn(ctx: RouteContext, column: SheetColumn): number {
	let score = synonymScore(ctx, column);
	for (const alias of aliasesFor(column)) {
		if (hasPhrase(ctx.normalized, alias)) score += alias.includes(" ") ? 12 : 8;
		const aliasWords = words(alias);
		if (
			aliasWords.length > 1 &&
			aliasWords.every(
				(word) =>
					ctx.questionWords.includes(word) ||
					ctx.singularQuestionWords.includes(word),
			)
		) {
			score += aliasWords.length * 2;
		}
		if (
			aliasWords.length === 1 &&
			ctx.singularQuestionWords.includes(aliasWords[0]!)
		) {
			score += 6;
		}
	}
	return score;
}
