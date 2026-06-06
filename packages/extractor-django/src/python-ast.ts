/**
 * Tree-sitter-python integration.
 *
 * Mirrors `extractor-laravel/src/php-ast.ts`: lazy parser load, graceful
 * degradation when the native binding fails to load (CPU mismatch,
 * pre-built mismatch). Callers degrade to "skip the file" rather than
 * crash the whole run.
 *
 * The Python grammar exposes the constructs we care about cleanly:
 *
 *   - `class_definition` → `class Foo(models.Model): ...`
 *   - `assignment` → `name = models.CharField(verbose_name="Display")`
 *   - `call` → the `models.CharField(...)` expression on the rhs.
 *   - `keyword_argument` → `verbose_name="Display"` (name + value).
 *   - `string` / `integer` → literal nodes (we accept only static
 *     literals — non-static labels are runtime-resolved).
 *
 * We deliberately do NOT use the lower-level `child_count`/`child(i)`
 * positional walker; field-name access (`childForFieldName`) is robust
 * to grammar updates that reorder children.
 */

import { createRequire } from "node:module";
import type { default as ParserType } from "tree-sitter";

const requireFromModule = createRequire(import.meta.url);

let lazyParser: ParserType | null = null;
let lazyParserError: unknown = null;

interface Lazy {
	parser: ParserType;
}

/**
 * Get the shared parser. Returns `null` if tree-sitter could not be
 * loaded — typically because the native binding is missing for the
 * current CPU. Callers should record a "skipped" fragment and move on.
 */
export function getParser(): Lazy | null {
	if (lazyParser !== null) {
		return { parser: lazyParser };
	}
	if (lazyParserError !== null) {
		return null;
	}
	try {
		const Parser = requireFromModule("tree-sitter") as { new (): ParserType };
		const pyModule = requireFromModule("tree-sitter-python") as unknown;
		const parser = new Parser();
		parser.setLanguage(pyModule as Parameters<ParserType["setLanguage"]>[0]);
		lazyParser = parser;
		return { parser };
	} catch (e) {
		lazyParserError = e;
		return null;
	}
}

export type SyntaxNode = ParserType.SyntaxNode;

/**
 * Walk every named child recursively, depth-first. Caller-supplied
 * predicate decides which nodes to inspect — returning `true` from
 * `fn` short-circuits descent into that node's subtree (useful when
 * the caller has already extracted everything it needs from a class
 * body, for example).
 */
export function walk(
	node: SyntaxNode,
	fn: (n: SyntaxNode) => boolean | void,
): void {
	const stop = fn(node);
	if (stop === true) return;
	for (let i = 0; i < node.namedChildCount; i++) {
		const c = node.namedChild(i);
		if (c) walk(c, fn);
	}
}

/**
 * Decode a tree-sitter-python `string` node's literal value. Rejects
 * f-strings and any string with embedded interpolations — those are
 * runtime-resolved and not vocabulary-grade.
 *
 * The python grammar exposes string contents as a sequence of children:
 * `string_start`, zero or more `string_content` / `escape_sequence` /
 * `interpolation`, then `string_end`. We concatenate the static parts
 * and bail on interpolation.
 */
export function parsePythonStringLiteral(node: SyntaxNode): string | null {
	if (node.type !== "string") return null;
	let out = "";
	let isFString = false;
	for (let i = 0; i < node.namedChildCount; i++) {
		const c = node.namedChild(i);
		if (!c) continue;
		if (c.type === "string_start") {
			// Quote prefix may include `f`, `F`, `rb`, etc. — reject
			// anything carrying `f` so we don't mis-emit interpolated
			// labels as static vocabulary.
			const prefix = c.text.replace(/['"`].*$/s, "");
			if (/f/i.test(prefix)) isFString = true;
			continue;
		}
		if (c.type === "string_end") continue;
		if (c.type === "interpolation") return null;
		if (c.type === "string_content") {
			out += c.text;
			continue;
		}
		if (c.type === "escape_sequence") {
			out += decodeEscape(c.text);
			continue;
		}
	}
	if (isFString) return null;
	return out;
}

function decodeEscape(raw: string): string {
	if (raw.length < 2 || raw[0] !== "\\") return raw;
	const ch = raw[1];
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
			// Hex / unicode escape forms are rare in label literals;
			// surface them verbatim minus the leading backslash so the
			// sanitiser can reject them downstream.
			return ch ?? "";
	}
}
