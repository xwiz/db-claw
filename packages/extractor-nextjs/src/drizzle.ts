/**
 * Drizzle schema parser.
 *
 * Drizzle is the most popular TS-native ORM in modern Next.js apps.
 * Schema files declare tables as object literals consumed by helpers
 * named after the dialect — `pgTable` / `mysqlTable` / `sqliteTable`:
 *
 *     import { pgTable, text, integer, timestamp, boolean }
 *       from "drizzle-orm/pg-core";
 *
 *     export const users = pgTable("users", {
 *       id: integer("id").primaryKey(),
 *       email: text("email").notNull(),
 *       isActive: boolean("is_active").default(false),
 *       tenantId: integer("tenant_id").notNull(),
 *       createdAt: timestamp("created_at").defaultNow(),
 *     });
 *
 * What the walker extracts:
 *
 *  - **Table name**: the first string-literal argument to `*Table()`. The
 *    JS-side variable name (`users`) is the canonical entity in the
 *    SemanticGraph; we cross-check that the variable name and the
 *    string match (Drizzle convention) so a `users_legacy` table
 *    aliased as `users` doesn't silently mislabel.
 *  - **Column DB names**: the first string-literal argument to each
 *    column-type helper (`text("email")` → `email`).
 *  - **Column TS names**: the property key on the object literal
 *    (`isActive: ...` → `isActive`).
 *  - **Field-existence fragments**: emitted at layer 2 (ORM) using the
 *    DB-side name as canonical and the prettified TS-side name as the
 *    user-facing label (`isActive` → "is active"). The two together
 *    give the cascade enough vocabulary to resolve "is active" → the
 *    `is_active` column without a frontend label.
 *
 * The parser is regex-driven (no tree-sitter dep) because Drizzle
 * schemas have a stable, narrow surface — the >95% case is one
 * `pgTable("name", {...})` per file with literal string arguments.
 * Files that use computed names (`pgTable(getTableName(), ...)`) or
 * spread other schema objects skip out via the `skipped` log; the
 * tree-sitter-typescript walker in v0.5 covers the remainder.
 *
 * Deliberate non-goals (deferred):
 *
 *  - Relationship inference from `references()`. Cleanly extractable
 *    but the fragment shape needs design (see `Canonical.relationship`)
 *    and the cascade orchestrator doesn't consume them in v0.2.
 *  - Index / unique-constraint extraction.
 *  - Enum types from `pgEnum("status", ["active", "archived"])`. v0.5.
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

/** Result of scanning one project. */
export interface DrizzleScanResult {
    fragments: VocabFragment[];
    /** Files we recognised as schema sources but couldn't fully parse. */
    skipped: Array<{ file: string; reason: string }>;
}

const SCHEMA_DIR_CANDIDATES = [
    "src/db",
    "src/db/schema",
    "src/lib/db",
    "src/lib/db/schema",
    "src/schemas",
    "lib/db",
    "lib/db/schema",
    "drizzle",
    "drizzle/schema",
    "db",
    "db/schema",
    "schemas",
];

/**
 * Scan typical Drizzle schema locations under `root`. Returns a flat
 * fragment list across every detected schema file. Files outside the
 * known schema dirs are ignored — Drizzle's docs and `drizzle-kit`
 * defaults steer projects into one of these locations, and walking
 * `src/**` indiscriminately would pick up lots of non-schema TS that
 * happens to import from `drizzle-orm`.
 */
export async function scanDrizzleSchemas(root: string): Promise<DrizzleScanResult> {
    const result: DrizzleScanResult = { fragments: [], skipped: [] };
    for (const sub of SCHEMA_DIR_CANDIDATES) {
        await walk(path.join(root, sub), result);
    }
    return result;
}

async function walk(dir: string, result: DrizzleScanResult): Promise<void> {
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
        } else if (/\.(ts|tsx|mts|cts)$/.test(entry)) {
            await scanFile(full, result);
        }
    }
}

async function scanFile(file: string, result: DrizzleScanResult): Promise<void> {
    const text = await fs.readFile(file, "utf8");
    if (!/from\s+['"]drizzle-orm/.test(text)) {
        return; // not a Drizzle schema file
    }
    for (const table of extractTables(text)) {
        emitTable(file, text, table, result);
    }
}

// ---------------------------------------------------------------------------
// Table-decl extraction
// ---------------------------------------------------------------------------

interface DrizzleTable {
    /** TS-side variable / export name. */
    varName: string;
    /** DB-side table name (first arg to `*Table`). */
    dbName: string;
    /** Inner braced text of the column object literal. */
    body: string;
    /** Byte offset of the `{` opening the body — used for line lookup. */
    bodyOpenIndex: number;
}

const TABLE_DECL_RX =
    /(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:pgTable|mysqlTable|sqliteTable)\s*\(\s*(['"])((?:\\.|(?!\2).)*)\2\s*,\s*\{/g;

export function extractTables(text: string): DrizzleTable[] {
    const out: DrizzleTable[] = [];
    for (const match of text.matchAll(TABLE_DECL_RX)) {
        const varName = match[1]!;
        const dbName = match[3]!;
        const matchEnd = (match.index ?? 0) + match[0].length;
        // The regex consumed the `{`. Find the matching `}` while
        // ignoring braces inside strings.
        const closeIdx = findMatchingClose(text, matchEnd - 1);
        if (closeIdx < 0) continue;
        out.push({
            varName,
            dbName,
            body: text.slice(matchEnd, closeIdx),
            bodyOpenIndex: matchEnd - 1,
        });
    }
    return out;
}

function findMatchingClose(text: string, openIdx: number): number {
    if (text[openIdx] !== "{") return -1;
    let depth = 0;
    let inStr: '"' | "'" | "`" | null = null;
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
        if (ch === '"' || ch === "'" || ch === "`") {
            inStr = ch as '"' | "'" | "`";
            continue;
        }
        if (ch === "{") depth++;
        else if (ch === "}") {
            depth--;
            if (depth === 0) return i;
        }
    }
    return -1;
}

// ---------------------------------------------------------------------------
// Column extraction
// ---------------------------------------------------------------------------

/**
 * Recognise `propName: typeHelper("dbName"...)` pairs at the **top
 * level** of a table body. Drizzle column declarations live directly
 * under the `pgTable("...", { ... })` body brace; anything nested
 * inside `.default({ ... })`, `.$type<...>({ ... })`, an inner
 * function call, or a string interpolation is *not* a column and
 * must not be emitted — otherwise the layer-2 vocabulary fills up
 * with fake `entity.foo` fields that don't exist on the DB.
 *
 * We achieve this by walking the body once with brace + paren depth
 * tracking and string-state tracking; any `propName: typeHelper(...)`
 * match where either depth is non-zero is skipped.
 */
const COLUMN_RX =
    /\b([A-Za-z_$][\w$]*)\s*:\s*[A-Za-z_$][\w$]*\s*\(\s*(['"])((?:\\.|(?!\2).)*)\2/g;

interface DrizzleColumn {
    /** TS-side property name. */
    tsName: string;
    /** DB-side column name. */
    dbName: string;
    /** Byte offset within the body. */
    indexInBody: number;
}

export function extractColumns(body: string): DrizzleColumn[] {
    const topLevelMask = computeTopLevelMask(body);
    const out: DrizzleColumn[] = [];
    for (const match of body.matchAll(COLUMN_RX)) {
        const idx = match.index ?? 0;
        if (!topLevelMask[idx]) continue;
        out.push({
            tsName: match[1]!,
            dbName: match[3]!,
            indexInBody: idx,
        });
    }
    return out;
}

/**
 * Build a `body.length`-sized boolean array — `true` at byte offsets
 * that sit at brace depth 0 AND paren depth 0 (i.e. the table body's
 * top level), `false` everywhere else. Comments and string literals
 * are also marked `false` so that a `prop:` substring inside a
 * docstring or template literal can't trigger a column emission.
 *
 * Constructed once per call to `extractColumns`; O(n) in body length.
 */
function computeTopLevelMask(body: string): boolean[] {
    const mask = new Array<boolean>(body.length).fill(false);
    let braceDepth = 0;
    let parenDepth = 0;
    let inStr: '"' | "'" | "`" | null = null;
    let inLineComment = false;
    let inBlockComment = false;
    for (let i = 0; i < body.length; i++) {
        const ch = body[i]!;
        const next = body[i + 1];
        if (inLineComment) {
            if (ch === "\n") inLineComment = false;
            continue;
        }
        if (inBlockComment) {
            if (ch === "*" && next === "/") {
                inBlockComment = false;
                i++;
            }
            continue;
        }
        if (inStr) {
            if (ch === "\\") {
                i++;
                continue;
            }
            if (ch === inStr) inStr = null;
            continue;
        }
        if (ch === "/" && next === "/") {
            inLineComment = true;
            i++;
            continue;
        }
        if (ch === "/" && next === "*") {
            inBlockComment = true;
            i++;
            continue;
        }
        if (ch === '"' || ch === "'" || ch === "`") {
            inStr = ch as '"' | "'" | "`";
            continue;
        }
        if (ch === "{") {
            braceDepth++;
            continue;
        }
        if (ch === "}") {
            if (braceDepth > 0) braceDepth--;
            continue;
        }
        if (ch === "(") {
            parenDepth++;
            continue;
        }
        if (ch === ")") {
            if (parenDepth > 0) parenDepth--;
            continue;
        }
        mask[i] = braceDepth === 0 && parenDepth === 0;
    }
    return mask;
}

// ---------------------------------------------------------------------------
// Emission
// ---------------------------------------------------------------------------

function emitTable(
    file: string,
    text: string,
    table: DrizzleTable,
    result: DrizzleScanResult,
): void {
    let canonicalEntity: string;
    try {
        canonicalEntity = sanitiseCanonical(table.dbName);
    } catch (e) {
        if (e instanceof SanitiserError) {
            result.skipped.push({ file, reason: e.message });
            return;
        }
        throw e;
    }

    const tableLine = lineOf(text, table.bodyOpenIndex);
    // Variable-name vs DB-name mismatch is informational, not fatal —
    // emit anyway, but record so `semsql doctor` can surface it.
    if (table.varName.toLowerCase() !== table.dbName.toLowerCase()) {
        result.skipped.push({
            file,
            reason: `var name '${table.varName}' differs from table name '${table.dbName}' — verify intentional`,
        });
    }

    for (const col of extractColumns(table.body)) {
        let canonicalField: string;
        let label: string;
        try {
            canonicalField = sanitiseCanonical(col.dbName);
            label = sanitiseLabel(prettyName(col.tsName));
        } catch {
            continue; // bad column — silent drop
        }
        result.fragments.push({
            term: label.toLowerCase(),
            canonical: { kind: "field", field: `${canonicalEntity}.${canonicalField}` },
            confidence: 0.7,
            locator: {
                file,
                line: tableLine + countNewlines(table.body, col.indexInBody),
                layer: SourceLayer.Orm,
                extractor: "extractor-nextjs:drizzle",
            },
        });
    }
}

function prettyName(name: string): string {
    // camelCase / snake_case → space-separated lower-case label,
    // dropping a trailing `Id` segment so `tenantId` → "tenant" rather
    // than "tenant id" (consistent with the Eloquent walker in
    // `extractor-laravel`).
    const stripped = name.replace(/Id$/, "").replace(/_id$/i, "");
    return stripped
        .replace(/([a-z0-9])([A-Z])/g, "$1 $2")
        .replace(/_/g, " ")
        .toLowerCase()
        .trim() || name;
}

function lineOf(text: string, idx: number): number {
    let line = 1;
    for (let i = 0; i < idx; i++) {
        if (text[i] === "\n") line++;
    }
    return line;
}

function countNewlines(text: string, upTo: number): number {
    let n = 0;
    for (let i = 0; i < upTo; i++) {
        if (text[i] === "\n") n++;
    }
    return n;
}
