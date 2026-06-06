/**
 * Rails ERB views walker — `<label for="user_email">…</label>` + i18n.
 *
 * Rails form-builder helpers usually emit explicit `<label for="X">`
 * associations whose `for` attribute follows the Rails convention
 * `<entity>_<field>` (snake-case singular model + snake-case attribute).
 * The label's *inner* content is either:
 *
 *   1. Static text — `<label for="user_email">Email Address</label>`
 *   2. An ERB i18n call — `<label for="user_email"><%= t('users.email') %></label>`
 *   3. A combination — `<label for="user_email"><%= t('users.email') %>:</label>`
 *
 * We extract the (entity, field, label) triple in all three shapes,
 * resolving i18n keys via the optional `LangIndex` produced by
 * [`scanLocales`]. Without an index the i18n shape is silently dropped
 * — the `t('…')` call's literal value is unknown at extract time.
 *
 * Confidence ranking:
 *
 *   - Static label             → 0.95 (FormOrTableLabel layer)
 *   - i18n-resolved label      → 0.92 (FormOrTableLabel; same indirection
 *                                       penalty Laravel + Vue use)
 *
 * Deliberately skipped by this view reader:
 *
 *   - Custom form-builder helpers in plain Ruby (`f.label :email, …`)
 *     — needs a Ruby AST walker.
 *   - Multi-line label blocks with conditional ERB — the regex
 *     conservatively matches only single-line label tags. False
 *     negatives, never false positives.
 */

import { promises as fs } from "node:fs";
import path from "node:path";

import {
	type LangIndex,
	SanitiserError,
	SourceLayer,
	type VocabFragment,
	sanitiseCanonical,
	sanitiseLabel,
} from "@semsql/extractor-sdk";

/** Result of one walk over `app/views/`. */
export interface ViewsScanResult {
	fragments: VocabFragment[];
	/** Files we recognised as ERB views but couldn't fully parse. */
	skipped: Array<{ file: string; reason: string }>;
}

/** Optional cross-walker context for the views walker. */
export interface ViewsScanOptions {
	/**
	 * Locale index from [`scanLocales`]. When supplied, ERB labels
	 * containing `<%= t('key') %>` / `<%= I18n.t('key') %>` resolve
	 * against it; without it the i18n shape is silently dropped.
	 */
	langIndex?: LangIndex;
}

/**
 * Walk every `*.erb` file under `app/views/` and emit form-label
 * vocabulary fragments. Tolerant of missing dirs — projects without
 * `app/views/` produce an empty result.
 */
export async function scanViews(
	root: string,
	options: ViewsScanOptions = {},
): Promise<ViewsScanResult> {
	const result: ViewsScanResult = { fragments: [], skipped: [] };
	await walk(path.join(root, "app", "views"), result, options);
	return result;
}

async function walk(
	dir: string,
	result: ViewsScanResult,
	options: ViewsScanOptions,
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
			await walk(full, result, options);
		} else if (entry.endsWith(".erb")) {
			await scanFile(full, result, options);
		}
	}
}

// `<label for="user_email">…inner…</label>`
//   - `for` is double- or single-quoted
//   - `<entity>_<field>` snake_case shape; entity is restricted to a
//     SINGLE snake-segment (`[a-z][a-z0-9]*`) so the split lands on
//     the first underscore. Field captures the remainder, which can
//     itself contain underscores (`user_full_name` → entity=user,
//     field=full_name).
//   - inner can contain ERB tags + plain text + nested HTML; we
//     stop at the closing `</label>` on the same line
//
// Limitation: multi-word model classes (`OrderItem` → `order_item`)
// produce form ids like `order_item_quantity`. The first-underscore
// split mis-attributes the entity to `order`. The orchestrator can
// disambiguate by supplying an entity allowlist; until then
// the walker silently produces the single-word reading. False
// negatives (model not in schema), never injection-class false
// positives.
const LABEL_RX =
	/<label\b[^>]*\bfor\s*=\s*(['"])([a-z][a-z0-9]*)_([a-z][a-z0-9_]*)\1[^>]*>([^<]*(?:<%=?[\s\S]*?%>[^<]*)*)<\/label>/gi;

// `<%= t('users.email') %>` / `<%= I18n.t("a.b.c") %>` / `<%= t :symbol %>`
//   - first matched group is the key
//   - rejects multi-arg calls (count interpolation, scope: opts) — these
//     are dynamic and shouldn't be emitted as static vocabulary
const T_CALL_RX =
	/<%=?\s*(?:I18n\s*\.\s*)?t\s*\(?\s*['"]([^'"]+)['"]\s*\)?\s*%>/g;

// Rails form-builder label helper:
//
//     <%= f.label :email, "Email Address" %>
//     <%= form.label :full_name, t('users.full_name') %>
//
// Captures: form-builder var (unused), field symbol, literal label OR
// i18n key (via the `t()` shape). The entity is inferred from the
// view's path — `app/views/users/_form.html.erb` → entity `user`.
//
// We accept any identifier as the form-builder var (`f`, `form`, `ff`)
// but reject method-chain receivers (`form.fields_for(:address)
// .label(...)`) — those bind to a different model.
const F_LABEL_LITERAL_RX =
	/<%=\s*([a-zA-Z_]\w*)\.label\s*\(?\s*:([a-z_][a-z0-9_]*)\s*,\s*(['"])((?:\\.|(?!\3).)*)\3\s*\)?\s*%>/g;
const F_LABEL_I18N_RX =
	/<%=\s*([a-zA-Z_]\w*)\.label\s*\(?\s*:([a-z_][a-z0-9_]*)\s*,\s*(?:I18n\s*\.\s*)?t\s*\(?\s*['"]([^'"]+)['"]\s*\)?\s*\)?\s*%>/g;

async function scanFile(
	file: string,
	result: ViewsScanResult,
	options: ViewsScanOptions,
): Promise<void> {
	const text = await fs.readFile(file, "utf8");

	// Path-inferred entity for form-builder calls. Rails convention:
	//   app/views/<pluralized-model>/<action>.html.erb → entity = singular
	// Best-effort — path lookup runs once per file and is reused for
	// every `f.label :…` match. `null` skips the form-builder pass.
	const pathEntity = inferEntityFromPath(file);

	for (const match of text.matchAll(LABEL_RX)) {
		const entity = match[2]!;
		const field = match[3]!;
		const inner = match[4]!.trim();
		const labelInfo = resolveLabel(inner, options.langIndex);
		if (labelInfo === null) continue;

		let canonicalEntity: string;
		let canonicalField: string;
		let cleanedLabel: string;
		try {
			canonicalEntity = sanitiseCanonical(entity);
			canonicalField = sanitiseCanonical(field);
			cleanedLabel = sanitiseLabel(labelInfo.label);
		} catch (e) {
			if (e instanceof SanitiserError) {
				result.skipped.push({ file, reason: e.message });
				continue;
			}
			throw e;
		}
		result.fragments.push({
			term: cleanedLabel.toLowerCase(),
			canonical: {
				kind: "field",
				field: `${canonicalEntity}.${canonicalField}`,
			},
			confidence: labelInfo.viaI18n ? 0.92 : 0.95,
			locator: {
				file,
				line: lineOf(text, match.index ?? 0),
				layer: SourceLayer.FormOrTableLabel,
				extractor: labelInfo.viaI18n
					? `extractor-rails:views:label-i18n:${labelInfo.locale ?? "?"}`
					: "extractor-rails:views:label",
			},
		});
	}

	if (pathEntity !== null) {
		// Form-builder literal: `<%= f.label :email, "Email Address" %>`
		for (const match of text.matchAll(F_LABEL_LITERAL_RX)) {
			const field = match[2]!;
			const label = match[4]!;
			emitFormBuilderFragment(
				file,
				text,
				match.index ?? 0,
				pathEntity,
				field,
				label,
				/*viaI18n*/ false,
				/*locale*/ null,
				result,
			);
		}
		// Form-builder i18n: `<%= f.label :email, t('users.email') %>`
		if (options.langIndex !== undefined) {
			for (const match of text.matchAll(F_LABEL_I18N_RX)) {
				const field = match[2]!;
				const key = match[3]!;
				const entry = options.langIndex.get(key);
				if (entry === undefined) continue;
				emitFormBuilderFragment(
					file,
					text,
					match.index ?? 0,
					pathEntity,
					field,
					entry.label,
					/*viaI18n*/ true,
					entry.locale,
					result,
				);
			}
		}
	}
}

function emitFormBuilderFragment(
	file: string,
	text: string,
	matchIndex: number,
	pathEntity: string,
	rawField: string,
	rawLabel: string,
	viaI18n: boolean,
	locale: string | null,
	result: ViewsScanResult,
): void {
	let canonicalEntity: string;
	let canonicalField: string;
	let cleanedLabel: string;
	try {
		canonicalEntity = sanitiseCanonical(pathEntity);
		canonicalField = sanitiseCanonical(rawField);
		cleanedLabel = sanitiseLabel(rawLabel);
	} catch (e) {
		if (e instanceof SanitiserError) {
			result.skipped.push({ file, reason: e.message });
			return;
		}
		throw e;
	}
	result.fragments.push({
		term: cleanedLabel.toLowerCase(),
		canonical: {
			kind: "field",
			field: `${canonicalEntity}.${canonicalField}`,
		},
		// Form-builder fragments inherit a slightly lower confidence
		// than `<label for=…>` matches because the entity is *inferred*
		// from the view path rather than spelled out in the markup.
		// The orchestrator is expected to override path-inferred
		// entities via an explicit allowlist.
		confidence: viaI18n ? 0.88 : 0.9,
		locator: {
			file,
			line: lineOf(text, matchIndex),
			layer: SourceLayer.FormOrTableLabel,
			extractor: viaI18n
				? `extractor-rails:views:f-label-i18n:${locale ?? "?"}`
				: "extractor-rails:views:f-label",
		},
	});
}

/**
 * Infer the canonical entity name from a Rails view path.
 *
 *   app/views/users/_form.html.erb       → "user"
 *   app/views/order_items/_form.haml     → "order_item"
 *   app/views/people/index.html.erb      → "person" (irregular)
 *   app/views/categories/edit.html.erb   → "category"
 *
 * Returns `null` when:
 *   - the path doesn't include `app/views/`, OR
 *   - the segment immediately after `app/views/` is one of the
 *     known shared/layout dirs (`shared`, `layouts`, `application`,
 *     `partials`) which don't bind to a single model.
 *
 * Singularisation is a tiny rule set — Rails' real `inflector` is
 * substantially richer. Operators with non-standard pluralisation
 * supply an explicit entity allowlist via the orchestrator.
 */
export function inferEntityFromPath(file: string): string | null {
	const norm = file.replace(/\\/g, "/");
	const idx = norm.indexOf("/app/views/");
	if (idx < 0) return null;
	const after = norm.slice(idx + "/app/views/".length);
	const seg = after.split("/")[0];
	if (!seg) return null;
	if (
		seg === "shared" ||
		seg === "layouts" ||
		seg === "application" ||
		seg === "partials"
	) {
		return null;
	}
	return singularise(seg);
}

function singularise(plural: string): string {
	// Irregulars + tricky plurals first. Conservative — only
	// common-by-volume forms; everything else falls through to the
	// simple `-s` strip.
	const IRREG: Record<string, string> = {
		people: "person",
		children: "child",
		men: "man",
		women: "woman",
		feet: "foot",
		teeth: "tooth",
		mice: "mouse",
		geese: "goose",
		oxen: "ox",
	};
	if (IRREG[plural]) return IRREG[plural]!;
	if (plural.endsWith("ies") && plural.length > 3) {
		return `${plural.slice(0, -3)}y`;
	}
	if (
		plural.endsWith("ses") ||
		plural.endsWith("xes") ||
		plural.endsWith("zes") ||
		plural.endsWith("ches") ||
		plural.endsWith("shes")
	) {
		return plural.slice(0, -2);
	}
	if (plural.endsWith("s") && !plural.endsWith("ss")) {
		return plural.slice(0, -1);
	}
	return plural;
}

interface ResolvedLabel {
	label: string;
	viaI18n: boolean;
	locale: string | null;
}

/**
 * Resolve a label's inner content into a final label string.
 *
 * Cases:
 *   - Pure ERB `t(...)` call(s): resolve against the langIndex; the
 *     first resolvable key wins (typical pattern is one call per
 *     label). When no langIndex is supplied OR the key is absent,
 *     return null.
 *   - Mix of ERB + static text: prefer the i18n call when one is
 *     present (it's the authoritative label); fall back to the
 *     literal text with ERB tags stripped.
 *   - Pure static text: return verbatim.
 *
 * Returns null when nothing usable could be extracted (empty inner,
 * whitespace-only, unresolved t-call without static fallback).
 */
function resolveLabel(
	inner: string,
	langIndex: LangIndex | undefined,
): ResolvedLabel | null {
	const tCalls = Array.from(inner.matchAll(T_CALL_RX));
	if (tCalls.length > 0 && langIndex !== undefined) {
		for (const c of tCalls) {
			const entry = langIndex.get(c[1]!);
			if (entry !== undefined) {
				return { label: entry.label, viaI18n: true, locale: entry.locale };
			}
		}
	}
	// Static fallback: strip ERB tags + collapse whitespace.
	const stripped = inner
		.replace(/<%[=#]?[\s\S]*?%>/g, "")
		.replace(/\s+/g, " ")
		.trim()
		// Trim a trailing `:` punctuation that Rails templates often
		// append — `Email Address:` should normalise to `Email Address`.
		.replace(/[:\s]+$/, "")
		.trim();
	if (stripped.length === 0) return null;
	return { label: stripped, viaI18n: false, locale: null };
}

function lineOf(text: string, byteOffset: number): number {
	let line = 1;
	for (let i = 0; i < byteOffset && i < text.length; i++) {
		if (text[i] === "\n") line++;
	}
	return line;
}
