/**
 * Rails Slim views walker — `app/views/**\/*.slim`.
 *
 * Slim is a whitespace-sensitive HTML templating dialect. The
 * canonical label shape:
 *
 *     label for="user_email" Email Address
 *     label for="user_email" = t('activerecord.attributes.user.email')
 *     label for="user_email"
 *       | Email Address
 *     label for="user_email"
 *       = t('users.email')
 *
 * The walker recognises three shapes per [`scanSlim`]:
 *
 *   1. Inline static text:                 `label for="X" Email Address`
 *   2. Inline Ruby expression:             `label for="X" = t('key')`
 *   3. Block-form with next-line `|`/`=`:  `label for="X"\n  | Email`
 *                                          `label for="X"\n  = t('key')`
 *
 * Block-form continuations are detected via indentation: any line whose
 * leading whitespace strictly exceeds the `label` line's indent and
 * starts with `|` or `=` contributes its inner content. The first
 * continuation wins — multi-line labels with conditional Ruby are
 * out of scope.
 *
 * Confidence ranking mirrors the ERB walker:
 *
 *   - Static label             → 0.95 (FormOrTableLabel)
 *   - i18n-resolved label      → 0.92 (FormOrTableLabel)
 *
 * Entity / field extraction follows the ERB walker's first-underscore
 * convention (`user_full_name` → entity=user, field=full_name).
 * Multi-word model classes require an
 * entity allowlist.
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
export interface SlimScanResult {
	fragments: VocabFragment[];
	/** Files we recognised as Slim views but couldn't fully parse. */
	skipped: Array<{ file: string; reason: string }>;
}

/** Optional cross-walker context for the slim walker. */
export interface SlimScanOptions {
	/**
	 * Locale index from [`scanLocales`]. When supplied, Slim labels
	 * containing `= t('key')` resolve against it; without it the
	 * i18n-only shape is silently dropped.
	 */
	langIndex?: LangIndex;
}

// Slim shorthand allows `label.required#id for="…"`. The `[#.][\w-]+`
// non-capturing group consumes one or more class/id selectors that
// directly follow the tag name (no space). After that we require
// whitespace before the `for=` attribute.
const LABEL_LINE_RX =
	/^(\s*)label(?:[.#][\w-]+)*\s+(?:[^>\n]*?\s)?for\s*=\s*(['"])([a-z][a-z0-9]*)_([a-z][a-z0-9_]*)\2(?:\s+(.*))?$/i;

const T_CALL_RX = /\bt\s*\(?\s*['"]([^'"]+)['"]\s*\)?(?!\s*,)/g;

/**
 * Walk every `*.slim` file under `app/views/` and emit form-label
 * vocabulary fragments. Tolerant of missing dirs.
 */
export async function scanSlim(
	root: string,
	options: SlimScanOptions = {},
): Promise<SlimScanResult> {
	const result: SlimScanResult = { fragments: [], skipped: [] };
	await walk(path.join(root, "app", "views"), result, options);
	return result;
}

async function walk(
	dir: string,
	result: SlimScanResult,
	options: SlimScanOptions,
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
		} else if (entry.endsWith(".slim")) {
			await scanFile(full, result, options);
		}
	}
}

async function scanFile(
	file: string,
	result: SlimScanResult,
	options: SlimScanOptions,
): Promise<void> {
	const text = await fs.readFile(file, "utf8");
	const lines = text.split(/\r?\n/);
	for (let i = 0; i < lines.length; i++) {
		const m = lines[i]!.match(LABEL_LINE_RX);
		if (!m) continue;
		const indent = (m[1] ?? "").length;
		const entity = m[3]!;
		const field = m[4]!;
		const inlineRest = (m[5] ?? "").trim();
		let labelInfo: ResolvedLabel | null = null;

		// Inline content path (1) static or (2) `= t(...)`.
		if (inlineRest.length > 0) {
			labelInfo = resolveSlimContent(inlineRest, options.langIndex);
		}

		// Block-form path (3): scan forward until indentation drops
		// back to or below the label line. The first `|` or `=` line
		// we encounter wins.
		if (labelInfo === null) {
			for (let j = i + 1; j < lines.length; j++) {
				const ln = lines[j]!;
				const lnIndent = ln.match(/^\s*/)?.[0].length ?? 0;
				if (ln.trim().length === 0) continue;
				if (lnIndent <= indent) break;
				const content = ln.slice(lnIndent);
				if (content.startsWith("|") || content.startsWith("=")) {
					labelInfo = resolveSlimContent(content, options.langIndex);
					break;
				}
			}
		}

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
				line: i + 1,
				layer: SourceLayer.FormOrTableLabel,
				extractor: labelInfo.viaI18n
					? `extractor-rails:slim:label-i18n:${labelInfo.locale ?? "?"}`
					: "extractor-rails:slim:label",
			},
		});
	}
}

interface ResolvedLabel {
	label: string;
	viaI18n: boolean;
	locale: string | null;
}

/**
 * Resolve a Slim label's content into a final label string.
 *
 * Cases:
 *   - Leading `|` followed by static text: take the text verbatim.
 *   - Leading `=` followed by a `t('key')` call: resolve via langIndex.
 *   - Bare static text: take verbatim.
 *
 * Returns `null` when nothing usable could be extracted.
 */
function resolveSlimContent(
	inner: string,
	langIndex: LangIndex | undefined,
): ResolvedLabel | null {
	const stripped = inner.trim();
	if (stripped.length === 0) return null;

	if (stripped.startsWith("|")) {
		const text = stripped.slice(1).trim();
		if (text.length === 0) return null;
		return cleanLiteral(text);
	}

	if (stripped.startsWith("=")) {
		const expr = stripped.slice(1).trim();
		return resolveRubyExpr(expr, langIndex);
	}

	// Bare-text inline form: `label for="X" Email Address`. Could
	// still contain a Ruby `= t(...)` mid-line — Slim tolerates it
	// less commonly. Treat the whole as literal.
	return cleanLiteral(stripped);
}

function resolveRubyExpr(
	expr: string,
	langIndex: LangIndex | undefined,
): ResolvedLabel | null {
	// Single `t('key')` expression — the most common Rails idiom.
	const tCalls = Array.from(expr.matchAll(T_CALL_RX));
	if (tCalls.length > 0 && langIndex !== undefined) {
		for (const c of tCalls) {
			const entry = langIndex.get(c[1]!);
			if (entry !== undefined) {
				return { label: entry.label, viaI18n: true, locale: entry.locale };
			}
		}
	}
	// Reject other dynamic expressions — they're computed at render
	// time and aren't stable vocabulary terms.
	return null;
}

function cleanLiteral(text: string): ResolvedLabel | null {
	const cleaned = text
		.replace(/\s+/g, " ")
		.replace(/[:\s]+$/, "")
		.trim();
	if (cleaned.length === 0) return null;
	return { label: cleaned, viaI18n: false, locale: null };
}
