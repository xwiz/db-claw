/**
 * Tree-sitter-php integration.
 *
 * Replaces the regex-based `MAKE_LABEL_RX` walker in `filament.ts` with a
 * real PHP parser. The regex approach handles ~80% of shipping Filament
 * code; the AST walker handles the long tail:
 *
 *  - Split-line chains across many modifier calls.
 *  - Nested parentheses in modifier arguments
 *    (`->options([...])`, `->visible(fn () => $auth)`,
 *    `->default(now())`).
 *  - Conditional modifiers (`->when($cond, fn ($c) => $c->required())`).
 *  - Multi-line label expressions like `->label(__('users.full_name'))`
 *    (we still skip these — they require i18n key resolution — but we
 *    *recognise* and silently drop them instead of mis-parsing).
 *  - Trait imports (`use HasLabel;`) that influence label resolution
 *    (recognised but currently not followed).
 *
 * The parser is lazy-loaded once per process — tree-sitter native modules
 * have non-trivial cold-start cost (~30 ms) and we don't want to pay that
 * for projects without any Filament Resource files. If tree-sitter fails
 * to load (rare; usually a pre-built binary mismatch on exotic CPUs), the
 * caller should fall back to the regex path in `filament.ts`.
 */

import type { default as ParserType } from "tree-sitter";

let lazyParser: ParserType | null = null;
let lazyParserError: unknown = null;

interface Lazy {
    parser: ParserType;
}

/**
 * Get the shared parser. Returns `null` if tree-sitter could not be loaded
 * (callers should then fall back to the regex walker).
 *
 * The require chain is wrapped in try/catch because tree-sitter ships a
 * native binding; if Node can't load it (CPU mismatch, rebuild needed),
 * we want to degrade gracefully rather than crash the whole extractor.
 */
export function getParser(): Lazy | null {
    if (lazyParser !== null) {
        return { parser: lazyParser };
    }
    if (lazyParserError !== null) {
        return null;
    }
    try {
        // CommonJS require shim — `tree-sitter` ships only CJS at the
        // moment. Using `createRequire` keeps us compatible with the
        // ESM-everywhere TypeScript build.
        const { createRequire } = require("node:module") as {
            createRequire: (filename: string) => (id: string) => unknown;
        };
        const r = createRequire(__filename);
        const Parser = r("tree-sitter") as { new (): ParserType };
        const phpModule = r("tree-sitter-php") as { php: unknown };
        const parser = new Parser();
        parser.setLanguage(phpModule.php as Parameters<ParserType["setLanguage"]>[0]);
        lazyParser = parser;
        return { parser };
    } catch (e) {
        lazyParserError = e;
        return null;
    }
}

/** A `Type::make('field')->...->label('Display')` chain found via AST walk. */
export interface MakeLabelChain {
    field: string;
    label: string;
    /** 1-indexed line of the `::make` token. */
    line: number;
}

/**
 * A `Type::make('field')->...->label(__('lang.key'))` chain — i18n-bound.
 * Filament v3 and v4 codebases use this pattern when labels are localised
 * via Laravel's `__()` helper; the literal label text lives in
 * `lang/*.php` instead of inline. The merge engine resolves the i18n key
 * against the lang index at SDK time.
 */
export interface MakeI18nChain {
    field: string;
    /** Lang-key argument to `__()`, e.g. `"users.full_name"`. */
    i18nKey: string;
    /** 1-indexed line of the `::make` token. */
    line: number;
}

/**
 * Walk the AST of `text` and return every `Type::make('field')->...
 * ->label('Display')` chain. The walker is robust to:
 *
 *  - Modifiers in any order between `make` and `label`.
 *  - Nested parens inside modifier arguments.
 *  - Newlines and comments anywhere in the chain.
 *
 * It explicitly skips:
 *
 *  - Chains where the `make` argument is not a static string literal
 *    (e.g. `Type::make($field)`).
 *  - Chains where the `label` argument is not a static string literal
 *    (e.g. `->label(__('key'))` — i18n resolution lives in v0.5).
 *  - Chains rooted at a non-`make` static call.
 *
 * Returns `null` if the parser could not be loaded — callers should fall
 * back to the regex path.
 */
export function extractMakeLabelChainsAst(text: string): MakeLabelChain[] | null {
    const lazy = getParser();
    if (!lazy) return null;
    // tree-sitter-php only parses statements after a `<?php` tag. Files
    // off-disk always have one; test fixtures and inline PHP fragments
    // sometimes don't. Inject one transparently and offset line numbers
    // back so callers see the original 1-indexed line.
    const hasTag = /<\?(php|=|\s)/.test(text.slice(0, 32));
    const source = hasTag ? text : `<?php\n${text}`;
    const lineOffset = hasTag ? 0 : 1;
    const tree = lazy.parser.parse(source);
    const out: MakeLabelChain[] = [];
    visit(tree.rootNode, (node) => {
        if (node.type !== "member_call_expression") return;
        const methodNode = node.childForFieldName("name");
        if (!methodNode || methodNode.text !== "label") return;

        const labelLit = firstStringArgument(node);
        if (labelLit === null) return;

        const root = followChainToRoot(node);
        if (!root) return;
        if (root.type !== "scoped_call_expression") return;
        const rootMethod = root.childForFieldName("name");
        if (!rootMethod || rootMethod.text !== "make") return;
        const fieldLit = firstStringArgument(root);
        if (fieldLit === null) return;

        out.push({
            field: fieldLit,
            label: labelLit,
            line: Math.max(1, root.startPosition.row + 1 - lineOffset),
        });
    });
    return out;
}

/**
 * Walk the AST of `text` and return every `Type::make('field')->...
 * ->label(__('lang.key'))` chain. Behaves like
 * [`extractMakeLabelChainsAst`] but matches the i18n-helper form and
 * captures the key instead of a literal label.
 *
 * Filament v3 and v4 codebases use this pattern heavily (often
 * majority of labels in a localised app). Without this scanner the
 * regex / static-string AST walker silently drops them, which leaves
 * UI-layer (layer 6) vocabulary undetected and forces the cascade to
 * fall back on the lower-fidelity ORM / DB layer.
 *
 * Returns `null` if tree-sitter could not be loaded.
 */
export function extractMakeI18nChainsAst(text: string): MakeI18nChain[] | null {
    const lazy = getParser();
    if (!lazy) return null;
    const hasTag = /<\?(php|=|\s)/.test(text.slice(0, 32));
    const source = hasTag ? text : `<?php\n${text}`;
    const lineOffset = hasTag ? 0 : 1;
    const tree = lazy.parser.parse(source);
    const out: MakeI18nChain[] = [];
    visit(tree.rootNode, (node) => {
        if (node.type !== "member_call_expression") return;
        const methodNode = node.childForFieldName("name");
        if (!methodNode || methodNode.text !== "label") return;

        const i18nKey = firstI18nKeyArgument(node);
        if (i18nKey === null) return;

        const root = followChainToRoot(node);
        if (!root) return;
        if (root.type !== "scoped_call_expression") return;
        const rootMethod = root.childForFieldName("name");
        if (!rootMethod || rootMethod.text !== "make") return;
        const fieldLit = firstStringArgument(root);
        if (fieldLit === null) return;

        out.push({
            field: fieldLit,
            i18nKey,
            line: Math.max(1, root.startPosition.row + 1 - lineOffset),
        });
    });
    return out;
}

type SyntaxNode = ParserType.SyntaxNode;

function visit(node: SyntaxNode, fn: (n: SyntaxNode) => void): void {
    fn(node);
    for (let i = 0; i < node.namedChildCount; i++) {
        const c = node.namedChild(i);
        if (c) visit(c, fn);
    }
}

/**
 * Walk down the `object` field of a member-call chain until we hit a node
 * that is NOT a member_call_expression. That node is the "root" of the
 * fluent chain — typically a `scoped_call_expression` (`Type::make(...)`)
 * for Filament builders.
 */
function followChainToRoot(node: SyntaxNode): SyntaxNode | null {
    let cur: SyntaxNode | null = node;
    while (cur && cur.type === "member_call_expression") {
        cur = cur.childForFieldName("object");
    }
    return cur;
}

/**
 * Return the parsed value of the first argument iff it is a static string
 * literal. PHP allows several string forms — single-quoted, double-quoted,
 * heredoc, nowdoc — we accept the first two (the only ones that matter
 * for Filament idiom) and reject the rest.
 */
function firstStringArgument(callExpr: SyntaxNode): string | null {
    const args = callExpr.childForFieldName("arguments");
    if (!args) return null;
    let firstArg: SyntaxNode | null = null;
    for (let i = 0; i < args.namedChildCount; i++) {
        const c = args.namedChild(i);
        if (!c) continue;
        if (c.type === "argument") {
            firstArg = c.namedChildCount > 0 ? c.namedChild(0) : c;
            break;
        }
        // Some grammars expose the argument value directly without an
        // `argument` wrapper; tolerate both.
        firstArg = c;
        break;
    }
    if (!firstArg) return null;
    return parsePhpStringLiteral(firstArg);
}

/**
 * If the first argument of `callExpr` is a call to the global `__()`
 * helper with a single static string literal, return that literal.
 * Otherwise return `null`.
 *
 * Examples that match (return `"users.full_name"`):
 *   ->label(__('users.full_name'))
 *   ->label(__("users.full_name"))
 *
 * Examples that don't match:
 *   ->label(__('x', ['count' => 1]))   — second arg means dynamic
 *   ->label(__($var))                  — non-static key
 *   ->label(trans('x'))                — different helper (could be
 *                                        added later; rare in practice)
 */
function firstI18nKeyArgument(callExpr: SyntaxNode): string | null {
    const args = callExpr.childForFieldName("arguments");
    if (!args) return null;
    let firstArg: SyntaxNode | null = null;
    for (let i = 0; i < args.namedChildCount; i++) {
        const c = args.namedChild(i);
        if (!c) continue;
        if (c.type === "argument") {
            firstArg = c.namedChildCount > 0 ? c.namedChild(0) : c;
            break;
        }
        firstArg = c;
        break;
    }
    if (!firstArg) return null;
    // First arg must be a function call expression named `__`.
    if (firstArg.type !== "function_call_expression") return null;
    const fnName = firstArg.childForFieldName("function");
    if (!fnName || fnName.text !== "__") return null;
    const fnArgs = firstArg.childForFieldName("arguments");
    if (!fnArgs) return null;
    // Reject calls with !==1 args — multi-arg `__()` carries replacements
    // (`['count' => 1]`) which mean the string is dynamic.
    let argCount = 0;
    let inner: SyntaxNode | null = null;
    for (let i = 0; i < fnArgs.namedChildCount; i++) {
        const c = fnArgs.namedChild(i);
        if (!c) continue;
        if (c.type === "argument") {
            argCount++;
            if (argCount === 1) {
                inner = c.namedChildCount > 0 ? c.namedChild(0) : c;
            }
        }
    }
    if (argCount !== 1 || inner === null) return null;
    return parsePhpStringLiteral(inner);
}

function parsePhpStringLiteral(node: SyntaxNode): string | null {
    if (node.type === "string" || node.type === "encapsed_string") {
        // Reject interpolated double-quoted strings — they are not
        // statically known. tree-sitter-php exposes interpolation via
        // child nodes of type `interpolation`.
        for (let i = 0; i < node.namedChildCount; i++) {
            const c = node.namedChild(i);
            if (c && c.type !== "string_value" && c.type !== "escape_sequence") {
                if (c.type === "interpolation") return null;
            }
        }
        return decodePhpStringText(node.text);
    }
    return null;
}

function decodePhpStringText(raw: string): string {
    if (raw.length < 2) return "";
    const q = raw[0];
    if (q !== "'" && q !== '"') return "";
    if (raw[raw.length - 1] !== q) return "";
    const body = raw.slice(1, -1);
    return body.replace(/\\(.)/g, (_full, ch: string) => {
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
}
