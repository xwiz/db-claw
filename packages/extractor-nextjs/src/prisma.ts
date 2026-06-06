/**
 * Prisma `schema.prisma` parser.
 *
 * Prisma's schema DSL is small and stable: model definitions follow
 * a consistent shape that we can parse with a hand-rolled lexer-free
 * regex sweep. Example:
 *
 *     model User {
 *       id        Int      @id @default(autoincrement())
 *       email     String   @unique
 *       isActive  Boolean  @default(false) @map("is_active")
 *       tenantId  Int      @map("tenant_id")
 *       createdAt DateTime @default(now()) @map("created_at")
 *
 *       @@map("users")
 *       @@index([tenantId])
 *     }
 *
 * What we extract:
 *
 *  - **Table name**: `@@map("users")` if present, else the model
 *    identifier (lower-cased per Prisma's default `@@map`-less
 *    behaviour).
 *  - **Column DB name**: `@map("col")` on a field, else the field
 *    identifier.
 *  - **Column TS name**: the model field identifier.
 *  - **Field-existence fragments**: emitted at layer 2 (ORM) using the
 *    DB-side name as canonical and the prettified TS-side name as
 *    label, mirroring the Drizzle walker.
 *
 * Deliberately skipped by this Prisma reader:
 *
 *  - Relation fields with `@relation(...)` — emitting these requires a
 *    relationship-fragment design that the cascade orchestrator doesn't
 *    yet consume.
 *  - `enum` blocks — Stage 3 enum resolution lives in
 *    `semsql-natsql`'s transpile pass; the SemanticGraph already gets
 *    enum vocab from DB introspection.
 *  - View definitions (`view` block) — Prisma views are an unusual
 *    setup; defer to a follow-up walker.
 *  - Type aliases via `type Foo`. Same reasoning.
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

/** Result of scanning one project. */
export interface PrismaScanResult {
	fragments: VocabFragment[];
	/** Files we recognised but couldn't fully parse. */
	skipped: Array<{ file: string; reason: string }>;
}

const SCHEMA_PATH_CANDIDATES = [
	"prisma/schema.prisma",
	"schema.prisma",
	"src/prisma/schema.prisma",
	"apps/web/prisma/schema.prisma",
	"packages/db/prisma/schema.prisma",
];

/**
 * Find and parse every `schema.prisma` under the canonical Prisma
 * locations. Caller can also pass an explicit path via the standalone
 * {@link parsePrismaSchema} for non-conventional layouts.
 */
export async function scanPrismaSchemas(
	root: string,
): Promise<PrismaScanResult> {
	const result: PrismaScanResult = { fragments: [], skipped: [] };
	for (const sub of SCHEMA_PATH_CANDIDATES) {
		const full = path.join(root, sub);
		try {
			const text = await fs.readFile(full, "utf8");
			mergeInto(result, parsePrismaSchema(full, text));
		} catch {
			// file missing — try next candidate
		}
	}
	return result;
}

function mergeInto(into: PrismaScanResult, from: PrismaScanResult): void {
	into.fragments.push(...from.fragments);
	into.skipped.push(...from.skipped);
}

// ---------------------------------------------------------------------------
// Parser
// ---------------------------------------------------------------------------

/** Parse a `schema.prisma` file's text. Exposed for unit tests + custom layouts. */
export function parsePrismaSchema(
	file: string,
	text: string,
): PrismaScanResult {
	const result: PrismaScanResult = { fragments: [], skipped: [] };
	for (const block of extractModelBlocks(text)) {
		emitModel(file, text, block, result);
	}
	return result;
}

interface ModelBlock {
	/** Identifier after the `model` keyword. */
	name: string;
	/** Inner body between `{` and `}`. */
	body: string;
	/** Byte offset of `model` keyword in the source. */
	indexInText: number;
}

const MODEL_DECL_RX = /\bmodel\s+([A-Za-z_][\w]*)\s*\{/g;

export function extractModelBlocks(text: string): ModelBlock[] {
	// Strip line + block comments by replacing their characters with
	// spaces — preserves byte offsets so `indexInText` and the body
	// start/end indices still line up against the original source.
	// Without this pre-pass, `// model X { fake }` matches
	// MODEL_DECL_RX and emits a phantom block.
	const stripped = stripComments(text);
	const out: ModelBlock[] = [];
	for (const match of stripped.matchAll(MODEL_DECL_RX)) {
		const name = match[1]!;
		const matchEnd = (match.index ?? 0) + match[0].length;
		const closeIdx = findMatchingClose(stripped, matchEnd - 1);
		if (closeIdx < 0) continue;
		out.push({
			name,
			// Body text comes from the original `text` (with comments
			// intact). The downstream field extractor strips comments
			// line-by-line, so retaining them here is harmless and
			// preserves diagnostics that need to surface comment
			// context (e.g. `///` doc lines).
			body: text.slice(matchEnd, closeIdx),
			indexInText: match.index ?? 0,
		});
	}
	return out;
}

/**
 * Replace every byte inside a `//` line comment or `/* ... *\/` block
 * comment with a space (newlines preserved). Length and per-byte
 * offsets stay identical to the input — every other parser in this
 * file indexes against the original source positions, so we cannot
 * shorten the string.
 */
function stripComments(text: string): string {
	const buf = text.split("");
	let inLine = false;
	let inBlock = false;
	let inStr: '"' | "'" | "`" | null = null;
	for (let i = 0; i < buf.length; i++) {
		const ch = buf[i]!;
		const next = buf[i + 1];
		if (inLine) {
			if (ch === "\n") {
				inLine = false;
			} else {
				buf[i] = " ";
			}
			continue;
		}
		if (inBlock) {
			if (ch === "*" && next === "/") {
				buf[i] = " ";
				buf[i + 1] = " ";
				i++;
				inBlock = false;
			} else if (ch !== "\n") {
				buf[i] = " ";
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
			buf[i] = " ";
			buf[i + 1] = " ";
			inLine = true;
			i++;
			continue;
		}
		if (ch === "/" && next === "*") {
			buf[i] = " ";
			buf[i + 1] = " ";
			inBlock = true;
			i++;
			continue;
		}
		if (ch === '"' || ch === "'" || ch === "`") {
			inStr = ch as '"' | "'" | "`";
		}
	}
	return buf.join("");
}

function findMatchingClose(text: string, openIdx: number): number {
	if (text[openIdx] !== "{") return -1;
	let depth = 0;
	let inStr: '"' | "'" | "`" | null = null;
	let inLineComment = false;
	for (let i = openIdx; i < text.length; i++) {
		const ch = text[i]!;
		if (inLineComment) {
			if (ch === "\n") inLineComment = false;
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
		if (ch === "/" && text[i + 1] === "/") {
			inLineComment = true;
			i++;
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
// Field + @@map extraction
// ---------------------------------------------------------------------------

interface PrismaField {
	/** TS-side field identifier. */
	tsName: string;
	/** DB-side column name (from `@map(...)`, else identical to tsName). */
	dbName: string;
	/** Byte offset within the model body — used for line lookup. */
	indexInBody: number;
	/** True if this is a relation field (`User`, `User?`, `User[]`) — skipped. */
	isRelation: boolean;
}

// Field declarations live at top-level of the model body. We line-scan
// rather than regex-on-body because Prisma field shape is one
// declaration per line, terminated by a newline; multiline scalar
// declarations don't exist.
//
// Pattern per line: `  identifier  Type[?|[]]   @attr1 @attr2(...)`
// We accept identifier + type word; @map("...") arg if present.
const FIELD_LINE_RX = /^\s*([A-Za-z_][\w]*)\s+([A-Za-z_][\w?\[\]]*)\s*(.*)$/;

const MAP_ATTR_RX = /@map\(\s*"((?:\\.|[^"\\])*)"\s*\)/;
const TABLE_MAP_RX = /@@map\(\s*"((?:\\.|[^"\\])*)"\s*\)/;

// Built-in Prisma scalars; anything else is treated as a relation /
// custom type and skipped by this Prisma reader.
const SCALAR_TYPES = new Set([
	"String",
	"Int",
	"BigInt",
	"Float",
	"Decimal",
	"Boolean",
	"DateTime",
	"Json",
	"Bytes",
	"Unsupported",
]);

export function extractFields(body: string): PrismaField[] {
	const out: PrismaField[] = [];
	let cursor = 0;
	for (const line of body.split("\n")) {
		const trimmed = line.trim();
		// Skip block-level annotations, comments, and blank lines.
		if (
			!trimmed ||
			trimmed.startsWith("//") ||
			trimmed.startsWith("@@") ||
			trimmed.startsWith("/*")
		) {
			cursor += line.length + 1;
			continue;
		}
		const match = FIELD_LINE_RX.exec(line);
		if (!match) {
			cursor += line.length + 1;
			continue;
		}
		const tsName = match[1]!;
		const typeRaw = match[2]!;
		const tail = match[3] ?? "";
		const baseType = typeRaw.replace(/[?\[\]]/g, "");
		const isRelation = !SCALAR_TYPES.has(baseType);
		const mapMatch = MAP_ATTR_RX.exec(tail);
		const dbName = mapMatch?.[1] ?? tsName;
		out.push({
			tsName,
			dbName,
			indexInBody: cursor + line.indexOf(tsName),
			isRelation,
		});
		cursor += line.length + 1;
	}
	return out;
}

/** Extract the `@@map("table_name")` annotation if present. */
export function extractTableMap(body: string): string | null {
	const m = TABLE_MAP_RX.exec(body);
	return m?.[1] ?? null;
}

// ---------------------------------------------------------------------------
// Emission
// ---------------------------------------------------------------------------

function emitModel(
	file: string,
	text: string,
	model: ModelBlock,
	result: PrismaScanResult,
): void {
	const tableMap = extractTableMap(model.body);
	// Prisma's default mapping when @@map is absent is the model name
	// verbatim — Prisma does NOT pluralise or lower-case by default.
	// Database extractors will canonicalise downstream, but we feed
	// the literal so cross-checks against the live schema match.
	const rawTableName = tableMap ?? model.name;

	let canonicalEntity: string;
	try {
		canonicalEntity = sanitiseCanonical(rawTableName);
	} catch (e) {
		if (e instanceof SanitiserError) {
			result.skipped.push({ file, reason: e.message });
			return;
		}
		throw e;
	}

	const modelStartLine = lineOf(text, model.indexInText);

	for (const field of extractFields(model.body)) {
		if (field.isRelation) continue;
		let canonicalField: string;
		let label: string;
		try {
			canonicalField = sanitiseCanonical(field.dbName);
			label = sanitiseLabel(prettyName(field.tsName));
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
				line: modelStartLine + countNewlines(model.body, field.indexInBody),
				layer: SourceLayer.Orm,
				extractor: "extractor-nextjs:prisma",
			},
		});
	}
}

function prettyName(name: string): string {
	const stripped = name.replace(/Id$/, "").replace(/_id$/i, "");
	return (
		stripped
			.replace(/([a-z0-9])([A-Z])/g, "$1 $2")
			.replace(/_/g, " ")
			.toLowerCase()
			.trim() || name
	);
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
