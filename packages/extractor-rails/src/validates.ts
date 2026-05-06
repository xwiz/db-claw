/**
 * Rails `validates ..., inclusion: { in: [...] }` walker.
 *
 * The secondary value-vocabulary surface in idiomatic Rails apps —
 * fields that aren't `enum`-declared but have a constrained set of
 * allowed values via the inclusion validator:
 *
 *     class User < ApplicationRecord
 *       validates :status, inclusion: { in: %w[active inactive pending] }
 *       validates :role, inclusion: { in: ["admin", "member", "guest"] }
 *       validates :tier, inclusion: { in: [:bronze, :silver, :gold], message: "..." }
 *     end
 *
 * What we extract:
 *
 *  - **Field name**: the first symbol arg to `validates`.
 *  - **Allowed values**: the array literal under `inclusion: { in: ... }`.
 *    Three array forms supported:
 *      * `%w[a b c]`           → `["a", "b", "c"]` (whitespace-split words)
 *      * `["a", "b", "c"]`     → quoted-string list
 *      * `[:a, :b, :c]`        → symbol list (output uses bare names)
 *  - **Raw value**: the string verbatim. Symbol form (`:bronze`) emits
 *    raw value `"bronze"` since AR stores symbols as strings under the
 *    column.
 *
 * Each allowed value yields one `enum_value` fragment, sharing the
 * canonical-name shape with the actual `enum` walker — downstream
 * the cascade can't tell the difference between an `enum` and an
 * inclusion validator, which is the right outcome.
 *
 * Skipped at v0.5:
 *
 *  - `validates :name, format: { with: /pattern/ }` — pattern-based
 *    constraints don't yield a finite vocabulary.
 *  - `validates :age, numericality: { in: 0..150 }` — range, not
 *    enumerated values.
 *  - Computed `in:` arguments (e.g. `in: User.allowed_statuses`) —
 *    runtime resolution out of scope at v0.5.
 *  - `validates_inclusion_of :foo, in: [...]` — the legacy macro
 *    form. Rails 5+ idiom is `validates :foo, inclusion: ...`; we
 *    add the legacy macro in v1.0 alongside the tree-sitter walker.
 */

import { promises as fs } from "node:fs";
import path from "node:path";
import {
    sanitiseCanonical,
    sanitiseLabel,
    SanitiserError,
    SourceLayer,
    type VocabFragment,
} from "@semsql/extractor-sdk";

import {
    extractTableNameOverride,
    extractTopLevelClassName,
    inflectTableName,
} from "./enums.js";

/** Result of walking the models directory. */
export interface ValidatesScanResult {
    fragments: VocabFragment[];
    /** Files we recognised but couldn't fully parse. */
    skipped: Array<{ file: string; reason: string }>;
}

const MODELS_DIR_CANDIDATES = ["app/models"];

/** Scan `app/models/**\/*.rb` for inclusion validators. */
export async function scanRailsValidates(
    root: string,
): Promise<ValidatesScanResult> {
    const result: ValidatesScanResult = { fragments: [], skipped: [] };
    for (const sub of MODELS_DIR_CANDIDATES) {
        await walk(path.join(root, sub), result);
    }
    return result;
}

async function walk(dir: string, result: ValidatesScanResult): Promise<void> {
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
            await walk(full, result);
        } else if (entry.endsWith(".rb")) {
            await scanFile(full, result);
        }
    }
}

async function scanFile(
    file: string,
    result: ValidatesScanResult,
): Promise<void> {
    const text = await fs.readFile(file, "utf8");
    if (!/\bvalidates\b/.test(text)) return; // fast-path
    const className = extractTopLevelClassName(text);
    if (!className) return;
    const override = extractTableNameOverride(text);
    const entity = override ?? inflectTableName(className);
    for (const decl of extractInclusionValidators(text)) {
        emitInclusion(file, text, entity, decl, result);
    }
}

// ---------------------------------------------------------------------------
// Validator declaration extraction
// ---------------------------------------------------------------------------

interface InclusionDecl {
    /** Field name on the model. */
    fieldName: string;
    /** Allowed value strings (in source order). */
    values: string[];
    /** Byte offset of the `validates` keyword. */
    indexInText: number;
}

// `validates :status, inclusion: { in: %w[a b c] }`
// Capture: 1 = field name, 2 = the in-array body (without the brackets).
const VALIDATES_PERCENT_W_RX =
    /\bvalidates\s+:([A-Za-z_]\w*)\s*,[^\n]*?\binclusion\s*:\s*\{[^{}]*?\bin\s*:\s*%w\[([^\]]*)\][^{}]*?\}/g;

// `validates :status, inclusion: { in: ["a", "b"] }`
const VALIDATES_BRACKETS_RX =
    /\bvalidates\s+:([A-Za-z_]\w*)\s*,[^\n]*?\binclusion\s*:\s*\{[^{}]*?\bin\s*:\s*\[([^\[\]]*)\][^{}]*?\}/g;

// `validates :status, inclusion: { in: %i[a b c] }` — symbol literal array
const VALIDATES_PERCENT_I_RX =
    /\bvalidates\s+:([A-Za-z_]\w*)\s*,[^\n]*?\binclusion\s*:\s*\{[^{}]*?\bin\s*:\s*%i\[([^\]]*)\][^{}]*?\}/g;

// Legacy macro form (still common in Rails 4-era apps and in
// codebases that survived multiple Rails upgrades):
//   validates_inclusion_of :status, in: %w[a b c]
//   validates_inclusion_of :role, in: ["admin", "member"]
//   validates_inclusion_of :tier, in: [:bronze, :silver]
// The `in:` argument lives at the top level of the call, not inside
// a hash like the modern `validates :foo, inclusion: { in: ... }`
// form, so we need separate patterns.
const LEGACY_PERCENT_W_RX =
    /\bvalidates_inclusion_of\s+:([A-Za-z_]\w*)\s*,[^\n]*?\bin\s*:\s*%w\[([^\]]*)\]/g;
const LEGACY_PERCENT_I_RX =
    /\bvalidates_inclusion_of\s+:([A-Za-z_]\w*)\s*,[^\n]*?\bin\s*:\s*%i\[([^\]]*)\]/g;
const LEGACY_BRACKETS_RX =
    /\bvalidates_inclusion_of\s+:([A-Za-z_]\w*)\s*,[^\n]*?\bin\s*:\s*\[([^\[\]]*)\]/g;

export function extractInclusionValidators(text: string): InclusionDecl[] {
    const stripped = stripRubyComments(text);
    const out: InclusionDecl[] = [];
    const collect = (
        rx: RegExp,
        parser: (raw: string) => string[],
    ): void => {
        for (const match of stripped.matchAll(rx)) {
            const body = sliceOriginalGroup(text, match, 2);
            if (body === null) continue;
            out.push({
                fieldName: match[1]!,
                values: parser(body),
                indexInText: match.index ?? 0,
            });
        }
    };
    // Order matters: the bracket-form regex would also match a
    // `%w[]` body if we'd allowed `[` inside `[^...]`, but we
    // tightened the inner class to `[^\[\]]` so the two patterns
    // are disjoint. Run all three; dedupe later if needed.
    collect(VALIDATES_PERCENT_W_RX, parsePercentWords);
    collect(VALIDATES_PERCENT_I_RX, parsePercentSymbols);
    collect(VALIDATES_BRACKETS_RX, parseBracketArray);
    // Legacy macro form — runs after the modern form so the modern
    // patterns get first crack at the source. The macro patterns
    // anchor on `validates_inclusion_of` which the modern matchers
    // don't even attempt, so the two are disjoint.
    collect(LEGACY_PERCENT_W_RX, parsePercentWords);
    collect(LEGACY_PERCENT_I_RX, parsePercentSymbols);
    collect(LEGACY_BRACKETS_RX, parseBracketArray);
    return out;
}

function sliceOriginalGroup(
    original: string,
    match: RegExpMatchArray,
    groupIdx: number,
): string | null {
    const captured = match[groupIdx];
    if (captured === undefined) return null;
    const startInStripped =
        (match.index ?? 0) + match[0].indexOf(captured);
    const endInStripped = startInStripped + captured.length;
    if (endInStripped > original.length) return null;
    return original.slice(startInStripped, endInStripped);
}

function parsePercentWords(body: string): string[] {
    // %w[active inactive pending] — whitespace-separated word
    // tokens. Empty entries dropped.
    return body
        .split(/\s+/)
        .map((s) => s.trim())
        .filter((s) => s.length > 0);
}

function parsePercentSymbols(body: string): string[] {
    // %i[a b c] — same shape as %w; symbols stored as strings
    // server-side.
    return parsePercentWords(body);
}

function parseBracketArray(body: string): string[] {
    // ["a", "b"] OR [:a, :b, :c] — split on top-level commas, then
    // strip the quote/symbol prefix.
    const out: string[] = [];
    for (const part of splitTopLevelCommas(body)) {
        const trimmed = part.trim();
        if (!trimmed) continue;
        // Quoted string?
        if (
            (trimmed.startsWith('"') && trimmed.endsWith('"')) ||
            (trimmed.startsWith("'") && trimmed.endsWith("'"))
        ) {
            out.push(trimmed.slice(1, -1));
            continue;
        }
        // Symbol?
        if (trimmed.startsWith(":")) {
            const sym = trimmed.slice(1);
            if (/^[A-Za-z_]\w*$/.test(sym)) out.push(sym);
            continue;
        }
        // Bare integer? — accept as a string value (rare but legal
        // in older Rails inclusion validators).
        if (/^-?\d+$/.test(trimmed)) out.push(trimmed);
    }
    return out;
}

function splitTopLevelCommas(text: string): string[] {
    const parts: string[] = [];
    let depth = 0;
    let inStr: '"' | "'" | null = null;
    let buf = "";
    for (let i = 0; i < text.length; i++) {
        const ch = text[i]!;
        if (inStr) {
            buf += ch;
            if (ch === "\\") {
                buf += text[i + 1] ?? "";
                i++;
                continue;
            }
            if (ch === inStr) inStr = null;
            continue;
        }
        if (ch === '"' || ch === "'") {
            inStr = ch as '"' | "'";
            buf += ch;
            continue;
        }
        if (ch === "{" || ch === "[" || ch === "(") {
            depth++;
            buf += ch;
            continue;
        }
        if (ch === "}" || ch === "]" || ch === ")") {
            if (depth > 0) depth--;
            buf += ch;
            continue;
        }
        if (ch === "," && depth === 0) {
            parts.push(buf);
            buf = "";
            continue;
        }
        buf += ch;
    }
    if (buf.length > 0) parts.push(buf);
    return parts;
}

// ---------------------------------------------------------------------------
// Comment + string stripping (same shape as enums.ts) — comments only,
// because we need string-literal bodies preserved to capture the values.
// ---------------------------------------------------------------------------

function stripRubyComments(text: string): string {
    const buf = text.split("");
    let inStr: '"' | "'" | null = null;
    let i = 0;
    while (i < buf.length) {
        const ch = buf[i]!;
        if (inStr) {
            if (ch === "\\") {
                i += 2;
                continue;
            }
            if (ch === inStr) inStr = null;
            i++;
            continue;
        }
        if (ch === "'" || ch === '"') {
            inStr = ch as '"' | "'";
            i++;
            continue;
        }
        if (ch === "#") {
            while (i < buf.length && buf[i] !== "\n") {
                buf[i] = " ";
                i++;
            }
            continue;
        }
        i++;
    }
    return buf.join("");
}

// ---------------------------------------------------------------------------
// Emission
// ---------------------------------------------------------------------------

function emitInclusion(
    file: string,
    text: string,
    entity: string,
    decl: InclusionDecl,
    result: ValidatesScanResult,
): void {
    let canonicalEntity: string;
    let canonicalField: string;
    try {
        canonicalEntity = sanitiseCanonical(entity);
        canonicalField = sanitiseCanonical(decl.fieldName);
    } catch (e) {
        if (e instanceof SanitiserError) {
            result.skipped.push({ file, reason: e.message });
            return;
        }
        throw e;
    }
    const enumName = `${canonicalEntity}.${canonicalField}`;
    const declLine = lineOf(text, decl.indexInText);
    for (const rawValue of decl.values) {
        let label: string;
        try {
            label = sanitiseLabel(prettyName(rawValue));
        } catch {
            continue;
        }
        result.fragments.push({
            term: label.toLowerCase(),
            canonical: { kind: "enum_value", enumName, rawValue },
            // Slightly lower than `enums.ts` (0.85) because the
            // inclusion validator is a runtime constraint that may
            // be overridden by `:if` / `:unless` clauses we don't
            // parse. The merge engine resolves conflicts.
            confidence: 0.75,
            locator: {
                file,
                line: declLine,
                layer: SourceLayer.Orm,
                extractor: "extractor-rails:validates",
            },
        });
    }
}

function prettyName(name: string): string {
    return name.replace(/_/g, " ").toLowerCase().trim() || name;
}

function lineOf(text: string, idx: number): number {
    let line = 1;
    for (let i = 0; i < idx; i++) {
        if (text[i] === "\n") line++;
    }
    return line;
}
