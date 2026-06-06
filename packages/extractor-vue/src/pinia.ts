/**
 * Pinia store walker — `defineStore("name", { state: () => ({ ... }) })`.
 *
 * Pinia is the canonical Vue 3 state-management library. Stores
 * declare their state shape via the `state` factory:
 *
 *     export const useUserStore = defineStore("user", {
 *       state: () => ({
 *         email: "",
 *         isActive: false,
 *         tenantId: null as number | null,
 *       }),
 *       actions: { ... },
 *     });
 *
 * What we extract:
 *
 *  - **Entity name**: the first string-literal arg to `defineStore` —
 *    `"user"` here. Treated as a layer-2 (ORM) entity surface even
 *    though Pinia is a UI store; in practice the store keys mirror
 *    the backend resource names in well-architected apps, and the
 *    cross-extractor merge engine resolves conflicts via layer
 *    priority.
 *  - **Field names**: the keys on the object literal returned by the
 *    `state` factory.
 *
 * Pinia's "setup syntax" form (`defineStore("user", () => { const
 * email = ref(""); return { email }; })`) is recognised at the
 * `return { ... }` site. Both styles emit the same fragment shape.
 *
 * This walker is regex-driven; the Vue compiler-backed SFC walker
 * upgrade replaces this with a real AST visitor that handles
 * computed-property keys, spread, and shorthand returns.
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

/** Result of walking one project. */
export interface PiniaScanResult {
	fragments: VocabFragment[];
	/** Stores we recognised but couldn't fully parse. */
	skipped: Array<{ file: string; reason: string }>;
	/** Map from store name → field set. Used by the v-model walker
	 *  to promote bare-ref `v-model="email"` to the qualified
	 *  `entity.field` form. */
	entityIndex: Map<string, Set<string>>;
}

const STORE_DIR_CANDIDATES = ["src/stores", "src/store", "stores", "store"];

const STORE_FILE_RX = /\.(ts|tsx|mts|cts|js|jsx|mjs|cjs)$/;

export async function scanPiniaStores(root: string): Promise<PiniaScanResult> {
	const result: PiniaScanResult = {
		fragments: [],
		skipped: [],
		entityIndex: new Map(),
	};
	for (const sub of STORE_DIR_CANDIDATES) {
		await walk(path.join(root, sub), result);
	}
	return result;
}

async function walk(dir: string, result: PiniaScanResult): Promise<void> {
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
			if (entry === "node_modules" || entry === "dist") continue;
			await walk(full, result);
		} else if (STORE_FILE_RX.test(entry)) {
			await scanFile(full, result);
		}
	}
}

async function scanFile(file: string, result: PiniaScanResult): Promise<void> {
	const text = await fs.readFile(file, "utf8");
	if (!/\bdefineStore\s*\(/.test(text)) return;
	for (const store of extractStores(text)) {
		emitStore(file, text, store, result);
	}
}

// ---------------------------------------------------------------------------
// defineStore("name", ...) extraction
// ---------------------------------------------------------------------------

interface PiniaStore {
	/** First-arg store name. */
	storeName: string;
	/** Body of the second-arg object literal OR setup function. */
	body: string;
	/** Whether the body is the setup-function form (vs options-object). */
	isSetupForm: boolean;
	/** Byte offset of `defineStore` keyword. */
	indexInText: number;
}

const DEFINE_STORE_RX =
	/\bdefineStore\s*\(\s*(['"])((?:\\.|(?!\1).)*)\1\s*,\s*(\{|\(\s*\)\s*=>\s*\{|\(\s*\)\s*=>\s*\()/g;

export function extractStores(text: string): PiniaStore[] {
	const stripped = stripJsComments(text);
	const out: PiniaStore[] = [];
	for (const match of stripped.matchAll(DEFINE_STORE_RX)) {
		const storeName = match[2]!;
		const opener = match[3]!;
		const matchEnd = (match.index ?? 0) + match[0].length;
		// The opener captured the `{` or `(`. Find the matching close.
		const openChar = stripped[matchEnd - 1]!;
		const closeIdx = findMatchingClose(stripped, matchEnd - 1, openChar);
		if (closeIdx < 0) continue;
		out.push({
			storeName,
			body: text.slice(matchEnd, closeIdx),
			isSetupForm: opener.startsWith("("),
			indexInText: match.index ?? 0,
		});
	}
	return out;
}

// ---------------------------------------------------------------------------
// Field extraction — both options-object + setup-form
// ---------------------------------------------------------------------------

/**
 * In options form, look for `state: () => ({ ... })` and return the
 * top-level keys. In setup form, look for `return { ... }` (with
 * shorthand keys) and return the top-level keys.
 */
export function extractStoreFields(
	body: string,
	isSetupForm: boolean,
): string[] {
	if (isSetupForm) {
		return extractReturnedKeys(body);
	}
	return extractStateKeys(body);
}

const STATE_RX = /\bstate\s*:\s*\(\s*\)\s*=>\s*\(\s*\{/;

function extractStateKeys(body: string): string[] {
	const m = STATE_RX.exec(body);
	if (!m) return [];
	const start = (m.index ?? 0) + m[0].length - 1; // position of `{`
	const end = findMatchingClose(body, start, "{");
	if (end < 0) return [];
	return collectTopLevelKeys(body.slice(start + 1, end));
}

function extractReturnedKeys(body: string): string[] {
	// Setup-form: the return statement is at the bottom of the
	// function body. We look for the last `return { ... }` since
	// earlier returns are typically inside conditionals.
	const RX = /\breturn\s*\{/g;
	let lastIdx = -1;
	for (const m of body.matchAll(RX)) lastIdx = m.index ?? -1;
	if (lastIdx < 0) return [];
	const open = body.indexOf("{", lastIdx);
	const close = findMatchingClose(body, open, "{");
	if (close < 0) return [];
	return collectTopLevelKeys(body.slice(open + 1, close));
}

/**
 * Collect property keys at depth 0 of an object-literal body. Walks
 * the body as a tiny state machine that tracks the position within a
 * key-value pair so a value-side identifier (`false`, `null`, a
 * function name) cannot be misread as a shorthand key:
 *
 *  - **expectKey** (initial): look for an identifier or `[computed]`
 *    or `...spread`. After matching identifier `K`, peek the next
 *    non-whitespace char. If `:`, emit `K` and switch to skipValue.
 *    If `,` or end-of-body, emit `K` (true shorthand) and stay in
 *    expectKey. If anything else, the identifier was probably a stray
 *    token (skip it).
 *  - **skipValue**: walk forward at depth 0, jumping over braces /
 *    parens / strings / comments, until the top-level `,` (or end of
 *    body) terminates the value expression — at which point we flip
 *    back to expectKey.
 *
 * Handles correctly:
 *
 *  - `email: ""` → `email`, value `""` skipped
 *  - `isActive: false` → `isActive`, value `false` skipped (no longer
 *    misread as a shorthand key)
 *  - `tenantId: null as number | null` → `tenantId`, complex value
 *    skipped at top-level `,`
 *  - `{ email, isActive }` → both as shorthand
 *  - `[computed]: value` → skipped (can't know runtime key)
 *  - spread `...other` → skipped
 */
function collectTopLevelKeys(text: string): string[] {
	const keys: string[] = [];
	let mode: "expectKey" | "skipValue" = "expectKey";
	let depth = 0;
	let inStr: '"' | "'" | "`" | null = null;
	let inLineComment = false;
	let inBlockComment = false;
	let i = 0;
	while (i < text.length) {
		const ch = text[i]!;
		const next = text[i + 1];
		// ---- comment + string skipping ---------------------------
		if (inLineComment) {
			if (ch === "\n") inLineComment = false;
			i++;
			continue;
		}
		if (inBlockComment) {
			if (ch === "*" && next === "/") {
				inBlockComment = false;
				i += 2;
			} else {
				i++;
			}
			continue;
		}
		if (inStr) {
			if (ch === "\\") {
				i += 2;
				continue;
			}
			if (ch === inStr) inStr = null;
			i++;
			continue;
		}
		if (ch === "/" && next === "/") {
			inLineComment = true;
			i += 2;
			continue;
		}
		if (ch === "/" && next === "*") {
			inBlockComment = true;
			i += 2;
			continue;
		}
		if (ch === '"' || ch === "'" || ch === "`") {
			inStr = ch as '"' | "'" | "`";
			i++;
			continue;
		}
		// ---- depth tracking --------------------------------------
		if (ch === "{" || ch === "[" || ch === "(") {
			depth++;
			i++;
			continue;
		}
		if (ch === "}" || ch === "]" || ch === ")") {
			if (depth > 0) depth--;
			i++;
			continue;
		}
		// ---- mode-specific behaviour at depth 0 ------------------
		if (depth !== 0) {
			i++;
			continue;
		}
		if (mode === "skipValue") {
			// Top-level comma terminates the value; flip back to key
			// expectation.
			if (ch === ",") mode = "expectKey";
			i++;
			continue;
		}
		// expectKey
		if (/\s/.test(ch)) {
			i++;
			continue;
		}
		if (ch === ",") {
			// Stray comma between fields, or trailing — stay in
			// expectKey.
			i++;
			continue;
		}
		if (ch === "." && text.slice(i, i + 3) === "...") {
			// Spread — skip the spread expression like a value.
			mode = "skipValue";
			i += 3;
			continue;
		}
		if (ch === "[") {
			// Computed key — skipped. The above depth handler would
			// already pop us out, but we must also avoid recording a
			// key from inside the brackets.
			// Falls through to depth handler on next iter.
			i++;
			continue;
		}
		const idMatch = /^[A-Za-z_$][\w$]*/.exec(text.slice(i));
		if (!idMatch) {
			// Anything else at depth 0 is unexpected token — slide on.
			i++;
			continue;
		}
		const key = idMatch[0]!;
		const after = i + key.length;
		let j = after;
		while (j < text.length && /\s/.test(text[j]!)) j++;
		const sep = text[j];
		if (sep === ":") {
			keys.push(key);
			mode = "skipValue";
			i = j + 1;
			continue;
		}
		if (sep === "," || sep === undefined) {
			// Shorthand — emit and keep expecting keys.
			keys.push(key);
			i = j;
			continue;
		}
		// Other separators (e.g. `=` in TS as-cast inside a value
		// context, or `(` from a method shorthand) → not a key.
		if (sep === "(") {
			// Method shorthand `email() { ... }` — emit, then skip
			// the parameter list + body as a value.
			keys.push(key);
			mode = "skipValue";
			i = j;
			continue;
		}
		i = after;
	}
	return keys;
}

// ---------------------------------------------------------------------------
// Comment stripping + brace matching (shared)
// ---------------------------------------------------------------------------

function stripJsComments(text: string): string {
	const buf = text.split("");
	let inLine = false;
	let inBlock = false;
	let inStr: '"' | "'" | "`" | null = null;
	for (let i = 0; i < buf.length; i++) {
		const ch = buf[i]!;
		const next = buf[i + 1];
		if (inLine) {
			if (ch === "\n") inLine = false;
			else buf[i] = " ";
			continue;
		}
		if (inBlock) {
			if (ch === "*" && next === "/") {
				buf[i] = " ";
				buf[i + 1] = " ";
				i++;
				inBlock = false;
			} else if (ch !== "\n") buf[i] = " ";
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

function findMatchingClose(
	text: string,
	openIdx: number,
	openChar: string,
): number {
	if (text[openIdx] !== openChar) return -1;
	const closeChar = openChar === "{" ? "}" : openChar === "[" ? "]" : ")";
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
		if (ch === openChar) depth++;
		else if (ch === closeChar) {
			depth--;
			if (depth === 0) return i;
		}
	}
	return -1;
}

// ---------------------------------------------------------------------------
// Emission
// ---------------------------------------------------------------------------

function emitStore(
	file: string,
	text: string,
	store: PiniaStore,
	result: PiniaScanResult,
): void {
	let canonicalEntity: string;
	try {
		canonicalEntity = sanitiseCanonical(store.storeName);
	} catch (e) {
		if (e instanceof SanitiserError) {
			result.skipped.push({ file, reason: e.message });
			return;
		}
		throw e;
	}
	const fields = extractStoreFields(store.body, store.isSetupForm);
	if (fields.length === 0) {
		result.skipped.push({
			file,
			reason: `defineStore('${store.storeName}') had no recognisable state shape`,
		});
		return;
	}
	const indexEntry =
		result.entityIndex.get(canonicalEntity) ?? new Set<string>();
	const storeStartLine = lineOf(text, store.indexInText);
	for (const fieldName of fields) {
		let canonicalField: string;
		let label: string;
		try {
			canonicalField = sanitiseCanonical(fieldName);
			label = sanitiseLabel(prettyName(fieldName));
		} catch {
			continue;
		}
		indexEntry.add(canonicalField);
		result.fragments.push({
			term: label.toLowerCase(),
			canonical: {
				kind: "field",
				field: `${canonicalEntity}.${canonicalField}`,
			},
			// 0.5 — Pinia stores reflect the developer's mental model
			// but aren't the source of truth for the DB schema. Lower
			// confidence than Drizzle/Prisma so the merge engine
			// prefers ORM-layer fragments when they conflict.
			confidence: 0.5,
			locator: {
				file,
				line: storeStartLine,
				layer: SourceLayer.Orm,
				extractor: "extractor-vue:pinia",
			},
		});
	}
	result.entityIndex.set(canonicalEntity, indexEntry);
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
