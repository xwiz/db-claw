/**
 * Eloquent model walker.
 *
 * Eloquent models are the second-highest fidelity vocabulary source in a
 * Laravel codebase, behind only Filament UI labels. From each model we can
 * derive:
 *
 *  - **`$table`** — the authoritative DB table name. When present, this
 *    overrides the convention-based pluralisation in `filament.ts` so that
 *    edge cases (irregular plurals, namespace overrides, legacy snake-case
 *    quirks) resolve correctly.
 *  - **`$fillable`** — declares which columns are mass-assignable. We treat
 *    every fillable name as a confirmed field on the entity.
 *  - **`$casts`** — column → cast-type map. The cast (`bool`, `array`,
 *    `datetime`, …) lets Stage 3 (slot filler) pick the right literal form
 *    without guessing.
 *
 * Layer assignment: ORM (= 2). Higher than DB schema, lower than i18n /
 * Filament — those still win when they disagree.
 *
 * Relationships (`$this->hasMany(...)`, `belongsTo(...)`, etc.) are
 * emitted as `relationship` fragments by the PHP AST walker; the
 * v0.2 cut is intentionally property-only because relationship methods
 * require expression-level analysis.
 *
 * The walker also exposes a **class-to-table index** that `filament.ts`
 * consults before falling back to convention pluralisation — this is the
 * mechanism by which `$table = 'students'` on `User::class` flows through
 * to a Filament Resource referencing `User::class`.
 */

import { promises as fs } from "node:fs";
import path from "node:path";
import {
	SanitiserError,
	SourceLayer,
	type VocabFragment,
	sanitiseCanonical,
	sanitiseLabel,
} from "@semsql/extractor-sdk";
import { readLaravelConfigScalarConstants } from "./config.js";

/** Result of one walk over Eloquent models. */
export interface EloquentScanResult {
	fragments: VocabFragment[];
	/** Files we recognised as model sources but couldn't fully parse. */
	skipped: Array<{ file: string; reason: string }>;
	/**
	 * Map of `<Namespace\Class>` (and bare class basename, both keys for
	 * convenience) → canonical entity name. Filament uses this to resolve
	 * `$model = User::class` to a real table name when `$table` overrides
	 * the convention.
	 */
	classToEntity: Map<string, string>;
	/**
	 * Per-entity `$casts` map. Surfaced for downstream Stage 3 typing —
	 * unused in v0.2 but populated so callers can pre-bind it.
	 */
	castsByEntity: Map<string, Record<string, string>>;
}

const MODEL_BASE_CLASSES = new Set([
	"Model",
	"BaseModel",
	"Authenticatable",
	"Pivot",
	"MorphPivot",
]);

/**
 * Recursively scan `root/app/` for files declaring a class that extends
 * one of {@link MODEL_BASE_CLASSES}. Standard Laravel layouts put models
 * in `app/Models/`; legacy projects keep them at `app/`. The recursive
 * walk handles both without special-casing.
 */
export async function scanEloquentModels(
	root: string,
): Promise<EloquentScanResult> {
	const result: EloquentScanResult = {
		fragments: [],
		skipped: [],
		classToEntity: new Map(),
		castsByEntity: new Map(),
	};
	const configConstants = await readLaravelConfigScalarConstants(root);
	await walk(path.join(root, "app"), result, configConstants);
	return result;
}

async function walk(
	dir: string,
	result: EloquentScanResult,
	configConstants: Map<string, string>,
): Promise<void> {
	let entries: string[];
	try {
		entries = await fs.readdir(dir);
	} catch {
		return;
	}
	for (const entry of entries) {
		const full = path.join(dir, entry);
		const stat = await fs.stat(full).catch(() => null);
		if (!stat) continue;
		if (stat.isDirectory()) {
			// Skip well-known non-model directories to keep the walk
			// proportional to the model count, not the codebase size.
			if (entry === "Filament" || entry === "Http" || entry === "Console")
				continue;
			await walk(full, result, configConstants);
		} else if (entry.endsWith(".php")) {
			await scanModelFile(full, result, configConstants);
		}
	}
}

async function scanModelFile(
	file: string,
	result: EloquentScanResult,
	configConstants: Map<string, string>,
): Promise<void> {
	const text = await fs.readFile(file, "utf8");
	const cls = parseClassDeclaration(text);
	if (!cls) return;
	if (!extendsAnyOf(text, MODEL_BASE_CLASSES)) return;

	const props = parseModelProperties(text, configConstants);
	const conventionEntity = classNameToTable(cls.name);
	const entity = props.table ?? conventionEntity;
	if (!entity) {
		result.skipped.push({
			file,
			reason: `cannot derive entity for class ${cls.name}`,
		});
		return;
	}

	let canonicalEntity: string;
	try {
		canonicalEntity = sanitiseCanonical(entity);
	} catch (e) {
		if (e instanceof SanitiserError) {
			result.skipped.push({ file, reason: e.message });
			return;
		}
		throw e;
	}

	const fqn = cls.namespace ? `${cls.namespace}\\${cls.name}` : cls.name;
	result.classToEntity.set(fqn, canonicalEntity);
	result.classToEntity.set(cls.name, canonicalEntity);

	emitModelEntityAliases(result, file, cls, canonicalEntity);

	if (Object.keys(props.casts).length > 0) {
		result.castsByEntity.set(canonicalEntity, props.casts);
	}

	// Field fragments from $fillable. Layer 2 (ORM). The label is the
	// prettified field name — Eloquent never declares a display label,
	// only the column name, so we synthesise `is_active → "is active"`.
	for (const { name, line } of props.fillable) {
		let canonicalField: string;
		let label: string;
		try {
			canonicalField = sanitiseCanonical(name);
			label = sanitiseLabel(prettyFieldName(name));
		} catch {
			continue; // keep the loop tolerant — bad lines just drop
		}
		result.fragments.push({
			term: label.toLowerCase(),
			canonical: {
				kind: "field",
				field: `${canonicalEntity}.${canonicalField}`,
			},
			confidence: 0.7,
			locator: {
				file,
				line,
				layer: SourceLayer.Orm,
				extractor: "extractor-laravel:eloquent:fillable",
			},
		});
	}

	// Cast keys also imply field existence; emit if not already covered
	// by $fillable. (Some projects set $casts without $fillable or vice
	// versa.)
	const fillableNames = new Set(props.fillable.map((f) => f.name));
	for (const [name, _type] of Object.entries(props.casts)) {
		if (fillableNames.has(name)) continue;
		let canonicalField: string;
		let label: string;
		try {
			canonicalField = sanitiseCanonical(name);
			label = sanitiseLabel(prettyFieldName(name));
		} catch {
			continue;
		}
		result.fragments.push({
			term: label.toLowerCase(),
			canonical: {
				kind: "field",
				field: `${canonicalEntity}.${canonicalField}`,
			},
			confidence: 0.7,
			locator: {
				file,
				line: 1,
				layer: SourceLayer.Orm,
				extractor: "extractor-laravel:eloquent:casts",
			},
		});
	}

	const scopesByName = groupScopeFilters(props.scopes);
	for (const [scopeName, filters] of scopesByName) {
		if (filters.length === 1 && filters[0]?.operator === "=") {
			emitScopeEnumValue(result, file, canonicalEntity, scopeName, filters[0]);
			continue;
		}
		for (const filter of filters) {
			emitScopePredicate(result, file, canonicalEntity, scopeName, filter);
		}
	}
}

function emitModelEntityAliases(
	result: EloquentScanResult,
	file: string,
	cls: ClassDecl,
	canonicalEntity: string,
): void {
	const terms = new Set<string>();
	for (const term of [prettyModelName(cls.name)]) {
		const lowered = term.toLowerCase();
		if (lowered) terms.add(lowered);
	}
	for (const term of terms) {
		try {
			result.fragments.push({
				term: sanitiseLabel(term),
				canonical: {
					kind: "entity",
					entity: canonicalEntity,
				},
				confidence: 0.72,
				locator: {
					file,
					line: cls.line,
					layer: SourceLayer.Orm,
					extractor: "extractor-laravel:eloquent:model",
				},
			});
		} catch {
			// Bad model labels should not drop otherwise useful field evidence.
		}
	}
}

function groupScopeFilters(scopes: ScopeFilter[]): Map<string, ScopeFilter[]> {
	const grouped = new Map<string, ScopeFilter[]>();
	for (const scope of scopes) {
		const list = grouped.get(scope.scope);
		if (list) {
			list.push(scope);
		} else {
			grouped.set(scope.scope, [scope]);
		}
	}
	return grouped;
}

function emitScopeEnumValue(
	result: EloquentScanResult,
	file: string,
	canonicalEntity: string,
	scopeName: string,
	scope: ScopeFilter,
): void {
	let canonicalField: string;
	let label: string;
	try {
		canonicalField = sanitiseCanonical(scope.field);
		label = sanitiseLabel(prettyScopeName(scopeName));
	} catch {
		return;
	}
	result.fragments.push({
		term: label.toLowerCase(),
		canonical: {
			kind: "enum_value",
			enumName: `${canonicalEntity}.${canonicalField}`,
			rawValue: scope.rawValue,
		},
		confidence: 0.78,
		locator: scopeLocator(file, scope.line),
	});
}

function emitScopePredicate(
	result: EloquentScanResult,
	file: string,
	canonicalEntity: string,
	scopeName: string,
	scope: ScopeFilter,
): void {
	let canonicalField: string;
	let canonicalScope: string;
	let label: string;
	try {
		canonicalField = sanitiseCanonical(scope.field);
		canonicalScope = sanitiseCanonical(scopeNameToCanonical(scopeName));
		label = sanitiseLabel(prettyScopeName(scopeName));
	} catch {
		return;
	}
	result.fragments.push({
		term: label.toLowerCase(),
		canonical: {
			kind: "scope_predicate",
			scope: `${canonicalEntity}.${canonicalScope}`,
			field: `${canonicalEntity}.${canonicalField}`,
			operator: scope.operator,
			rawValue: scope.rawValue,
		},
		confidence: 0.78,
		locator: scopeLocator(file, scope.line),
	});
}

function scopeLocator(file: string, line: number): VocabFragment["locator"] {
	return {
		file,
		line,
		layer: SourceLayer.AppConstant,
		extractor: "extractor-laravel:eloquent:scope",
	};
}

function scopeNameToCanonical(name: string): string {
	return name
		.replace(/([a-z0-9])([A-Z])/g, "$1_$2")
		.replace(/[^A-Za-z0-9_]+/g, "_")
		.replace(/^_+|_+$/g, "")
		.toLowerCase();
}

// ---------------------------------------------------------------------------
// Class / namespace parsing
// ---------------------------------------------------------------------------

interface ClassDecl {
	name: string;
	line: number;
	namespace?: string;
}

const NAMESPACE_RX = /^\s*namespace\s+([A-Za-z_][A-Za-z0-9_\\]*)\s*;/m;
const CLASS_DECL_RX = /\bclass\s+([A-Za-z_][A-Za-z0-9_]*)\b/;

function parseClassDeclaration(text: string): ClassDecl | null {
	const cm = CLASS_DECL_RX.exec(text);
	if (!cm) return null;
	const ns = text.match(NAMESPACE_RX);
	const decl: ClassDecl = { name: cm[1]!, line: lineOf(text, cm.index) };
	if (ns) decl.namespace = ns[1];
	return decl;
}

function extendsAnyOf(text: string, bases: Set<string>): boolean {
	const m = text.match(/\bextends\s+([A-Za-z_\\][A-Za-z0-9_\\]*)/);
	if (!m) return false;
	const base = m[1]!.split("\\").pop()!;
	return bases.has(base);
}

// ---------------------------------------------------------------------------
// Property parsing
// ---------------------------------------------------------------------------

interface ModelProps {
	table?: string;
	fillable: Array<{ name: string; line: number }>;
	casts: Record<string, string>;
	scopes: ScopeFilter[];
}

interface ScopeFilter {
	scope: string;
	field: string;
	operator: string;
	rawValue: string;
	line: number;
}

const TABLE_PROP_RX =
	/(?:public|protected|private)?\s*(?:static\s+)?(?:\??[A-Za-z_][A-Za-z0-9_\\]*\s+)?\$table\s*=\s*(['"])((?:\\.|(?!\1).)*)\1\s*;/;

const ARRAY_PROP_RX_FACTORY = (name: string) =>
	new RegExp(
		`(?:public|protected|private)?\\s*(?:static\\s+)?(?:\\??[A-Za-z_][A-Za-z0-9_\\\\]*\\s+)?\\$${name}\\s*=\\s*(\\[|array\\s*\\()`,
	);

export function parseModelProperties(
	text: string,
	configConstants = new Map<string, string>(),
): ModelProps {
	const props: ModelProps = { fillable: [], casts: {}, scopes: [] };

	const tm = text.match(TABLE_PROP_RX);
	if (tm) props.table = unescapePhp(tm[2]!);

	const fillable = readArrayProperty(text, "fillable");
	if (fillable) {
		for (const item of fillable.items) {
			const lit = readPhpStringLiteral(item.value);
			if (lit !== null) {
				props.fillable.push({
					name: lit,
					line: lineOf(text, item.indexInText),
				});
			}
		}
	}

	const casts = readArrayProperty(text, "casts");
	if (casts) {
		for (const item of casts.items) {
			// Casts entries are key => value pairs.
			if (!item.key) continue;
			const k = readPhpStringLiteral(item.key);
			const v = readPhpStringLiteral(item.value);
			if (k !== null && v !== null) {
				props.casts[k] = v;
			}
		}
	}

	props.scopes = readScopeFilters(text, configConstants);

	return props;
}

interface ArrayItem {
	/** Optional key for `'key' => 'value'` entries. */
	key?: string;
	/** Raw value-side text (a string literal or expression). */
	value: string;
	/** Byte offset of the value within the surrounding text. */
	indexInText: number;
}

interface ArrayProperty {
	items: ArrayItem[];
}

function readArrayProperty(
	text: string,
	propName: string,
): ArrayProperty | null {
	const headRx = ARRAY_PROP_RX_FACTORY(propName);
	const m = text.match(headRx);
	if (!m) return null;
	const headEnd = (m.index ?? 0) + m[0].length;
	const opener = m[1]!.startsWith("[") ? "[" : "(";
	const closer = opener === "[" ? "]" : ")";
	// Re-locate the opener position because `array (` may have whitespace.
	let openIdx = headEnd - 1;
	while (openIdx > 0 && text[openIdx] !== opener) openIdx--;
	const inner = sliceBalanced(text, openIdx, opener, closer);
	if (!inner) return null;
	return { items: splitArrayItems(inner.body, inner.bodyStart) };
}

function readScopeFilters(
	text: string,
	configConstants: Map<string, string>,
): ScopeFilter[] {
	const scopes: ScopeFilter[] = [];
	const rx =
		/function\s+scope([A-Z][A-Za-z0-9_]*)\s*\(([^)]*)\)\s*(?::\s*[A-Za-z_\\][A-Za-z0-9_\\|?]*)?\s*\{/g;
	let match = rx.exec(text);
	while (match !== null) {
		const scope = match[1]!;
		const params = match[2]!;
		const openIdx = match.index + match[0].lastIndexOf("{");
		const body = sliceBalanced(text, openIdx, "{", "}");
		if (body && scopeHasNoRequiredInput(params)) {
			const filters = parseScopeWherePredicates(body.body, configConstants);
			if (filters) {
				for (const filter of filters) {
					scopes.push({
						scope,
						field: filter.field,
						operator: filter.operator,
						rawValue: filter.rawValue,
						line: lineOf(text, openIdx),
					});
				}
			}
		}
		match = rx.exec(text);
	}
	return scopes;
}

function scopeHasNoRequiredInput(params: string): boolean {
	const parts = params
		.split(",")
		.map((part) => part.trim())
		.filter(Boolean);
	if (parts.length <= 1) {
		return true;
	}
	return parts.slice(1).every((part) => part.includes("="));
}

function parseScopeWherePredicates(
	body: string,
	configConstants: Map<string, string>,
): Array<{ field: string; operator: string; rawValue: string }> | null {
	if (
		/->\s*(orWhere|whereNull|whereNotNull|whereIn|whereNotIn|whereBetween|whereExists)\s*\(/.test(
			body,
		)
	) {
		return null;
	}
	const calls = topLevelWhereCalls(body);
	if (calls.length === 0) {
		return null;
	}
	const filters: Array<{ field: string; operator: string; rawValue: string }> =
		[];
	for (const call of calls) {
		const openIdx = (call.index ?? 0) + call[0].lastIndexOf("(");
		const argsSpan = sliceBalanced(body, openIdx, "(", ")");
		if (!argsSpan) {
			return null;
		}
		const args = splitTopLevelArgs(argsSpan.body);
		if (args.length !== 2 && args.length !== 3) {
			return null;
		}
		const field = readPhpStringLiteral(args[0]!);
		if (!field || field.includes(".") || field.includes("->")) {
			return null;
		}
		let operator = "=";
		let rawValue: string | null = null;
		if (args.length === 2) {
			rawValue = readPhpScalarLiteral(args[1]!, configConstants);
		} else {
			const rawOperator = readPhpStringLiteral(args[1]!);
			if (!rawOperator) {
				return null;
			}
			operator = normalizeScopeOperator(rawOperator);
			if (!operator) {
				return null;
			}
			rawValue = readPhpScalarLiteral(args[2]!, configConstants);
		}
		if (rawValue === null) {
			return null;
		}
		filters.push({ field, operator, rawValue });
	}
	return filters;
}

function topLevelWhereCalls(body: string): RegExpMatchArray[] {
	return Array.from(body.matchAll(/->where\s*\(/g)).filter((call) =>
		isTopLevelAt(body, call.index ?? 0),
	);
}

function isTopLevelAt(text: string, index: number): boolean {
	let inStr: '"' | "'" | null = null;
	let parenDepth = 0;
	let bracketDepth = 0;
	let braceDepth = 0;
	for (let i = 0; i < index; i++) {
		const ch = text[i]!;
		if (inStr) {
			if (ch === "\\") {
				i++;
				continue;
			}
			if (ch === inStr) inStr = null;
			continue;
		}
		if (ch === '"' || ch === "'") {
			inStr = ch as '"' | "'";
			continue;
		}
		if (ch === "(") parenDepth++;
		else if (ch === ")") parenDepth = Math.max(0, parenDepth - 1);
		else if (ch === "[") bracketDepth++;
		else if (ch === "]") bracketDepth = Math.max(0, bracketDepth - 1);
		else if (ch === "{") braceDepth++;
		else if (ch === "}") braceDepth = Math.max(0, braceDepth - 1);
	}
	return parenDepth === 0 && bracketDepth === 0 && braceDepth === 0;
}

function normalizeScopeOperator(operator: string): string {
	const trimmed = operator.trim();
	if (trimmed === "==") return "=";
	if (trimmed === "<>") return "!=";
	if (["=", "!=", ">", ">=", "<", "<="].includes(trimmed)) return trimmed;
	return "";
}

function splitTopLevelArgs(text: string): string[] {
	const out: string[] = [];
	let inStr: '"' | "'" | null = null;
	let depth = 0;
	let segmentStart = 0;
	for (let i = 0; i < text.length; i++) {
		const ch = text[i]!;
		if (inStr) {
			if (ch === "\\") {
				i++;
				continue;
			}
			if (ch === inStr) inStr = null;
			continue;
		}
		if (ch === '"' || ch === "'") {
			inStr = ch as '"' | "'";
			continue;
		}
		if (ch === "[" || ch === "(" || ch === "{") depth++;
		else if (ch === "]" || ch === ")" || ch === "}") depth--;
		else if (ch === "," && depth === 0) {
			out.push(text.slice(segmentStart, i).trim());
			segmentStart = i + 1;
		}
	}
	out.push(text.slice(segmentStart).trim());
	return out.filter(Boolean);
}

function readPhpScalarLiteral(
	text: string,
	configConstants = new Map<string, string>(),
): string | null {
	const trimmed = text.trim();
	const stringLiteral = readPhpStringLiteral(trimmed);
	if (stringLiteral !== null) {
		return stringLiteral;
	}
	if (/^-?\d+(?:\.\d+)?$/.test(trimmed)) {
		return trimmed;
	}
	if (/^true$/i.test(trimmed)) {
		return "1";
	}
	if (/^false$/i.test(trimmed)) {
		return "0";
	}
	const configMatch = trimmed.match(
		/^config\(\s*(['"])(constants\.[^'"]+)\1\s*(?:,\s*([^)]+))?\)$/,
	);
	if (configMatch) {
		const key = configMatch[2]!;
		const mapped = configConstants.get(key);
		if (mapped !== undefined) {
			return mapped;
		}
		const fallback = configMatch[3];
		if (fallback) {
			return readPhpScalarLiteral(fallback, configConstants);
		}
	}
	return null;
}

function sliceBalanced(
	text: string,
	openIdx: number,
	opener: string,
	closer: string,
): { body: string; bodyStart: number } | null {
	const bodyStart = openIdx + 1;
	let depth = 0;
	let inStr: '"' | "'" | null = null;
	for (let i = openIdx; i < text.length; i++) {
		const ch = text[i]!;
		if (inStr) {
			if (ch === "\\") {
				i++;
				continue;
			}
			if (ch === inStr) inStr = null;
			continue;
		}
		if (ch === '"' || ch === "'") {
			inStr = ch as '"' | "'";
			continue;
		}
		if (ch === opener) depth++;
		else if (ch === closer) {
			depth--;
			if (depth === 0) return { body: text.slice(bodyStart, i), bodyStart };
		}
	}
	return null;
}

function splitArrayItems(body: string, bodyStart: number): ArrayItem[] {
	const out: ArrayItem[] = [];
	let i = 0;
	let inStr: '"' | "'" | null = null;
	let depth = 0;
	let segmentStart = 0;
	const segments: Array<{ text: string; offset: number }> = [];
	for (; i < body.length; i++) {
		const ch = body[i]!;
		if (inStr) {
			if (ch === "\\") {
				i++;
				continue;
			}
			if (ch === inStr) inStr = null;
			continue;
		}
		if (ch === '"' || ch === "'") {
			inStr = ch as '"' | "'";
			continue;
		}
		if (ch === "[" || ch === "(") depth++;
		else if (ch === "]" || ch === ")") depth--;
		else if (ch === "," && depth === 0) {
			segments.push({
				text: body.slice(segmentStart, i),
				offset: segmentStart,
			});
			segmentStart = i + 1;
		}
	}
	if (segmentStart < body.length) {
		segments.push({ text: body.slice(segmentStart), offset: segmentStart });
	}
	for (const s of segments) {
		const trimmed = s.text.trim();
		if (!trimmed) continue;
		// Look for `=>` at top-level of the segment.
		const arrowIdx = findTopLevelArrow(trimmed);
		if (arrowIdx >= 0) {
			out.push({
				key: trimmed.slice(0, arrowIdx).trim(),
				value: trimmed.slice(arrowIdx + 2).trim(),
				indexInText: bodyStart + s.offset,
			});
		} else {
			out.push({ value: trimmed, indexInText: bodyStart + s.offset });
		}
	}
	return out;
}

function findTopLevelArrow(text: string): number {
	let inStr: '"' | "'" | null = null;
	let depth = 0;
	for (let i = 0; i < text.length - 1; i++) {
		const ch = text[i]!;
		if (inStr) {
			if (ch === "\\") {
				i++;
				continue;
			}
			if (ch === inStr) inStr = null;
			continue;
		}
		if (ch === '"' || ch === "'") {
			inStr = ch as '"' | "'";
			continue;
		}
		if (ch === "[" || ch === "(") depth++;
		else if (ch === "]" || ch === ")") depth--;
		else if (depth === 0 && ch === "=" && text[i + 1] === ">") {
			return i;
		}
	}
	return -1;
}

function readPhpStringLiteral(text: string): string | null {
	const trimmed = text.trim();
	if (trimmed.length < 2) return null;
	const q = trimmed[0];
	if (q !== "'" && q !== '"') return null;
	if (trimmed[trimmed.length - 1] !== q) return null;
	return unescapePhp(trimmed.slice(1, -1));
}

function unescapePhp(s: string): string {
	return s.replace(/\\(.)/g, (_full, ch: string) => {
		switch (ch) {
			case "n":
				return "\n";
			case "r":
				return "\r";
			case "t":
				return "\t";
			default:
				return ch;
		}
	});
}

function lineOf(text: string, idx: number): number {
	let line = 1;
	for (let i = 0; i < idx; i++) {
		if (text[i] === "\n") line++;
	}
	return line;
}

function prettyFieldName(name: string): string {
	return name.replace(/_id$/i, "").replace(/_/g, " ").trim() || name;
}

function prettyModelName(name: string): string {
	return (
		name
			.replace(/([A-Z]+)([A-Z][a-z])/g, "$1 $2")
			.replace(/([a-z0-9])([A-Z])/g, "$1 $2")
			.replace(/_/g, " ")
			.trim() || name
	);
}

function prettyScopeName(name: string): string {
	return name
		.replace(/([a-z0-9])([A-Z])/g, "$1 $2")
		.replace(/_/g, " ")
		.trim();
}

// ---------------------------------------------------------------------------
// Convention class → table fallback (mirrors `filament.ts`)
//
// Re-exported so the Filament walker can use the same rule as a fallback
// when the Eloquent walker hasn't seen a model. This keeps both walkers
// consistent without a circular import.
// ---------------------------------------------------------------------------

import { modelClassToEntityCanonical } from "./filament.js";

function classNameToTable(name: string): string | null {
	return modelClassToEntityCanonical(name);
}
