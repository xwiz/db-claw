/**
 * Laravel config semantic catalog scanner.
 *
 * This scanner turns static application constants into enum vocabulary, e.g.
 * `config/constants.php` `main_reasons => ['R' => 'Rate (STR)']` becomes a
 * graph mapping from "rate" to `transactions.main_reason:R`.
 */

import { promises as fs } from "node:fs";
import path from "node:path";
import {
	SanitiserError,
	SourceLayer,
	type VocabFragment,
	sanitiseLabel,
} from "@semsql/extractor-sdk";

export interface ConfigScanResult {
	fragments: VocabFragment[];
	skipped: Array<{ file: string; reason: string }>;
}

interface EnumConstantEntry {
	key: string;
	rawValue: string;
	label: string;
	line: number;
}

interface EnumTargetEntry extends EnumConstantEntry {
	field: string;
}

interface ArraySpan {
	body: string;
	line: number;
}

const CONFIG_CONSTANTS = "config/constants.php";
const CONFIG_AI = "config/ai.php";
const PHP_STRING_MAP_RX =
	/(?:'((?:\\.|[^'\\])*)'|"((?:\\.|[^"\\])*)"|(-?\d+))\s*=>\s*(?:'((?:\\.|[^'\\])*)'|"((?:\\.|[^"\\])*)"|(-?\d+))/g;
const PHP_RETURNED_REGEX_RX =
	/['"]\/((?:\\.|[^/"'])*)\/[a-z]*['"]\s*=>\s*['"]([^'"]+)['"]/g;

export async function scanLaravelConfig(
	root: string,
	fieldCanonicals: Iterable<string>,
): Promise<ConfigScanResult> {
	const result: ConfigScanResult = { fragments: [], skipped: [] };
	const fields = new Set(Array.from(fieldCanonicals));
	const constantsPath = path.join(root, CONFIG_CONSTANTS);
	const aiPath = path.join(root, CONFIG_AI);

	const enumEntries: EnumTargetEntry[] = [];
	let constantsText = "";
	try {
		constantsText = await fs.readFile(constantsPath, "utf8");
	} catch {
		constantsText = "";
	}

	if (constantsText) {
		for (const constantName of constantNames(constantsText)) {
			const targets = targetFieldsForConstant(constantName, fields);
			if (targets.length === 0) {
				continue;
			}
			const entries = parsePhpConstantStringMap(constantsText, constantName);
			if (entries.length === 0) {
				continue;
			}
			for (const field of targets) {
				for (const entry of entries) {
					const targetEntry: EnumTargetEntry = { ...entry, field };
					enumEntries.push(targetEntry);
					emitEnumTerms(result, CONFIG_CONSTANTS, targetEntry, 0.84);
				}
			}
		}
	}

	try {
		const aiText = await fs.readFile(aiPath, "utf8");
		emitSynonymEnumTerms(result, CONFIG_AI, aiText, enumEntries);
		emitSynonymFieldTerms(result, CONFIG_AI, aiText, fields);
	} catch {
		// Optional config file.
	}

	return result;
}

export async function readLaravelConfigScalarConstants(
	root: string,
): Promise<Map<string, string>> {
	const out = new Map<string, string>();
	const constantsPath = path.join(root, CONFIG_CONSTANTS);
	let constantsText = "";
	try {
		constantsText = await fs.readFile(constantsPath, "utf8");
	} catch {
		return out;
	}
	for (const constantName of constantNames(constantsText)) {
		for (const entry of parsePhpConstantStringMap(
			constantsText,
			constantName,
		)) {
			out.set(`constants.${constantName}.${entry.key}`, entry.rawValue);
		}
	}
	return out;
}

function constantNames(text: string): string[] {
	const out = new Set<string>();
	const rx = /['"]([A-Za-z_][A-Za-z0-9_]*)['"]\s*=>\s*(?:\[|array\s*\()/g;
	let match = rx.exec(text);
	while (match !== null) {
		out.add(match[1]!);
		match = rx.exec(text);
	}
	return Array.from(out).sort();
}

function parsePhpConstantStringMap(
	text: string,
	constantName: string,
): EnumConstantEntry[] {
	const span = findPhpArraySpan(text, constantName);
	if (!span) {
		return [];
	}
	const out: EnumConstantEntry[] = [];
	PHP_STRING_MAP_RX.lastIndex = 0;
	let match = PHP_STRING_MAP_RX.exec(span.body);
	while (match !== null) {
		const key = unescapePhp(match[1] ?? match[2] ?? match[3] ?? "");
		const value = unescapePhp(match[4] ?? match[5] ?? match[6] ?? "");
		const mapped = enumEntryFromPair(key, value);
		if (mapped) {
			out.push({
				key,
				rawValue: mapped.rawValue,
				label: mapped.label,
				line: span.line + lineOf(span.body, match.index) - 1,
			});
		}
		match = PHP_STRING_MAP_RX.exec(span.body);
	}
	return out;
}

function enumEntryFromPair(
	key: string,
	value: string,
): { rawValue: string; label: string } | null {
	if (!key || !value) {
		return null;
	}
	if (looksLikeHumanLabel(value)) {
		return { rawValue: key, label: value };
	}
	if (looksLikeHumanLabel(key)) {
		return { rawValue: value, label: prettyConstantTerm(key) };
	}
	return null;
}

function findPhpArraySpan(text: string, key: string): ArraySpan | null {
	const keyRx = new RegExp(
		`['"]${escapeRegex(key)}['"]\\s*=>\\s*(\\[|array\\s*\\()`,
		"g",
	);
	const match = keyRx.exec(text);
	if (!match) {
		return null;
	}
	const opener = match[1]!.startsWith("[") ? "[" : "(";
	const openerIndex = match.index + match[0].lastIndexOf(opener);
	const closer = opener === "[" ? "]" : ")";
	let depth = 0;
	let quote: "'" | '"' | null = null;
	let escaped = false;
	let lineComment = false;
	let blockComment = false;
	for (let i = openerIndex; i < text.length; i += 1) {
		const ch = text[i]!;
		const next = text[i + 1] ?? "";
		if (lineComment) {
			if (ch === "\n") lineComment = false;
			continue;
		}
		if (blockComment) {
			if (ch === "*" && next === "/") {
				blockComment = false;
				i += 1;
			}
			continue;
		}
		if (quote) {
			if (escaped) {
				escaped = false;
				continue;
			}
			if (ch === "\\") {
				escaped = true;
				continue;
			}
			if (ch === quote) quote = null;
			continue;
		}
		if (ch === "/" && next === "/") {
			lineComment = true;
			i += 1;
			continue;
		}
		if (ch === "/" && next === "*") {
			blockComment = true;
			i += 1;
			continue;
		}
		if (ch === "'" || ch === '"') {
			quote = ch;
			continue;
		}
		if (ch === opener) {
			depth += 1;
			continue;
		}
		if (ch === closer) {
			depth -= 1;
			if (depth === 0) {
				return {
					body: text.slice(openerIndex + 1, i),
					line: lineOf(text, openerIndex),
				};
			}
		}
	}
	return null;
}

function targetFieldsForConstant(
	constantName: string,
	fields: Set<string>,
): string[] {
	const candidates = new Set<string>([
		constantName,
		singularConstantName(constantName),
	]);
	for (const suffix of [
		"_statuses",
		"_status",
		"_types",
		"_type",
		"_names",
		"_details",
		"_reasons",
		"_reason",
	]) {
		if (constantName.endsWith(suffix)) {
			candidates.add(constantName.slice(0, -suffix.length));
			candidates.add(
				singularConstantName(constantName.slice(0, -suffix.length)),
			);
		}
	}
	return Array.from(fields)
		.filter((field) => {
			const column = field.split(".").pop() ?? field;
			return candidates.has(column);
		})
		.sort();
}

function singularConstantName(name: string): string {
	if (name.endsWith("statuses")) return `${name.slice(0, -8)}status`;
	if (name.endsWith("ies")) return `${name.slice(0, -3)}y`;
	if (name.endsWith("ses")) return name.slice(0, -2);
	if (name.endsWith("s")) return name.slice(0, -1);
	return name;
}

function emitEnumTerms(
	result: ConfigScanResult,
	file: string,
	entry: EnumTargetEntry,
	confidence: number,
): void {
	for (const term of labelTerms(entry.label)) {
		emitEnumTerm(result, file, entry, term, confidence);
	}
}

function emitSynonymEnumTerms(
	result: ConfigScanResult,
	file: string,
	text: string,
	enumEntries: EnumTargetEntry[],
): void {
	const aliasesByConcept = synonymAliasesByConcept(text);
	for (const [concept, aliases] of aliasesByConcept) {
		for (const entry of enumEntriesForConcept(concept, enumEntries)) {
			for (const alias of aliases) {
				emitEnumTerm(result, file, entry, alias, 0.74);
			}
		}
	}
}

function emitSynonymFieldTerms(
	result: ConfigScanResult,
	file: string,
	text: string,
	fields: Set<string>,
): void {
	const aliasesByConcept = synonymAliasesByConcept(text);
	for (const [concept, aliases] of aliasesByConcept) {
		for (const field of fieldTargetsForConcept(concept, fields)) {
			for (const alias of aliases) {
				emitFieldTerm(result, file, field, alias, 0.68);
			}
		}
	}
}

function synonymAliasesByConcept(text: string): Map<string, Set<string>> {
	const out = new Map<string, Set<string>>();
	PHP_RETURNED_REGEX_RX.lastIndex = 0;
	let match = PHP_RETURNED_REGEX_RX.exec(text);
	while (match !== null) {
		const pattern = match[1]!;
		const concept = normaliseTerm(unescapePhp(match[2]!));
		if (concept) {
			let aliases = out.get(concept);
			if (!aliases) {
				aliases = new Set<string>();
				out.set(concept, aliases);
			}
			for (const alias of aliasesFromRegexPattern(pattern)) {
				aliases.add(alias);
			}
		}
		match = PHP_RETURNED_REGEX_RX.exec(text);
	}
	return out;
}

function enumEntriesForConcept(
	concept: string,
	enumEntries: EnumTargetEntry[],
): EnumTargetEntry[] {
	if (concept === "velocity") {
		return enumEntries.filter(
			(entry) =>
				normaliseTerm(entry.label).includes("rate") ||
				entry.rawValue.toUpperCase() === "R",
		);
	}
	return enumEntries.filter((entry) =>
		labelTerms(entry.label).includes(concept),
	);
}

function fieldTargetsForConcept(
	concept: string,
	fields: Set<string>,
): string[] {
	const conceptTokens = semanticFieldConceptTokens(concept);
	if (conceptTokens.length === 0) {
		return [];
	}
	const out: string[] = [];
	for (const field of fields) {
		const column = field.split(".").pop() ?? field;
		const fieldTokens = fieldNameTokens(column);
		if (fieldTokens.length === 0 || graphUnsafeFieldAliasTarget(fieldTokens)) {
			continue;
		}
		if (conceptMatchesField(concept, conceptTokens, fieldTokens)) {
			out.push(field);
		}
	}
	return out.sort();
}

function conceptMatchesField(
	concept: string,
	conceptTokens: string[],
	fieldTokens: string[],
): boolean {
	const tokenSet = new Set(fieldTokens);
	if (concept === "total amount") {
		return tokenSet.has("amount") && !tokenSet.has("id");
	}
	if (concept === "beneficiary") {
		return ["beneficiary", "recipient", "payee", "destination"].some((token) =>
			tokenSet.has(token),
		);
	}
	if (concept === "by bank") {
		return tokenSet.has("bank");
	}
	return conceptTokens.every((token) => tokenSet.has(token));
}

function semanticFieldConceptTokens(concept: string): string[] {
	return normaliseTerm(concept)
		.split(" ")
		.filter(
			(token) =>
				token.length >= 2 &&
				!["total", "sum", "average", "avg", "mean", "by", "per"].includes(
					token,
				),
		);
}

function fieldNameTokens(field: string): string[] {
	return normaliseTerm(field)
		.split(" ")
		.filter((token) => token.length >= 2);
}

function graphUnsafeFieldAliasTarget(fieldTokens: string[]): boolean {
	const tokenSet = new Set(fieldTokens);
	return (
		tokenSet.has("password") || tokenSet.has("token") || tokenSet.has("secret")
	);
}

function emitEnumTerm(
	result: ConfigScanResult,
	file: string,
	entry: EnumTargetEntry,
	term: string,
	confidence: number,
): void {
	try {
		const cleanTerm = sanitiseLabel(term).toLowerCase();
		result.fragments.push({
			term: cleanTerm,
			canonical: {
				kind: "enum_value",
				enumName: entry.field,
				rawValue: entry.rawValue,
			},
			confidence,
			locator: {
				file,
				line: entry.line,
				layer: SourceLayer.AppConstant,
				extractor: "extractor-laravel:config",
			},
		});
	} catch (e) {
		if (e instanceof SanitiserError) {
			result.skipped.push({ file, reason: e.message });
			return;
		}
		throw e;
	}
}

function emitFieldTerm(
	result: ConfigScanResult,
	file: string,
	field: string,
	term: string,
	confidence: number,
): void {
	try {
		const cleanTerm = sanitiseLabel(term).toLowerCase();
		result.fragments.push({
			term: cleanTerm,
			canonical: {
				kind: "field",
				field,
			},
			confidence,
			locator: {
				file,
				line: 1,
				layer: SourceLayer.AppConstant,
				extractor: "extractor-laravel:config",
			},
		});
	} catch (e) {
		if (e instanceof SanitiserError) {
			result.skipped.push({ file, reason: e.message });
			return;
		}
		throw e;
	}
}

function labelTerms(label: string): string[] {
	const out = new Set<string>();
	const clean = normaliseTerm(label);
	if (clean) out.add(clean);
	const withoutParens = normaliseTerm(label.replace(/\([^)]*\)/g, " "));
	if (withoutParens) out.add(withoutParens);
	for (const part of label.matchAll(/\(([^)]*)\)/g)) {
		const term = normaliseTerm(part[1]!);
		if (term) out.add(term);
	}
	return Array.from(out).sort();
}

function aliasesFromRegexPattern(pattern: string): string[] {
	const simplified = pattern.replace(/\([^|()]*\)\?/g, "");
	const groups = Array.from(simplified.matchAll(/\(([^()]*\|[^()]*)\)/g)).map(
		(match) => match[1]!,
	);
	const raw =
		groups.length > 0
			? groups.flatMap((group) => group.split("|"))
			: [simplified];
	const out = new Set<string>();
	for (const piece of raw) {
		const cleaned = cleanRegexAlias(piece);
		if (!cleaned) continue;
		out.add(cleaned);
		if (cleaned.includes("-")) out.add(cleaned.replace(/-/g, " "));
	}
	return Array.from(out).sort();
}

function cleanRegexAlias(raw: string): string {
	return normaliseTerm(
		raw
			.replace(/\\b/g, " ")
			.replace(/\\s[+*?]/g, " ")
			.replace(/\\-\?/g, "-")
			.replace(/\\-/g, "-")
			.replace(/\([^)]*\)\?/g, "")
			.replace(/[?^$]/g, " ")
			.replace(/\\([A-Za-z])/g, "$1")
			.replace(/[^A-Za-z0-9_ -]+/g, " "),
	);
}

function looksLikeHumanLabel(value: string): boolean {
	return /[A-Za-z]/.test(value) && normaliseTerm(value).length >= 2;
}

function prettyConstantTerm(value: string): string {
	return value.replace(/_/g, " ");
}

function normaliseTerm(value: string): string {
	return value
		.normalize("NFC")
		.toLowerCase()
		.replace(/[_-]+/g, " ")
		.replace(/\s+/g, " ")
		.trim();
}

function unescapePhp(value: string): string {
	return value.replace(/\\'/g, "'").replace(/\\"/g, '"').replace(/\\\\/g, "\\");
}

function lineOf(text: string, index: number): number {
	let line = 1;
	for (let i = 0; i < index; i += 1) {
		if (text.charCodeAt(i) === 10) line += 1;
	}
	return line;
}

function escapeRegex(value: string): string {
	return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}
