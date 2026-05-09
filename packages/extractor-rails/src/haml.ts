/**
 * Rails Haml views walker — `app/views/**\/*.haml`.
 *
 * Haml is a whitespace-sensitive HTML templating dialect. Label shapes:
 *
 *     %label{ for: "user_email" } Email Address
 *     %label{for: "user_email"}= t('users.email')
 *     %label.required{ for: "user_email" } Email
 *     %label{ for: "user_email" }
 *       Email Address
 *     %label{ for: "user_email" }
 *       = t('users.email')
 *     = label_tag :email, t('users.email')                  (deferred)
 *     = f.label :email, t('users.email')                    (deferred)
 *
 * The walker recognises three shapes per [`scanHaml`]:
 *
 *   1. Inline static text:                 `%label{ for: "X" } Email Address`
 *   2. Inline Ruby expression:             `%label{ for: "X" }= t('key')`
 *   3. Block-form with next-line content:  `%label{ for: "X" }\n  = t('key')`
 *                                          `%label{ for: "X" }\n  Email Address`
 *
 * Block continuations are detected via indentation — any line whose
 * leading whitespace strictly exceeds the `%label` line's indent
 * contributes its inner content. The first contentful continuation
 * wins; subsequent lines are out of scope for v0.5.
 *
 * Confidence ranking + entity / field extraction mirror the ERB and
 * Slim walkers (first-underscore split, FormOrTableLabel layer, 0.95
 * static / 0.92 i18n).
 */

import { promises as fs } from "node:fs";
import path from "node:path";

import {
    sanitiseCanonical,
    sanitiseLabel,
    SanitiserError,
    SourceLayer,
    type LangIndex,
    type VocabFragment,
} from "@semsql/extractor-sdk";

/** Result of one walk over `app/views/`. */
export interface HamlScanResult {
    fragments: VocabFragment[];
    /** Files we recognised as Haml views but couldn't fully parse. */
    skipped: Array<{ file: string; reason: string }>;
}

/** Optional cross-walker context for the Haml walker. */
export interface HamlScanOptions {
    /**
     * Locale index from [`scanLocales`]. When supplied, Haml labels
     * containing `= t('key')` resolve against it; without it the
     * i18n-only shape is silently dropped.
     */
    langIndex?: LangIndex;
}

// `%label{ for: "user_email" } …` — Ruby-hash attribute syntax.
// Captures: indent, entity, field, optional inline rest.
//
// Notes on the attribute hash matcher:
//   - `["']?for["']?\s*(?::|=>)\s*` accepts both modern symbol-style
//     `for:` and legacy rocket-style `"for" => "…"` (Ruby ≤2.0
//     compatibility plus stringly-keyed hashes).
//   - Class-shorthand selectors (`%label.required`, `%label#id`) are
//     consumed before the attribute hash. Multiple selectors chain.
const LABEL_LINE_RX =
    /^(\s*)%label(?:[.#][\w-]+)*\s*\{\s*[^}]*?["']?for["']?\s*(?::|=>)\s*(['"])([a-z][a-z0-9]*)_([a-z][a-z0-9_]*)\2[^}]*\}(=)?\s*(.*)$/i;

const T_CALL_RX =
    /\bt\s*\(?\s*['"]([^'"]+)['"]\s*\)?(?!\s*,)/g;

/**
 * Walk every `*.haml` file under `app/views/` and emit form-label
 * vocabulary fragments. Tolerant of missing dirs.
 */
export async function scanHaml(
    root: string,
    options: HamlScanOptions = {},
): Promise<HamlScanResult> {
    const result: HamlScanResult = { fragments: [], skipped: [] };
    await walk(path.join(root, "app", "views"), result, options);
    return result;
}

async function walk(
    dir: string,
    result: HamlScanResult,
    options: HamlScanOptions,
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
        } else if (entry.endsWith(".haml")) {
            await scanFile(full, result, options);
        }
    }
}

async function scanFile(
    file: string,
    result: HamlScanResult,
    options: HamlScanOptions,
): Promise<void> {
    const text = await fs.readFile(file, "utf8");
    const lines = text.split(/\r?\n/);
    for (let i = 0; i < lines.length; i++) {
        const m = lines[i]!.match(LABEL_LINE_RX);
        if (!m) continue;
        const indent = (m[1] ?? "").length;
        const entity = m[3]!;
        const field = m[4]!;
        const isRubyExpr = m[5] === "=";
        const inlineRest = (m[6] ?? "").trim();
        let labelInfo: ResolvedLabel | null = null;

        if (inlineRest.length > 0) {
            labelInfo = isRubyExpr
                ? resolveRubyExpr(inlineRest, options.langIndex)
                : cleanLiteral(inlineRest);
        }

        // Block-form continuation: any indented line that's contentful
        // (not blank). First such line wins.
        if (labelInfo === null) {
            for (let j = i + 1; j < lines.length; j++) {
                const ln = lines[j]!;
                const lnIndent = ln.match(/^\s*/)?.[0].length ?? 0;
                if (ln.trim().length === 0) continue;
                if (lnIndent <= indent) break;
                const content = ln.slice(lnIndent);
                if (content.startsWith("=")) {
                    labelInfo = resolveRubyExpr(
                        content.slice(1).trim(),
                        options.langIndex,
                    );
                } else {
                    labelInfo = cleanLiteral(content);
                }
                if (labelInfo !== null) break;
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
                    ? `extractor-rails:haml:label-i18n:${labelInfo.locale ?? "?"}`
                    : "extractor-rails:haml:label",
            },
        });
    }
}

interface ResolvedLabel {
    label: string;
    viaI18n: boolean;
    locale: string | null;
}

function resolveRubyExpr(
    expr: string,
    langIndex: LangIndex | undefined,
): ResolvedLabel | null {
    const tCalls = Array.from(expr.matchAll(T_CALL_RX));
    if (tCalls.length > 0 && langIndex !== undefined) {
        for (const c of tCalls) {
            const entry = langIndex.get(c[1]!);
            if (entry !== undefined) {
                return { label: entry.label, viaI18n: true, locale: entry.locale };
            }
        }
    }
    return null;
}

function cleanLiteral(text: string): ResolvedLabel | null {
    const cleaned = text.replace(/\s+/g, " ").replace(/[:\s]+$/, "").trim();
    if (cleaned.length === 0) return null;
    return { label: cleaned, viaI18n: false, locale: null };
}
