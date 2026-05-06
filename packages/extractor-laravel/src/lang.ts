/**
 * Laravel `lang/` parser.
 *
 * Two formats:
 *
 * 1. **PHP arrays** — `lang/<locale>/<group>.php` returning an associative
 *    array. We don't run PHP; instead we recognise the simple
 *    `'key' => 'value'` and nested-array shape that 99% of Laravel projects
 *    use. Files that use anonymous functions or external constants are
 *    skipped (logged for follow-up via tree-sitter-php in v0.5).
 *
 * 2. **JSON files** — `lang/<locale>.json` and `lang/<locale>/<group>.json`.
 *    Plain UTF-8 JSON, parsed with `JSON.parse`.
 *
 * Output: a stream of {@link VocabFragment} records that the merge engine
 * consumes via the priority cascade. Only fragments whose canonical target
 * survives sanitisation reach the SemanticGraph.
 *
 * Mapping conventions:
 *
 * - Top-level groups commonly used for entity-level vocabulary
 *   (`models.php`, `entities.php`, `resources.php`) are treated as
 *   entity-label sources. Keys at the leaf level become the canonical
 *   entity name; values become the display label.
 * - Nested keys like `models.user.singular` / `models.user.plural` are
 *   merged into one entity entry with both labels populated.
 * - Other groups (forms, validation, …) are out of scope here — the
 *   Filament-form walker handles those once tree-sitter-php lands.
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

/** Result of one walk over a `lang/` directory. */
export interface LangScanResult {
    fragments: VocabFragment[];
    /** Files we recognised as Laravel lang sources but couldn't fully parse. */
    skipped: Array<{ file: string; reason: string }>;
}

/**
 * Recursively scan `langDir` and emit vocabulary fragments. Locale-aware:
 * the locale segment of the path becomes part of the locator and is
 * surfaced for downstream multi-locale dedup logic.
 */
export async function scanLangDir(langDir: string): Promise<LangScanResult> {
    const result: LangScanResult = { fragments: [], skipped: [] };
    let entries: string[];
    try {
        entries = await fs.readdir(langDir);
    } catch {
        return result; // empty dir is a no-op, not an error
    }

    for (const localeDirOrFile of entries) {
        const full = path.join(langDir, localeDirOrFile);
        // `.catch(() => null)` swallows the race where an entry vanishes
        // between `readdir` and `stat` (CI, watchers, deleted symlinks).
        const stat = await fs.stat(full).catch(() => null);
        if (!stat) continue;
        if (stat.isDirectory()) {
            // lang/<locale>/...
            const locale = localeDirOrFile;
            await scanLocaleDir(full, locale, result);
        } else if (localeDirOrFile.endsWith(".json")) {
            // lang/<locale>.json
            const locale = path.basename(localeDirOrFile, ".json");
            await scanJsonFile(full, locale, result);
        }
    }
    return result;
}

async function scanLocaleDir(
    dir: string,
    locale: string,
    result: LangScanResult,
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
            await scanLocaleDir(full, locale, result); // nested groups
        } else if (entry.endsWith(".php")) {
            await scanPhpFile(full, locale, result);
        } else if (entry.endsWith(".json")) {
            await scanJsonFile(full, locale, result);
        }
    }
}

// ---------------------------------------------------------------------------
// JSON parser
// ---------------------------------------------------------------------------

async function scanJsonFile(
    file: string,
    locale: string,
    result: LangScanResult,
): Promise<void> {
    const text = await fs.readFile(file, "utf8");
    let data: unknown;
    try {
        data = JSON.parse(text);
    } catch (e) {
        result.skipped.push({ file, reason: `invalid JSON: ${(e as Error).message}` });
        return;
    }
    if (!isPlainObject(data)) {
        result.skipped.push({ file, reason: "top-level must be an object" });
        return;
    }
    walkJson(data, [], file, locale, result);
}

function walkJson(
    node: unknown,
    keyStack: string[],
    file: string,
    locale: string,
    result: LangScanResult,
): void {
    if (typeof node === "string") {
        emitFragment(keyStack, node, file, locale, result, /*line*/ 1);
        return;
    }
    if (!isPlainObject(node)) return;
    for (const [k, v] of Object.entries(node)) {
        walkJson(v, [...keyStack, k], file, locale, result);
    }
}

// ---------------------------------------------------------------------------
// PHP array parser (regex-based — covers the 99% common shape)
//
// Recognised pattern:
//
//     return [
//         'user'  => 'Student',
//         'users' => 'Students',
//         'models' => [
//             'user' => ['singular' => 'Student', 'plural' => 'Students'],
//         ],
//     ];
//
// We scan for `'key' => 'value'` pairs and follow nested array literals
// by tracking bracket depth. Anything we can't parse is recorded in
// `skipped`. tree-sitter-php replaces this in v0.5 for full coverage.
// ---------------------------------------------------------------------------

async function scanPhpFile(
    file: string,
    locale: string,
    result: LangScanResult,
): Promise<void> {
    const text = await fs.readFile(file, "utf8");
    if (!/return\s*\[/.test(text) && !/return\s*array\s*\(/i.test(text)) {
        result.skipped.push({ file, reason: "no `return [...]` block found" });
        return;
    }
    const stripped = stripPhpComments(text);
    const start = stripped.search(/return\s*[\[(]/);
    if (start < 0) return;
    const slice = sliceBalanced(stripped, start);
    if (!slice) {
        result.skipped.push({ file, reason: "unbalanced `return [...]` block" });
        return;
    }
    walkPhpArray(slice.inner, [], file, locale, result);
}

function stripPhpComments(text: string): string {
    return text
        .replace(/\/\*[\s\S]*?\*\//g, " ")
        .replace(/(^|[^:])\/\/[^\n]*/g, "$1")
        .replace(/(^|[^:])#[^\n]*/g, "$1");
}

interface BalancedSlice {
    /** Inner text, between the brackets. */
    inner: string;
    /** Index just past the closing bracket. */
    endExclusive: number;
}

/** Slice the balanced `[...]` (or `(...)`) starting at the next opener
 *  on/after `start`. Returns the inner slice plus the position to
 *  resume scanning. */
function sliceBalanced(text: string, start: number): BalancedSlice | null {
    let i = start;
    while (i < text.length && text[i] !== "[" && text[i] !== "(") {
        i++;
    }
    if (i >= text.length) return null;
    const open = text[i] === "[" ? "[" : "(";
    const close = open === "[" ? "]" : ")";
    const innerStart = i + 1;
    let depth = 0;
    let j = i;
    let inStr: '"' | "'" | null = null;
    for (; j < text.length; j++) {
        const ch = text[j];
        if (inStr) {
            if (ch === "\\") {
                j++;
                continue;
            }
            if (ch === inStr) inStr = null;
            continue;
        }
        if (ch === '"' || ch === "'") {
            inStr = ch as '"' | "'";
            continue;
        }
        if (ch === open) depth++;
        else if (ch === close) {
            depth--;
            if (depth === 0)
                return { inner: text.slice(innerStart, j), endExclusive: j + 1 };
        }
    }
    return null;
}

function walkPhpArray(
    body: string,
    keyStack: string[],
    file: string,
    locale: string,
    result: LangScanResult,
): void {
    // Tokenise: find every `'key' => value` or `"key" => value` pair at the
    // top level of `body`. The value can be a string literal or a nested
    // array, which we recurse into.
    let i = 0;
    while (i < body.length) {
        // Skip whitespace + commas
        while (i < body.length && /[\s,]/.test(body[i]!)) i++;
        if (i >= body.length) break;

        const key = readPhpString(body, i);
        if (!key) {
            // Bareword keys, integer keys, dynamic keys — not in scope.
            // Move past the next entry separator and try again.
            i = nextTopLevelComma(body, i);
            if (i < 0) return;
            continue;
        }
        i = key.end;

        // Expect `=>`.
        while (i < body.length && /\s/.test(body[i]!)) i++;
        if (body.slice(i, i + 2) !== "=>") {
            i = nextTopLevelComma(body, i);
            if (i < 0) return;
            continue;
        }
        i += 2;
        while (i < body.length && /\s/.test(body[i]!)) i++;

        if (body[i] === "[" || body.slice(i, i + 6).toLowerCase() === "array(") {
            const slice = sliceBalanced(body, i);
            if (slice === null) {
                result.skipped.push({ file, reason: "unbalanced nested array" });
                return;
            }
            walkPhpArray(slice.inner, [...keyStack, key.value], file, locale, result);
            i = slice.endExclusive;
        } else {
            const val = readPhpString(body, i);
            if (val) {
                emitFragment(
                    [...keyStack, key.value],
                    val.value,
                    file,
                    locale,
                    result,
                    lineOf(body, i),
                );
                i = val.end;
            } else {
                // Non-string value — function call, constant, concat —
                // out of scope.
                i = nextTopLevelComma(body, i);
                if (i < 0) return;
            }
        }
    }
}

interface StringRead {
    value: string;
    end: number;
}

function readPhpString(body: string, start: number): StringRead | null {
    if (body[start] !== "'" && body[start] !== '"') return null;
    const quote = body[start]!;
    let i = start + 1;
    let out = "";
    while (i < body.length) {
        const ch = body[i]!;
        if (ch === "\\" && i + 1 < body.length) {
            const next = body[i + 1]!;
            // Single-quoted strings only honour \\ and \' escapes; double
            // honour the usual \n etc. Keep it pragmatic.
            if (quote === "'" && next !== "\\" && next !== "'") {
                out += ch;
            } else {
                out += unescapePhp(next);
                i += 2;
                continue;
            }
            i++;
            continue;
        }
        if (ch === quote) {
            return { value: out, end: i + 1 };
        }
        out += ch;
        i++;
    }
    return null;
}

function unescapePhp(ch: string): string {
    switch (ch) {
        case "n":
            return "\n";
        case "r":
            return "\r";
        case "t":
            return "\t";
        case "\\":
            return "\\";
        case "'":
            return "'";
        case '"':
            return '"';
        default:
            return ch;
    }
}

function nextTopLevelComma(body: string, start: number): number {
    let depth = 0;
    let inStr: '"' | "'" | null = null;
    for (let i = start; i < body.length; i++) {
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
        else if (ch === "]" || ch === ")") {
            if (depth === 0) return -1;
            depth--;
        } else if (ch === "," && depth === 0) {
            return i + 1;
        }
    }
    return -1;
}

function lineOf(body: string, idx: number): number {
    let line = 1;
    for (let i = 0; i < idx; i++) {
        if (body[i] === "\n") line++;
    }
    return line;
}

// ---------------------------------------------------------------------------
// emit
// ---------------------------------------------------------------------------

function emitFragment(
    keyStack: string[],
    value: string,
    file: string,
    locale: string,
    result: LangScanResult,
    line: number,
): void {
    if (keyStack.length === 0) return;

    // Mapping rule:
    //
    //   models.user.singular = "Student" → entity `user` (singular label)
    //   models.user.plural   = "Students" → entity `user` (plural label)
    //   user / users → entity if it survives sanitisation
    //
    // Anything else (validation strings, form labels) is left for the
    // Filament adapter to pick up.

    const last = keyStack[keyStack.length - 1]!;
    let canonicalCandidate: string | null = null;
    if (keyStack[0] === "models" || keyStack[0] === "entities" || keyStack[0] === "resources") {
        if (keyStack.length >= 2) {
            canonicalCandidate = keyStack[1] ?? null;
        }
    } else if (keyStack.length === 1) {
        // Flat `'user' => 'Student'` style.
        canonicalCandidate = last;
    }

    if (canonicalCandidate === null) return;

    let canonical: string;
    let label: string;
    try {
        canonical = sanitiseCanonical(canonicalCandidate);
        label = sanitiseLabel(value);
    } catch (e) {
        if (e instanceof SanitiserError) {
            result.skipped.push({ file, reason: e.message });
        } else {
            throw e;
        }
        return;
    }

    const isI18n = file.endsWith(".json");
    result.fragments.push({
        term: label.toLowerCase(),
        canonical: { kind: "entity", entity: canonical },
        confidence: 0.85,
        locator: {
            file,
            line,
            layer: SourceLayer.I18n,
            extractor: `extractor-laravel:lang:${locale}${isI18n ? ":json" : ":php"}`,
        },
    });
}

function isPlainObject(x: unknown): x is Record<string, unknown> {
    return typeof x === "object" && x !== null && !Array.isArray(x);
}
