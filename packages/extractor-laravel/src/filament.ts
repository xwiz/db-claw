/**
 * Filament Resource walker.
 *
 * Filament Resource classes are the highest-fidelity entity-vocabulary
 * source in a Laravel codebase: they explicitly declare what models map to
 * what UI labels. We exploit four static properties:
 *
 *     class StudentResource extends Resource
 *     {
 *         protected static ?string $model = User::class;
 *         protected static ?string $modelLabel = 'Student';
 *         protected static ?string $pluralModelLabel = 'Students';
 *         protected static ?string $navigationLabel = 'Students';
 *     }
 *
 * - `$model` → canonical entity (snake_case-d + pluralised, the Laravel
 *   default — e.g. `User::class` → `users`).
 * - `$modelLabel` → singular display label.
 * - `$pluralModelLabel` / `$navigationLabel` → plural display label.
 *
 * Form and Table column labels (`->label('X')`) are higher-fidelity still
 * but require the PHP AST walker. Tree-sitter-php coverage handles
 * the deeper cases; the regex pass here covers the common case in shipping
 * Filament apps.
 *
 * Ranks at source layer 6 (form/table label) — the spec's highest
 * priority. Beats i18n which ranks at layer 5.
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

import type { LangIndex } from "./lang.js";
import {
	type MakeI18nChain,
	type MakeLabelChain,
	extractMakeI18nChainsAst,
	extractMakeLabelChainsAst,
} from "./php-ast.js";

/** Result of one walk. */
export interface FilamentScanResult {
	fragments: VocabFragment[];
	/** Files we recognised as Filament Resource sources but couldn't fully parse. */
	skipped: Array<{ file: string; reason: string }>;
}

/** Optional class-to-entity map produced by the Eloquent walker. When
 *  present, takes precedence over the convention-based pluralisation
 *  in `modelClassToEntityCanonical`. */
export type ClassToEntityIndex = ReadonlyMap<string, string>;

/** Recursively scan `root/app/Filament/` for Resource classes. Single-panel
 *  layouts use `app/Filament/Resources/*.php`; multi-panel layouts use
 *  `app/Filament/<Panel>/Resources/*.php`. The recursive walk catches both.
 *
 *  Pass the optional `classToEntity` map (built by the Eloquent walker) to
 *  resolve `$model = User::class` against actual `$table` overrides on the
 *  Eloquent model — without it the walker falls back to convention
 *  pluralisation, which is correct most of the time but wrong for irregular
 *  table names (`$table = 'people'` on a `Person` model is fine, but
 *  `$table = 'tbl_user'` legacy-prefixed names need this map). */
export async function scanFilamentResources(
	root: string,
	classToEntity?: ClassToEntityIndex,
	langIndex?: LangIndex,
): Promise<FilamentScanResult> {
	const result: FilamentScanResult = { fragments: [], skipped: [] };
	await walk(
		path.join(root, "app", "Filament"),
		result,
		classToEntity,
		langIndex,
	);
	return result;
}

async function walk(
	dir: string,
	result: FilamentScanResult,
	classToEntity?: ClassToEntityIndex,
	langIndex?: LangIndex,
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
			await walk(full, result, classToEntity, langIndex);
		} else if (entry.endsWith("Resource.php")) {
			await scanResourceFile(full, result, classToEntity, langIndex);
		}
	}
}

async function scanResourceFile(
	file: string,
	result: FilamentScanResult,
	classToEntity?: ClassToEntityIndex,
	langIndex?: LangIndex,
): Promise<void> {
	const text = await fs.readFile(file, "utf8");
	if (!/extends\s+Resource\b/.test(text)) {
		return; // not a Filament Resource subclass
	}
	const props = parseResourceProperties(text);
	const model = props.model;
	if (!model) {
		result.skipped.push({ file, reason: "no $model property found" });
		return;
	}
	// Prefer the Eloquent walker's view (which has read `$table`) over
	// convention-derived pluralisation. We try the FQN first, then the
	// bare basename — both keys are populated by the Eloquent walker.
	const entity =
		classToEntity?.get(model) ??
		classToEntity?.get(model.split("\\").pop() ?? model) ??
		modelClassToEntityCanonical(model);
	if (entity === null || entity === undefined) {
		result.skipped.push({
			file,
			reason: `cannot derive canonical entity from $model = ${model}`,
		});
		return;
	}
	let canonical: string;
	try {
		canonical = sanitiseCanonical(entity);
	} catch (e) {
		if (e instanceof SanitiserError) {
			result.skipped.push({ file, reason: e.message });
			return;
		}
		throw e;
	}

	pushIfPresent(
		result,
		canonical,
		props.singularLabel,
		file,
		props.singularLine,
		"modelLabel",
	);
	pushIfPresent(
		result,
		canonical,
		props.pluralLabel,
		file,
		props.pluralLine,
		"pluralModelLabel",
	);
	pushIfPresent(
		result,
		canonical,
		props.navLabel,
		file,
		props.navLine,
		"navigationLabel",
	);

	// Form / Table column `->label()` calls — column-level vocabulary.
	for (const fl of extractMakeLabelPairs(text, file, canonical)) {
		result.fragments.push(fl);
	}

	// i18n-bound `->label(__('key'))` chains. Resolved against the
	// optional lang index — a chain whose key isn't in the index is
	// surfaced via `skipped` so users see the gap in `semsql doctor`
	// rather than silently lose vocabulary.
	if (langIndex !== undefined) {
		for (const fl of extractMakeI18nFragments(
			text,
			file,
			canonical,
			langIndex,
			result,
		)) {
			result.fragments.push(fl);
		}
	}
}

function pushIfPresent(
	result: FilamentScanResult,
	canonical: string,
	label: string | undefined,
	file: string,
	line: number | undefined,
	source: string,
): void {
	// `undefined` = property not declared → silent skip. Empty string =
	// declared with a bad value → loud skip via the sanitiser.
	if (label === undefined) return;
	let cleaned: string;
	try {
		cleaned = sanitiseLabel(label);
	} catch (e) {
		if (e instanceof SanitiserError) {
			result.skipped.push({ file, reason: e.message });
			return;
		}
		throw e;
	}
	result.fragments.push({
		term: cleaned.toLowerCase(),
		canonical: { kind: "entity", entity: canonical },
		confidence: 0.95,
		locator: {
			file,
			line: line ?? 1,
			layer: SourceLayer.FormOrTableLabel,
			extractor: `extractor-laravel:filament:${source}`,
		},
	});
}

// ---------------------------------------------------------------------------
// Form / Table column `->label()` extraction
//
// Captures the dominant Filament v3 idiom:
//
//     Forms\Components\TextInput::make('name')->label('Full Name')
//     Tables\Columns\TextColumn::make('email')->label('Email Address')
//
// Two paths in priority order:
//
//   1. Tree-sitter-php AST walk via `php-ast.ts`. Handles split-line
//      chains, nested parens, conditional modifiers — the long-tail
//      shapes the regex misses. Lazy-loaded; cold-start ~30 ms.
//   2. Regex fallback. Used iff tree-sitter fails to load (rare; usually
//      a pre-built binding mismatch on exotic CPU). Less robust but
//      still covers ~80% of shipping Filament code.
//
// Both paths produce the same {field, label, line} hit shape so the
// downstream sanitiser + emit code is path-agnostic.
// ---------------------------------------------------------------------------

const MAKE_LABEL_RX =
	/::make\s*\(\s*(['"])([^'"]+)\1\s*\)((?:\s*->\s*\w+\s*\([^()]*\))*?)\s*->\s*label\s*\(\s*(['"])((?:\\.|(?!\4).)*)\4\s*\)/g;

interface MakeLabelHit {
	field: string;
	label: string;
	/** 1-indexed line of the `::make` token. */
	line: number;
}

/**
 * Public entry point — used by the tests in `filament.test.ts` and by the
 * internal emitter below. Walks the AST first; falls back to regex if the
 * parser is unavailable.
 */
export function extractMakeLabelPairsRaw(text: string): MakeLabelHit[] {
	const ast = extractMakeLabelChainsAst(text);
	if (ast !== null) {
		return ast.map((c: MakeLabelChain) => ({
			field: c.field,
			label: c.label,
			line: c.line,
		}));
	}
	// Regex fallback. Operates on byte offsets, so we convert to lines
	// here to match the AST shape.
	const out: MakeLabelHit[] = [];
	for (const match of text.matchAll(MAKE_LABEL_RX)) {
		out.push({
			field: match[2]!,
			label: unescapePhp(match[5]!),
			line: lineOf(text, match.index ?? 0),
		});
	}
	return out;
}

/**
 * Resolve every `Type::make('field')->label(__('key'))` chain in `text`
 * against the supplied `langIndex` and emit field-level vocabulary
 * fragments at FormOrTableLabel layer.
 *
 * Chains whose i18n key is not in the index are recorded in
 * `result.skipped` with `i18n key not in lang index: <key>` — this is
 * the diagnostic users want when `semsql doctor` shows under-vocabulary
 * coverage on a localised app.
 *
 * Without an AST walker, the regex fallback would need to recognise
 * the `__(...)` pattern too. tree-sitter is the only path supported
 * for now; if the AST walker isn't available the i18n pass is skipped
 * silently (the literal-string walker still runs).
 */
function extractMakeI18nFragments(
	text: string,
	file: string,
	entityCanonical: string,
	langIndex: LangIndex,
	result: FilamentScanResult,
): VocabFragment[] {
	const chains = extractMakeI18nChainsAst(text);
	if (chains === null) {
		return []; // no AST walker on this host; literal walker carries the load
	}
	const out: VocabFragment[] = [];
	for (const chain of chains as MakeI18nChain[]) {
		const entry = langIndex.get(chain.i18nKey);
		if (entry === undefined) {
			result.skipped.push({
				file,
				reason: `i18n key not in lang index: ${chain.i18nKey} (line ${chain.line})`,
			});
			continue;
		}
		let canonicalField: string;
		let cleanedLabel: string;
		try {
			canonicalField = sanitiseCanonical(chain.field);
			cleanedLabel = sanitiseLabel(entry.label);
		} catch (e) {
			if (e instanceof SanitiserError) {
				result.skipped.push({ file, reason: e.message });
				continue;
			}
			throw e;
		}
		out.push({
			term: cleanedLabel.toLowerCase(),
			canonical: {
				kind: "field",
				field: `${entityCanonical}.${canonicalField}`,
			},
			// Slightly lower than literal-string (`0.95`) to reflect
			// the i18n indirection — if the lang file changes after
			// extract, the binding is stale. Still well above the
			// 0.85 layer-5 confidence used for raw lang-file emits.
			confidence: 0.92,
			locator: {
				file,
				line: chain.line,
				layer: SourceLayer.FormOrTableLabel,
				extractor: `extractor-laravel:filament:make-label-i18n:${entry.locale}`,
			},
		});
	}
	return out;
}

function extractMakeLabelPairs(
	text: string,
	file: string,
	entityCanonical: string,
): VocabFragment[] {
	const out: VocabFragment[] = [];
	for (const hit of extractMakeLabelPairsRaw(text)) {
		let canonicalField: string;
		let cleanedLabel: string;
		try {
			canonicalField = sanitiseCanonical(hit.field);
			cleanedLabel = sanitiseLabel(hit.label);
		} catch {
			continue; // hostile or malformed — silent drop is fine here
		}
		out.push({
			term: cleanedLabel.toLowerCase(),
			canonical: {
				kind: "field",
				field: `${entityCanonical}.${canonicalField}`,
			},
			confidence: 0.95,
			locator: {
				file,
				line: hit.line,
				layer: SourceLayer.FormOrTableLabel,
				extractor: "extractor-laravel:filament:make-label",
			},
		});
	}
	return out;
}

// ---------------------------------------------------------------------------
// PHP property parsing
// ---------------------------------------------------------------------------

interface ResourceProps {
	model?: string;
	singularLabel?: string;
	pluralLabel?: string;
	navLabel?: string;
	singularLine?: number;
	pluralLine?: number;
	navLine?: number;
}

// Visibility prefix is optional in PHP — `static $foo` / `public static $foo` /
// `private static $foo` are all valid. Type hint is also optional, may be
// nullable (`?string`), or fully-qualified (`?\App\Models\Foo`). The middle
// non-capturing group covers all three shapes.
const STATIC_PROP_RX =
	/(?:public|protected|private)?\s*static\s+(?:\??\\?[A-Za-z_][A-Za-z0-9_\\]*\s+)?\$(\w+)\s*=\s*([^;]+);/g;

// Filament v3+ idiom: method-based label getters override the static
// properties when present. Pattern:
//
//     public static function getModelLabel(): string { return 'Student'; }
//
// We capture the trivial-return shape only — methods that compute the
// label dynamically (`return __('students.label')`) require resolving an
// i18n key and are left to the tree-sitter-php path.
const STATIC_LABEL_METHOD_RX =
	/(?:public|protected|private)?\s*static\s+function\s+(get(?:ModelLabel|PluralModelLabel|NavigationLabel))\s*\([^)]*\)\s*(?::\s*\??[A-Za-z_][A-Za-z0-9_\\]*\s*)?\{\s*return\s+(['"])((?:\\.|(?!\2).)*)\2\s*;\s*\}/g;

const STRING_LITERAL_RX = /^\s*(['"])((?:\\.|(?!\1).)*)\1\s*$/;
const CLASS_REF_RX = /^\s*([A-Za-z_][A-Za-z0-9_\\]*)::class\s*$/;

/** Parse the four properties of interest from a PHP class body. */
export function parseResourceProperties(text: string): ResourceProps {
	const props: ResourceProps = {};
	for (const match of text.matchAll(STATIC_PROP_RX)) {
		const name = match[1]!;
		const rawValue = match[2]!;
		const lineNumber = lineOf(text, match.index ?? 0);
		switch (name) {
			case "model": {
				const cls = rawValue.match(CLASS_REF_RX);
				if (cls) props.model = cls[1];
				break;
			}
			case "modelLabel": {
				const lit = rawValue.match(STRING_LITERAL_RX);
				if (lit) {
					props.singularLabel = unescapePhp(lit[2]!);
					props.singularLine = lineNumber;
				}
				break;
			}
			case "pluralModelLabel": {
				const lit = rawValue.match(STRING_LITERAL_RX);
				if (lit) {
					props.pluralLabel = unescapePhp(lit[2]!);
					props.pluralLine = lineNumber;
				}
				break;
			}
			case "navigationLabel": {
				const lit = rawValue.match(STRING_LITERAL_RX);
				if (lit) {
					props.navLabel = unescapePhp(lit[2]!);
					props.navLine = lineNumber;
				}
				break;
			}
		}
	}
	// Method-form overrides static-property form (Filament v3 semantics:
	// the method is consulted last, so it's the authoritative label).
	for (const match of text.matchAll(STATIC_LABEL_METHOD_RX)) {
		const fn = match[1]!;
		const literal = unescapePhp(match[3]!);
		const lineNumber = lineOf(text, match.index ?? 0);
		switch (fn) {
			case "getModelLabel":
				props.singularLabel = literal;
				props.singularLine = lineNumber;
				break;
			case "getPluralModelLabel":
				props.pluralLabel = literal;
				props.pluralLine = lineNumber;
				break;
			case "getNavigationLabel":
				props.navLabel = literal;
				props.navLine = lineNumber;
				break;
		}
	}
	return props;
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

// ---------------------------------------------------------------------------
// Model class → canonical entity name
//
// Laravel convention: `User::class` → table `users`, `OrderItem::class` →
// `order_items`. We mirror Laravel's `Str::snake` (`(.)(?=[A-Z])` — every
// char before an uppercase gets a `_` suffix) so that `URLPath` →
// `u_r_l_path` matches the framework's actual default table name. Users
// with custom `$table` overrides should ship the Eloquent walker output;
// we emit a `skipped` entry for collisions caught downstream.
//
// Pluralisation is intentionally conservative: a small `-ves` allow-list
// (leaf, life, knife …) rather than a blanket `f|fe → ves`. Words like
// `chief`, `roof`, `belief`, `proof` correctly become `chiefs`, `roofs`,
// `beliefs`, `proofs`. The full doctrine-inflector ruleset is dozens of
// edge cases; we aim for "no false positives" rather than "covers every
// English noun", and let users override the rest via `semsql.overrides.yaml`.
// ---------------------------------------------------------------------------

const IRREGULAR: Record<string, string> = {
	person: "people",
	child: "children",
	foot: "feet",
	tooth: "teeth",
	mouse: "mice",
	man: "men",
	woman: "women",
	datum: "data",
	criterion: "criteria",
	analysis: "analyses",
	diagnosis: "diagnoses",
	index: "indices",
	matrix: "matrices",
	vertex: "vertices",
};

const UNCHANGED_PLURAL = new Set([
	"fish",
	"sheep",
	"deer",
	"series",
	"species",
	"aircraft",
	"news",
	"media",
]);

// Words ending in -f / -fe that *do* take -ves. Anything else ending in
// -f / -fe takes a plain -s.
const VES_PLURAL = new Set([
	"leaf",
	"loaf",
	"sheaf",
	"thief",
	"life",
	"knife",
	"wife",
	"half",
	"calf",
	"shelf",
	"self",
	"wolf",
	"elf",
	"scarf",
	"wharf",
	"hoof",
]);

export function modelClassToEntityCanonical(model: string): string | null {
	const last = model.split("\\").pop();
	if (!last) return null;
	// Laravel `Str::snake`: insert `_` before every uppercase character
	// (except at the start), then lower-case. Matches `(.)(?=[A-Z])`.
	const snake = last.replace(/(.)(?=[A-Z])/g, "$1_").toLowerCase();
	return pluralise(snake);
}

function pluralise(word: string): string {
	const lower = word.toLowerCase();
	const tail = lower.split("_").pop()!;
	if (UNCHANGED_PLURAL.has(tail)) return lower;
	if (IRREGULAR[tail]) {
		return lower.slice(0, lower.length - tail.length) + IRREGULAR[tail];
	}
	if (VES_PLURAL.has(tail)) {
		return lower.replace(/(f|fe)$/, "ves");
	}
	if (/(s|x|z|ch|sh)$/.test(lower)) return `${lower}es`;
	if (/[bcdfghjklmnpqrstvwxz]y$/.test(lower)) return `${lower.slice(0, -1)}ies`;
	return `${lower}s`;
}
