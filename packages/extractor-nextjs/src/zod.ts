/**
 * Zod schema walker.
 *
 * Zod is the canonical TS-side validator in modern Next.js (App
 * Router server actions, RHF resolvers, tRPC routers). Its
 * `.describe("...")` API attaches human-facing metadata to a field
 * — the same string that surfaces in OpenAPI exporters, generated
 * forms, and error UIs. That makes it a high-fidelity vocabulary
 * source at the `ApiResource` layer (=4).
 *
 * Recognised pattern (v0.5 cut):
 *
 * ```ts
 * export const userSchema = z.object({
 *   email:     z.string().email().describe("Email Address"),
 *   isActive:  z.boolean().describe("Account Status"),
 * });
 * ```
 *
 * Schema-name → entity normalisation: drop conventional suffixes
 * (`Schema`, `Insert`, `Update`, `Validator`, `Form`), then
 * snake_case the residual identifier. `userSchema` / `UserSchema` /
 * `UserInsertSchema` → `user`. `userFormValidator` → `user`.
 *
 * Out of scope for the v0.5 cut:
 *
 *   - Object spreads (`...baseFields`) — common but require
 *     symbol-tracking that ts-morph's full AST would give us. Real
 *     Zod codebases declare each field once, so this hurts less in
 *     practice.
 *   - Nested `z.object` field schemas — recursed for sub-objects but
 *     the current emitter doesn't track parent property paths.
 *   - `.describe()` on non-string arguments — e.g. lazy-evaluated
 *     bindings; rejected as non-static.
 *   - The `.meta({ description: "..." })` (Zod 3.22+) form — easy to
 *     add later, not yet widespread.
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

/** Result of walking one project for Zod schemas. */
export interface ZodScanResult {
    fragments: VocabFragment[];
    /** Files we recognised but couldn't parse cleanly. */
    skipped: Array<{ file: string; reason: string }>;
}

const SOURCE_DIR_CANDIDATES = [
    "src",
    "app",
    "lib",
    "schemas",
    "validators",
    "actions",
    "pages",
];

const TS_FILE_RX = /\.(ts|tsx|mts|cts)$/i;

/** Recursively walk conventional source dirs and emit fragments. */
export async function scanZodSchemas(root: string): Promise<ZodScanResult> {
    const result: ZodScanResult = { fragments: [], skipped: [] };
    for (const sub of SOURCE_DIR_CANDIDATES) {
        await walk(path.join(root, sub), result);
    }
    return result;
}

async function walk(dir: string, result: ZodScanResult): Promise<void> {
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
            if (entry === "node_modules" || entry === "dist" || entry === ".next") {
                continue;
            }
            await walk(full, result);
        } else if (TS_FILE_RX.test(entry)) {
            await scanFile(full, result);
        }
    }
}

async function scanFile(file: string, result: ZodScanResult): Promise<void> {
    const text = await fs.readFile(file, "utf8");
    if (!text.includes("z.object")) return; // cheap pre-filter
    for (const block of findZodObjectBlocks(text)) {
        const entity = entityFromSchemaName(block.schemaName);
        if (!entity) continue;
        let canonicalEntity: string;
        try {
            canonicalEntity = sanitiseCanonical(entity);
        } catch (e) {
            if (e instanceof SanitiserError) {
                result.skipped.push({ file, reason: e.message });
                continue;
            }
            throw e;
        }
        for (const field of findFieldsWithDescribe(block.body, block.bodyOffset)) {
            let canonicalField: string;
            let label: string;
            try {
                // TS conventionally uses camelCase property names
                // (`isActive`); the SemanticGraph stores snake_case
                // canonical fields. Convert eagerly so downstream
                // joiners line up with the DB-extracted shape.
                canonicalField = sanitiseCanonical(toSnakeCase(field.fieldName));
                label = sanitiseLabel(field.label);
            } catch (e) {
                if (e instanceof SanitiserError) {
                    result.skipped.push({ file, reason: e.message });
                    continue;
                }
                throw e;
            }
            result.fragments.push({
                term: label.toLowerCase(),
                canonical: {
                    kind: "field",
                    field: `${canonicalEntity}.${canonicalField}`,
                },
                confidence: 0.85,
                locator: {
                    file,
                    line: lineOf(text, field.absoluteOffset),
                    layer: SourceLayer.ApiResource,
                    extractor: "extractor-nextjs:zod",
                },
            });
        }
    }
}

// ---------------------------------------------------------------------------
// Block extraction — `<name> = z.object({ ... })`
// ---------------------------------------------------------------------------

interface ZodObjectBlock {
    schemaName: string;
    body: string;
    /** Absolute offset of the body's first character within the source. */
    bodyOffset: number;
}

const BLOCK_DECL_RX =
    /(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*(?::\s*[^=]+)?=\s*z\.object\s*\(\s*\{/g;

/**
 * Walk every `[<exports>]?(const|let|var) <name> = z.object({ ... })`
 * declaration in `text`. Body extraction is brace-balanced — handles
 * nested object/array literals, template strings, line comments, and
 * regex-looking literals via state-machine scanning rather than
 * regex (regex over balanced braces is unreliable).
 */
export function findZodObjectBlocks(text: string): ZodObjectBlock[] {
    const out: ZodObjectBlock[] = [];
    BLOCK_DECL_RX.lastIndex = 0;
    let m: RegExpExecArray | null;
    while ((m = BLOCK_DECL_RX.exec(text)) !== null) {
        const schemaName = m[1]!;
        const bodyStart = m.index + m[0].length;
        const closeIdx = matchClosingBrace(text, bodyStart - 1);
        if (closeIdx === -1) continue; // mismatched braces; skip
        out.push({
            schemaName,
            body: text.slice(bodyStart, closeIdx),
            bodyOffset: bodyStart,
        });
        // Skip past this body so the regex doesn't re-match nested
        // `z.object` inside it (those are walked recursively below).
        BLOCK_DECL_RX.lastIndex = closeIdx + 1;
    }
    return out;
}

/**
 * Scan a TS source string starting at the position of an opening
 * `{` and return the index of its matching closing `}`. Honours:
 *
 *   - String literals (`'`, `"`, `` ` ``) — `${...}` template
 *     interpolations are treated as code, so braces inside
 *     interpolation count.
 *   - Single-line `//` comments.
 *   - Block `/* ... *\/` comments.
 *
 * Returns -1 if the brace is unbalanced.
 */
function matchClosingBrace(text: string, openIdx: number): number {
    if (text[openIdx] !== "{") return -1;
    let depth = 0;
    let i = openIdx;
    const n = text.length;
    // Stack tracks the *outer* lexical context. When we enter a
    // template `${`, push `template`; when its `}` closes, pop.
    const ctx: Array<"code" | "template"> = ["code"];
    while (i < n) {
        const ch = text[i]!;
        const top = ctx[ctx.length - 1]!;
        if (top === "code") {
            if (ch === "{") {
                depth++;
                i++;
                continue;
            }
            if (ch === "}") {
                depth--;
                if (depth === 0) return i;
                i++;
                continue;
            }
            // Strings.
            if (ch === '"' || ch === "'") {
                i = skipString(text, i, ch);
                continue;
            }
            if (ch === "`") {
                i = skipTemplate(text, i, ctx);
                continue;
            }
            if (ch === "/" && text[i + 1] === "/") {
                i = text.indexOf("\n", i + 2);
                if (i === -1) return -1;
                continue;
            }
            if (ch === "/" && text[i + 1] === "*") {
                const end = text.indexOf("*/", i + 2);
                if (end === -1) return -1;
                i = end + 2;
                continue;
            }
            i++;
            continue;
        }
        // top === "template" — we're inside `${...}` but only after
        // the brace that started it has been counted. Treat as code
        // until we see the matching `}`, which transitions back.
        if (ch === "}") {
            ctx.pop();
            i++;
            continue;
        }
        if (ch === "{") {
            depth++;
            i++;
            continue;
        }
        if (ch === '"' || ch === "'") {
            i = skipString(text, i, ch);
            continue;
        }
        if (ch === "`") {
            i = skipTemplate(text, i, ctx);
            continue;
        }
        i++;
    }
    return -1;
}

function skipString(text: string, start: number, quote: string): number {
    let i = start + 1;
    while (i < text.length) {
        const ch = text[i];
        if (ch === "\\") {
            i += 2;
            continue;
        }
        if (ch === quote) return i + 1;
        if (ch === "\n" && quote !== "`") return i + 1; // unterminated; bail
        i++;
    }
    return text.length;
}

function skipTemplate(
    text: string,
    start: number,
    ctx: Array<"code" | "template">,
): number {
    let i = start + 1;
    while (i < text.length) {
        const ch = text[i];
        if (ch === "\\") {
            i += 2;
            continue;
        }
        if (ch === "`") return i + 1;
        if (ch === "$" && text[i + 1] === "{") {
            // Enter template-interpolation context.
            ctx.push("template");
            return i + 2;
        }
        i++;
    }
    return text.length;
}

// ---------------------------------------------------------------------------
// Field extraction — `<key>: <chain>.describe('label')`
// ---------------------------------------------------------------------------

interface ZodFieldHit {
    fieldName: string;
    label: string;
    /** Absolute offset (in the original source) where the property starts. */
    absoluteOffset: number;
}

/**
 * Walk a z.object body and return every property declaration whose
 * RHS contains a chained `.describe("string")`. Handles property
 * keys that are bare identifiers (`email`), single-quoted, or
 * double-quoted. Skips computed `[expr]:` keys, function shorthand
 * `email() {}`, and method definitions.
 */
export function findFieldsWithDescribe(
    body: string,
    bodyOffset: number,
): ZodFieldHit[] {
    const out: ZodFieldHit[] = [];
    // Walk top-level properties only — nested `z.object({...})`
    // bodies have their own block declaration walker pass elsewhere.
    let i = 0;
    const n = body.length;
    let propStart = 0;
    let depth = 0;
    while (i < n) {
        const ch = body[i]!;
        if (depth === 0 && (ch === "," || ch === "\n")) {
            const segment = body.slice(propStart, i);
            const hit = parsePropertySegment(segment);
            if (hit) {
                out.push({
                    fieldName: hit.fieldName,
                    label: hit.label,
                    absoluteOffset: bodyOffset + propStart,
                });
            }
            propStart = i + 1;
            i++;
            continue;
        }
        if (ch === "{" || ch === "(" || ch === "[") {
            depth++;
            i++;
            continue;
        }
        if (ch === "}" || ch === ")" || ch === "]") {
            depth--;
            i++;
            continue;
        }
        if (ch === '"' || ch === "'") {
            i = skipString(body, i, ch);
            continue;
        }
        if (ch === "`") {
            i = skipTemplate(body, i, ["code"]);
            continue;
        }
        if (ch === "/" && body[i + 1] === "/") {
            i = body.indexOf("\n", i + 2);
            if (i === -1) i = n;
            continue;
        }
        if (ch === "/" && body[i + 1] === "*") {
            const end = body.indexOf("*/", i + 2);
            if (end === -1) i = n;
            else i = end + 2;
            continue;
        }
        i++;
    }
    // Tail segment.
    if (propStart < n) {
        const segment = body.slice(propStart);
        const hit = parsePropertySegment(segment);
        if (hit) {
            out.push({
                fieldName: hit.fieldName,
                label: hit.label,
                absoluteOffset: bodyOffset + propStart,
            });
        }
    }
    return out;
}

const PROP_KEY_RX = /^\s*(?:["']([^"']+)["']|([A-Za-z_$][\w$]*))\s*:/;
const DESCRIBE_RX = /\.describe\s*\(\s*(["'`])((?:\\.|(?!\1).)*)\1\s*\)/;

function parsePropertySegment(
    segment: string,
): { fieldName: string; label: string } | null {
    const keyMatch = PROP_KEY_RX.exec(segment);
    if (!keyMatch) return null;
    const fieldName = keyMatch[1] ?? keyMatch[2]!;
    const desc = DESCRIBE_RX.exec(segment);
    if (!desc) return null;
    const raw = desc[2]!;
    // Decode escape sequences so `\\"` round-trips. Keep simple.
    const label = raw
        .replace(/\\(.)/g, (_full, ch: string) => {
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
    return { fieldName, label };
}

// ---------------------------------------------------------------------------
// Entity name derivation
// ---------------------------------------------------------------------------

const SUFFIXES = ["Schema", "Validator", "Form", "Insert", "Update", "Select"];

/**
 * Reduce a Zod-schema variable name to a canonical entity. Strips
 * conventional suffixes in order of likelihood, then snake_cases.
 *
 * Examples:
 *   - `userSchema` → `user`
 *   - `UserSchema` → `user`
 *   - `UserInsertSchema` → `user`
 *   - `Posts` → `posts`
 *   - `users` → `users`
 *
 * Returns null when stripping yields the empty string (e.g. lone
 * `Schema`), so the caller skips emission rather than emit a
 * nameless entity.
 */
export function entityFromSchemaName(name: string): string | null {
    // Reject identifiers that are exactly a noise suffix — "Schema",
    // "Validator", etc. carry no entity information on their own.
    for (const suf of SUFFIXES) {
        if (name.toLowerCase() === suf.toLowerCase()) return null;
    }
    let trimmed = name;
    let didTrim = true;
    while (didTrim) {
        didTrim = false;
        for (const suf of SUFFIXES) {
            if (trimmed.endsWith(suf) && trimmed.length > suf.length) {
                trimmed = trimmed.slice(0, -suf.length);
                didTrim = true;
            }
        }
    }
    if (!trimmed) return null;
    return toSnakeCase(trimmed);
}

function toSnakeCase(name: string): string {
    return name
        .replace(/([a-z0-9])([A-Z])/g, "$1_$2")
        .replace(/([A-Z]+)([A-Z][a-z])/g, "$1_$2")
        .toLowerCase();
}

// ---------------------------------------------------------------------------
// Source-line lookup
// ---------------------------------------------------------------------------

function lineOf(text: string, offset: number): number {
    let line = 1;
    for (let i = 0; i < offset && i < text.length; i++) {
        if (text[i] === "\n") line++;
    }
    return line;
}
