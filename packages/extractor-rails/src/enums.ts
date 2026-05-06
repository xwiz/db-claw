/**
 * Rails ActiveRecord `enum` declaration walker.
 *
 * Rails enums map a symbol to an integer (or string) and are the
 * dominant value-vocabulary surface in idiomatic Rails apps:
 *
 *     class User < ApplicationRecord
 *       enum status: { active: 0, archived: 1, banned: 2 }
 *     end
 *
 * The cascade needs to bind a NL token like "archived" to the
 * `users.status` column AND to the raw value `1` (or `"archived"`
 * for string-backed enums) so the rendered SQL is dialect-correct.
 *
 * Supported syntaxes:
 *
 *  - **Hash with integer values**:
 *    `enum status: { active: 0, archived: 1 }`
 *  - **Hash with string values** (Rails 7+):
 *    `enum status: { active: "active", archived: "archived" }`
 *  - **Array form** (auto-numbered, deprecated but still common):
 *    `enum status: [:draft, :published]`
 *  - **Rails 7 positional-arg form**:
 *    `enum :status, { pending: 0, paid: 1 }`
 *    `enum :priority, [:low, :medium, :high]`
 *
 * Each enum entry yields one fragment of kind `enum_value` with:
 *  - `enumName` = `<entity>.<field>` (dotted)
 *  - `rawValue` = the canonical raw value (string of the integer for
 *    integer-backed enums; the literal string for string-backed)
 *  - `term` = the symbol name (e.g. "archived")
 *
 * The entity name is derived from the class name via a tiny
 * inflector (`User` → `users`, `OrderItem` → `order_items`,
 * `Person` → `people` is NOT special-cased; v0.5 inflector handles
 * the >95% pluralisation case, irregulars are deferred to v1.0).
 *
 * Skipped at v0.5:
 *
 *  - `enum :status, validate: true, prefix: true` — option args
 *    don't affect canonical names; we ignore them.
 *  - `self.table_name = "custom"` — class-level overrides; we use
 *    the inflected class name. The schema.rb walker is the source
 *    of truth for table-name canonicalisation; the merge engine
 *    resolves discrepancies via layer + confidence.
 *  - STI subclasses — child class would emit duplicate enums
 *    against its own inflected name; the merge engine dedupes by
 *    canonical.
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

/** Result of walking the models directory. */
export interface EnumScanResult {
    fragments: VocabFragment[];
    /** Files we recognised but couldn't fully parse. */
    skipped: Array<{ file: string; reason: string }>;
}

const MODELS_DIR_CANDIDATES = ["app/models"];

/** Scan `app/models/**\/*.rb` for `enum` declarations. */
export async function scanRailsEnums(root: string): Promise<EnumScanResult> {
    const result: EnumScanResult = { fragments: [], skipped: [] };
    for (const sub of MODELS_DIR_CANDIDATES) {
        await walk(path.join(root, sub), result);
    }
    return result;
}

async function walk(dir: string, result: EnumScanResult): Promise<void> {
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

async function scanFile(file: string, result: EnumScanResult): Promise<void> {
    const text = await fs.readFile(file, "utf8");
    if (!/\benum\b/.test(text)) return; // fast-path
    const className = extractTopLevelClassName(text);
    if (!className) return;
    // Prefer an explicit `self.table_name = "..."` override; fall
    // back to the conventional inflector. STI subclasses commonly
    // declare `self.table_name = parent.table_name` (or omit it
    // entirely and let Rails resolve at runtime); we treat the
    // string-literal form as the only authoritative override.
    const override = extractTableNameOverride(text);
    const entity = override ?? inflectTableName(className);
    for (const decl of extractEnumDeclarations(text)) {
        emitEnum(file, text, entity, decl, result);
    }
}

// ---------------------------------------------------------------------------
// Class-name extraction (top-level only)
// ---------------------------------------------------------------------------

const CLASS_RX = /\bclass\s+([A-Z][\w]*)\s*<\s*[A-Z][\w:]*/;

/** Pull the first top-level class name out of a Rails model file. */
export function extractTopLevelClassName(text: string): string | null {
    const stripped = stripRubyComments(text);
    const m = CLASS_RX.exec(stripped);
    return m ? m[1]! : null;
}

const TABLE_NAME_RX =
    /\bself\.table_name\s*=\s*(['"])((?:\\.|(?!\1).)*)\1/;

/**
 * Pull a `self.table_name = "custom"` override out of a model file,
 * if present. Returns the string-literal value verbatim; the caller
 * canonicalises via `sanitiseCanonical`. Non-literal forms (e.g.
 * `self.table_name = Settings.table_name`) are out of scope at
 * v0.5 — those skip the override and fall back to the inflector.
 *
 * Uses a comment-only stripper (NOT the string-blanking
 * `stripRubyComments`) because the override RHS is itself a
 * string literal — blanking string bodies would erase the value.
 */
export function extractTableNameOverride(text: string): string | null {
    const stripped = stripCommentsOnly(text);
    const m = TABLE_NAME_RX.exec(stripped);
    return m ? m[2]! : null;
}

/**
 * Blank Ruby `#`-line comments only. String literals are left intact
 * so callers that need to read a string-literal RHS still see it.
 * Newlines preserved so byte offsets stay aligned.
 */
function stripCommentsOnly(text: string): string {
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
// Enum declaration extraction
// ---------------------------------------------------------------------------

interface EnumDecl {
    /** Field name on the model — `status`, `priority`, etc. */
    fieldName: string;
    /** Map from symbol → raw value string. Auto-numbered for arrays. */
    values: Map<string, string>;
    /** Byte offset of the `enum` keyword within the source. */
    indexInText: number;
}

/**
 * Match every `enum` declaration in the file. Two forms supported:
 *
 *   enum status: { active: 0, archived: 1 }
 *   enum :status, { active: 0, archived: 1 }
 *   enum status: [:draft, :published]
 *   enum :status, [:draft, :published]
 */
const ENUM_HASH_KW_RX =
    /\benum\s+([A-Za-z_]\w*)\s*:\s*\{([^{}]*)\}/g;
const ENUM_ARR_KW_RX =
    /\benum\s+([A-Za-z_]\w*)\s*:\s*\[([^\[\]]*)\]/g;
const ENUM_HASH_POS_RX =
    /\benum\s+:([A-Za-z_]\w*)\s*,\s*\{([^{}]*)\}/g;
const ENUM_ARR_POS_RX =
    /\benum\s+:([A-Za-z_]\w*)\s*,\s*\[([^\[\]]*)\]/g;

export function extractEnumDeclarations(text: string): EnumDecl[] {
    // Match on the stripped (comment + string blanked) text so doc
    // strings can't masquerade as declarations. Re-slice the inner
    // hash / array body from the ORIGINAL text — string-backed
    // enums have value strings that the strip pass blanked, which
    // would otherwise produce empty values.
    const stripped = stripRubyComments(text);
    const out: EnumDecl[] = [];
    const collect = (
        rx: RegExp,
        parser: (raw: string) => Map<string, string>,
    ) => {
        for (const match of stripped.matchAll(rx)) {
            const body = sliceOriginalBody(text, stripped, match);
            if (body === null) continue;
            out.push({
                fieldName: match[1]!,
                values: parser(body),
                indexInText: match.index ?? 0,
            });
        }
    };
    collect(ENUM_HASH_KW_RX, parseHashLiteral);
    collect(ENUM_HASH_POS_RX, parseHashLiteral);
    collect(ENUM_ARR_KW_RX, parseArrayLiteral);
    collect(ENUM_ARR_POS_RX, parseArrayLiteral);
    return out;
}

/**
 * Given a regex match on the stripped text, return the corresponding
 * inner-body slice from the ORIGINAL text. The outer regex captures
 * group 2 as the inside of `{...}` or `[...]`; we locate that span
 * positionally and lift it from the original so string-literal
 * values survive.
 */
function sliceOriginalBody(
    original: string,
    _stripped: string,
    match: RegExpMatchArray,
): string | null {
    const startInStripped = (match.index ?? 0) + match[0].indexOf(match[2]!);
    const endInStripped = startInStripped + match[2]!.length;
    if (endInStripped > original.length) return null;
    return original.slice(startInStripped, endInStripped);
}

/** Parse `active: 0, archived: 1` or `active: "active", archived: "archived"`. */
function parseHashLiteral(text: string): Map<string, string> {
    const out = new Map<string, string>();
    // Tokenise on commas at depth 0 (no nested braces possible here
    // because the outer regex already prevented nested `{` `}` via
    // `[^{}]*`).
    for (const part of splitTopLevelCommas(text)) {
        const m = /^\s*([A-Za-z_]\w*)\s*:\s*(.+?)\s*$/.exec(part);
        if (!m) continue;
        const key = m[1]!;
        const valueRaw = m[2]!.trim();
        const cleaned = stripQuotesIfPresent(valueRaw);
        out.set(key, cleaned);
    }
    return out;
}

/** Parse `:draft, :published, :archived` → auto-number 0,1,2. */
function parseArrayLiteral(text: string): Map<string, string> {
    const out = new Map<string, string>();
    let idx = 0;
    for (const part of splitTopLevelCommas(text)) {
        const trimmed = part.trim();
        if (!trimmed) continue;
        const m = /^:([A-Za-z_]\w*)$/.exec(trimmed);
        if (m) {
            out.set(m[1]!, String(idx));
            idx++;
        }
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

function stripQuotesIfPresent(s: string): string {
    if (
        (s.startsWith('"') && s.endsWith('"')) ||
        (s.startsWith("'") && s.endsWith("'"))
    ) {
        return s.slice(1, -1);
    }
    return s;
}

// ---------------------------------------------------------------------------
// Inflector — class name → table name
// ---------------------------------------------------------------------------

/**
 * Convert a Ruby class name to the conventional Rails table name.
 *
 * Pluralisation handles the most common English suffix rules (CamelCase
 * with snake_case conversion + plural suffix). Irregular plurals
 * (person → people, child → children) are deferred to v1.0; they
 * surface to the merge engine which can resolve via the schema.rb
 * walker's authoritative entity name.
 */
export function inflectTableName(className: string): string {
    const snake = className
        .replace(/([a-z0-9])([A-Z])/g, "$1_$2")
        .toLowerCase();
    return pluralise(snake);
}

function pluralise(word: string): string {
    if (/(s|x|z|ch|sh)$/.test(word)) return word + "es";
    if (/[^aeiou]y$/.test(word)) return word.slice(0, -1) + "ies";
    if (word.endsWith("fe")) return word.slice(0, -2) + "ves";
    if (word.endsWith("f")) return word.slice(0, -1) + "ves";
    return word + "s";
}

// ---------------------------------------------------------------------------
// Comment stripping (shared with schema_rb walker semantics)
// ---------------------------------------------------------------------------

/**
 * Replace every Ruby comment AND string-literal body with spaces
 * (newlines preserved, byte offsets unchanged). Stripping strings
 * is required because a docstring or constant assignment that
 * happens to contain `enum status: { ... }` would otherwise emit
 * a phantom enum declaration alongside the real one. The real
 * tree-sitter walker in v1.0 will distinguish via AST node type;
 * v0.5 takes the conservative path and ignores all string content.
 */
function stripRubyComments(text: string): string {
    const buf = text.split("");
    let inStr: '"' | "'" | null = null;
    let i = 0;
    while (i < buf.length) {
        const ch = buf[i]!;
        if (inStr) {
            if (ch === "\\") {
                // Escape sequence — blank both the backslash and
                // the next char (unless newline, which we preserve).
                buf[i] = " ";
                if (i + 1 < buf.length && buf[i + 1] !== "\n") {
                    buf[i + 1] = " ";
                }
                i += 2;
                continue;
            }
            if (ch === inStr) {
                buf[i] = " ";
                inStr = null;
                i++;
                continue;
            }
            // Inside the string body — blank everything except
            // newlines so subsequent line-aware logic still aligns.
            if (ch !== "\n") buf[i] = " ";
            i++;
            continue;
        }
        if (ch === "'" || ch === '"') {
            inStr = ch as '"' | "'";
            buf[i] = " ";
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

function emitEnum(
    file: string,
    text: string,
    entity: string,
    decl: EnumDecl,
    result: EnumScanResult,
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
    for (const [symbol, rawValue] of decl.values) {
        let label: string;
        try {
            label = sanitiseLabel(prettyName(symbol));
        } catch {
            continue;
        }
        result.fragments.push({
            term: label.toLowerCase(),
            canonical: { kind: "enum_value", enumName, rawValue },
            confidence: 0.85,
            locator: {
                file,
                line: declLine,
                layer: SourceLayer.Orm,
                extractor: "extractor-rails:enums",
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
