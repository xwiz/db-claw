/**
 * Vue 3 SFC walker — `<label>` + `v-model` field-association extractor.
 *
 * Vue Single File Components colocate the form template, the script,
 * and the styles. The highest-fidelity vocabulary signal is the pair:
 *
 *     <label for="email">Email Address</label>
 *     <input id="email" v-model="user.email" />
 *
 * From this we derive:
 *
 *  - **canonical entity / field**: from the dotted `v-model` value
 *    (`user.email` → entity `user`, field `email`).
 *  - **display label**: from the `<label>` element's inner text.
 *  - **layer**: 6 (FormOrTableLabel) — the highest in the cascade.
 *
 * v1.0 walker uses `@vue/compiler-sfc` AST instead of regex. Handles:
 *
 *  - Attributes split across newlines, around comments, in any order.
 *  - Implicit-label idiom — wrapping `<input>` inside `<label>` without
 *    `for=`/`id=` — via DOM proximity in the parsed AST.
 *  - Conditional templates (`v-if` / `v-for`), `<template>` wrappers,
 *    nested components — the walker descends through every kind of
 *    template child without needing per-construct regex.
 *  - SFC syntax errors are surfaced via `skipped` rather than thrown,
 *    so a single broken file doesn't abort an extract run.
 *
 * Scope: walks every `.vue` file under the conventional Vue 3 source
 * directories (`src`, `pages`, `components`, `views`, `app`).
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
import { parse as parseSfc } from "@vue/compiler-sfc";
import type {
    AttributeNode,
    DirectiveNode,
    ElementNode,
    RootNode,
    TemplateChildNode,
} from "@vue/compiler-core";

/** Result of walking one project. */
export interface VueScanResult {
    fragments: VocabFragment[];
    /** Files we recognised as Vue SFCs but couldn't fully parse. */
    skipped: Array<{ file: string; reason: string }>;
}

/** Optional cross-walker context passed in by the orchestrator. */
export interface VueScanOptions {
    /**
     * Map of canonical entity name → set of canonical field names,
     * produced by the Pinia walker. When a `v-model="email"` ref has
     * no entity prefix, the SFC walker consults this index: if
     * exactly one entity declares the field, the ref is promoted to
     * `entity.field`. Ambiguous fields (declared in multiple stores)
     * are recorded in `skipped` with the candidate list.
     */
    entityIndex?: Map<string, Set<string>>;

    /**
     * Locale index produced by [`scanVueLocales`]. When supplied, the
     * SFC walker resolves `:label="$t('key')"` chains
     * (see [`extractI18nVuetifyPairs`]) into concrete vocabulary
     * fragments at FormOrTableLabel layer. Without it, the i18n pass
     * is a no-op — the literal-label walker still runs.
     */
    langIndex?: LangIndex;
}

const SFC_DIR_CANDIDATES = ["src", "pages", "components", "views", "app"];

// Vue's NodeTypes enum imported by name would force a runtime import of
// the (large) compiler-core module solely for these constants. The
// numeric values are stable across Vue 3.x — colocate as named consts.
const NODE_ELEMENT = 1;
const NODE_TEXT = 2;
const NODE_INTERPOLATION = 5;
const NODE_ATTRIBUTE = 6;
const NODE_DIRECTIVE = 7;

/** Recursively walk conventional Vue source dirs and emit fragments. */
export async function scanVueSfcs(
    root: string,
    options: VueScanOptions = {},
): Promise<VueScanResult> {
    const result: VueScanResult = { fragments: [], skipped: [] };
    for (const sub of SFC_DIR_CANDIDATES) {
        await walk(path.join(root, sub), result, options);
    }
    return result;
}

async function walk(
    dir: string,
    result: VueScanResult,
    options: VueScanOptions,
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
            // Skip dependency / build dirs we know don't contain SFCs.
            if (entry === "node_modules" || entry === "dist" || entry === ".vite") {
                continue;
            }
            await walk(full, result, options);
        } else if (entry.endsWith(".vue")) {
            await scanFile(full, result, options);
        }
    }
}

async function scanFile(
    file: string,
    result: VueScanResult,
    options: VueScanOptions,
): Promise<void> {
    const text = await fs.readFile(file, "utf8");
    const ast = parseTemplate(file, text, result);
    if (ast === null) {
        return;
    }
    const idIndex = collectIdIndex(ast);
    for (const pair of findExplicitPairs(ast, idIndex)) {
        emitPair(file, pair, result, options, "extractor-vue:label-vmodel");
    }
    for (const pair of findImplicitPairs(ast)) {
        emitPair(file, pair, result, options, "extractor-vue:label-vmodel");
    }
    for (const pair of findVuetifyPairs(ast)) {
        emitPair(file, pair, result, options, "extractor-vue:vuetify");
    }
    if (options.langIndex !== undefined) {
        for (const i18n of findI18nVuetifyPairs(ast)) {
            emitI18nPair(file, i18n, result, options, options.langIndex);
        }
    }
}

// ---------------------------------------------------------------------------
// SFC parse + AST walking
// ---------------------------------------------------------------------------

/**
 * Parse `text` as a Vue SFC and return the template AST root, or null
 * when the SFC has no `<template>` block. SFC parse errors are
 * captured into `result.skipped` so one broken file doesn't abort the
 * run.
 */
function parseTemplate(file: string, text: string, result: VueScanResult): RootNode | null {
    let parsed;
    try {
        parsed = parseSfc(text, { filename: file });
    } catch (e) {
        result.skipped.push({
            file,
            reason: `SFC parse threw: ${(e as Error).message}`,
        });
        return null;
    }
    if (parsed.errors.length > 0) {
        // Don't bail outright — Vue's parser returns a usable AST even
        // when it surfaces non-fatal warnings (mismatched closers,
        // duplicate attributes). Record the first error so users see
        // it without spamming the log per-file.
        const first = parsed.errors[0]!;
        result.skipped.push({
            file,
            reason: `SFC parse warning: ${first.message ?? String(first)}`,
        });
    }
    const tmpl = parsed.descriptor.template;
    if (!tmpl || !tmpl.ast) return null;
    return tmpl.ast;
}

interface PairInfo {
    /** `<label>` inner text after collapse. */
    labelText: string;
    /** Raw `v-model` expression, e.g. `user.email`. */
    vModel: string;
    /** Absolute line in the source file (1-indexed). */
    line: number;
}

function collectIdIndex(ast: RootNode): Map<string, ElementNode> {
    const idx = new Map<string, ElementNode>();
    for (const el of walkElements(ast)) {
        const id = staticAttrValue(el, "id");
        // Empty / missing ids would let `<label for="">` accidentally
        // pair with an unrelated `<input id="">` — index only
        // non-empty ids. First-wins so duplicate-id diagnostics from
        // Vue's parser surface as a single ambiguity rather than a
        // silent last-one-wins.
        if (id !== undefined && id !== "" && !idx.has(id)) {
            idx.set(id, el);
        }
    }
    return idx;
}

function findExplicitPairs(
    ast: RootNode,
    idIndex: Map<string, ElementNode>,
): PairInfo[] {
    const out: PairInfo[] = [];
    for (const label of walkElements(ast)) {
        if (label.tag !== "label") continue;
        const forVal = staticAttrValue(label, "for");
        if (forVal === undefined || forVal === "") continue;
        const target = idIndex.get(forVal);
        if (!target) continue;
        const vmodel = vModelExpression(target);
        if (vmodel === undefined) continue;
        out.push({
            labelText: collapseText(textOf(label)),
            vModel: vmodel,
            line: label.loc?.start.line ?? 1,
        });
    }
    return out;
}

function findImplicitPairs(ast: RootNode): PairInfo[] {
    const out: PairInfo[] = [];
    for (const label of walkElements(ast)) {
        if (label.tag !== "label") continue;
        // `for=` labels are handled by the explicit walker. Skipping
        // here also avoids double-emitting when a label has BOTH a
        // `for=` and a nested input bound to the same canonical field.
        // Empty `for=""` does NOT count — the explicit walker bails on
        // empty values, so the implicit walker still has to consider
        // those labels.
        const forVal = staticAttrValue(label, "for");
        if (forVal !== undefined && forVal !== "") continue;
        const inputs: ElementNode[] = [];
        for (const desc of walkElements(label, /* skipRoot */ true)) {
            if (vModelExpression(desc) !== undefined) {
                inputs.push(desc);
            }
        }
        if (inputs.length === 0) continue;
        const labelText = collapseText(textOfExcluding(label, inputs));
        if (!labelText) continue;
        for (const inp of inputs) {
            out.push({
                labelText,
                vModel: vModelExpression(inp)!,
                line: label.loc?.start.line ?? 1,
            });
        }
    }
    return out;
}

/**
 * Recognise Vuetify-style components — any element whose tag matches
 * the kebab-case `v-*` prefix (`<v-text-field>`, `<v-select>`,
 * `<v-checkbox>`, `<v-switch>`, etc.) with both:
 *
 *   - a static `label="…"` attribute (the visible vocabulary term), and
 *   - a `v-model` directive (the canonical entity.field binding).
 *
 * Bound `:label="…"` (dynamic) is intentionally skipped — the value
 * is computed at runtime and isn't a stable vocabulary term. i18n
 * lookups (`:label="$t('users.email')"`) get the same treatment;
 * the i18n extractor handles those keys directly.
 *
 * Vuetify's `<v-data-table :headers="…">` is *not* matched here —
 * headers are JS objects in `<script>`, requiring a script-AST
 * walker that's out of scope for the v1.0 SFC walker.
 */
const VUETIFY_TAG_RX = /^v-[a-z][a-z0-9-]*$/;

function findVuetifyPairs(ast: RootNode): PairInfo[] {
    const out: PairInfo[] = [];
    for (const el of walkElements(ast)) {
        if (!VUETIFY_TAG_RX.test(el.tag)) continue;
        const label = staticAttrValue(el, "label");
        if (label === undefined || label === "") continue;
        const vmodel = vModelExpression(el);
        if (vmodel === undefined) continue;
        out.push({
            labelText: collapseText(label),
            vModel: vmodel,
            line: el.loc?.start.line ?? 1,
        });
    }
    return out;
}

/** A vuetify-style i18n-bound pair: `:label="$t('key')"` + `v-model`. */
export interface I18nPair {
    /** Lang key argument to `$t()`, e.g. `"users.email_label"`. */
    i18nKey: string;
    /** Raw `v-model` expression, e.g. `user.email`. */
    vModel: string;
    /** 1-indexed line of the element. */
    line: number;
}

/** A `defineModel(...)` declaration found in a Vue 3.4+ `<script setup>`. */
export interface DefineModelDecl {
    /**
     * The literal first argument to `defineModel(name, ...)`. When the
     * call is `defineModel()` with no name, this is the empty string —
     * Vue defaults to `"modelValue"` at runtime, but we surface the
     * literal-as-written so callers can distinguish "default" from a
     * declared name.
     */
    name: string;
    /** 1-indexed line of the `defineModel` token. */
    line: number;
}

/**
 * Walk a `<script setup>` body for `defineModel(...)` declarations.
 * Vue 3.4+ idiom — declares a parent-bound v-model on a child
 * component:
 *
 *     const email = defineModel<string>('email')
 *     const balance = defineModel('balance', { default: 0 })
 *     const name = defineModel<string>()                  // → modelValue
 *
 * Each declaration tells us which named field this component exposes
 * to its parent's v-model bindings. The orchestrator cross-references
 * these against parent SFCs in v1.0 for end-to-end field resolution.
 *
 * Pure regex walker — `<script setup>` is TypeScript and a full AST
 * needs `@babel/parser` or similar. The regex covers the >95% shape
 * of real Vue codebases: `defineModel<TypeArgs>?(...)?`, optionally
 * preceded by a `const`/`let`/`var` binding.
 *
 * Returns the empty array on input with no `defineModel` reference.
 */
export function extractDefineModels(scriptContent: string): DefineModelDecl[] {
    const out: DefineModelDecl[] = [];
    // Matches:
    //   defineModel(...)              → no name
    //   defineModel('name', ...)      → quoted name
    //   defineModel<T>('name', ...)   → with type args
    //   defineModel<T>()              → no name, type args present
    //
    // Reject:
    //   defineModel(varName)          → dynamic name
    //   defineModel('a' + 'b')        → dynamic
    //   defineModel(() => …)          → callable arg
    //
    // Captures: optional name string. The line number derives from the
    // position of the `defineModel` keyword in the source.
    // Type-args allow one level of nested generics — `Record<string,
    // number>` is common in real Vue codebases. Two levels would need
    // a balanced-bracket parser; one covers the >99% case.
    const RX =
        /\bdefineModel\b\s*(?:<[^<>]*(?:<[^<>]*>[^<>]*)*>)?\s*\(\s*(?:(['"])([^'"\\]*(?:\\.[^'"\\]*)*)\1)?\s*(?:,[^)]*)?\)/g;
    for (const match of scriptContent.matchAll(RX)) {
        const idx = match.index ?? 0;
        // Reject when the captured-name slot was the empty position
        // BUT a non-string token is sitting in front of the close-paren
        // — these are dynamic-arg shapes the regex can't disambiguate.
        // Cheap heuristic: if there's no quote AND the inside isn't
        // empty/whitespace, drop. The match's group[2] is the name when
        // present.
        const name = match[2];
        if (name === undefined) {
            // Inspect the raw match to confirm the call really is
            // `defineModel()` with no args (not `defineModel(varName)`).
            const inside = match[0]
                .replace(/^\bdefineModel\b\s*(?:<[^>]*>)?\s*\(/, "")
                .replace(/\)\s*$/, "");
            if (inside.trim().length > 0) continue;
        }
        out.push({
            name: name ?? "",
            line: lineOfOffset(scriptContent, idx),
        });
    }
    return out;
}

function lineOfOffset(text: string, offset: number): number {
    let line = 1;
    for (let i = 0; i < offset && i < text.length; i++) {
        if (text[i] === "\n") line++;
    }
    return line;
}

/** Result of one walk over a project's SFCs for `defineModel(...)` shapes. */
export interface ComponentModelScanResult {
    /**
     * `Map<componentFile, declarations>` keyed by absolute SFC file path.
     * The orchestrator uses this to cross-reference parent-side
     * `<UserForm v-model:email="user.email" />` invocations against the
     * child's `defineModel('email')` declarations — when both signals
     * agree, the parent's bare-ref `user.email` is promoted to the
     * canonical `(user, email)` field.
     */
    components: Map<string, DefineModelDecl[]>;
    /** SFCs we recognised but couldn't parse a script-setup body for. */
    skipped: Array<{ file: string; reason: string }>;
}

/**
 * Walk every Vue SFC under `root` and collect its `defineModel(...)`
 * declarations. The walker visits the same conventional source roots
 * as [`scanVueSfcs`] (`src`, `pages`, `components`, `views`, `app`).
 *
 * Reads the SFC's `<script setup>` block via `@vue/compiler-sfc` and
 * passes the body to [`extractDefineModels`]. Files without a script
 * setup block (or with one that throws on parse) are silently skipped
 * — the walker is best-effort, not the source of truth for vocabulary
 * resolution.
 */
export async function scanComponentModels(
    root: string,
): Promise<ComponentModelScanResult> {
    const result: ComponentModelScanResult = {
        components: new Map(),
        skipped: [],
    };
    for (const sub of SFC_DIR_CANDIDATES) {
        await componentWalk(path.join(root, sub), result);
    }
    return result;
}

async function componentWalk(
    dir: string,
    result: ComponentModelScanResult,
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
            if (entry === "node_modules" || entry === "dist" || entry === ".vite") {
                continue;
            }
            await componentWalk(full, result);
        } else if (entry.endsWith(".vue")) {
            await scanComponentFile(full, result);
        }
    }
}

async function scanComponentFile(
    file: string,
    result: ComponentModelScanResult,
): Promise<void> {
    const text = await fs.readFile(file, "utf8");
    let parsed;
    try {
        parsed = parseSfc(text, { filename: file });
    } catch (e) {
        result.skipped.push({
            file,
            reason: `SFC parse threw: ${(e as Error).message}`,
        });
        return;
    }
    const scriptSetup = parsed.descriptor.scriptSetup;
    if (!scriptSetup || typeof scriptSetup.content !== "string") {
        // A `<script>` (non-setup) block could also use the
        // composition API but `defineModel` is a setup-only macro. No
        // need to inspect non-setup scripts.
        return;
    }
    const decls = extractDefineModels(scriptSetup.content);
    if (decls.length > 0) {
        result.components.set(file, decls);
    }
}

/**
 * Walk the SFC AST for vuetify-style elements bound to an i18n key:
 *
 *     <v-text-field :label="$t('users.email_label')" v-model="user.email" />
 *
 * Common Vue 3 idiom in any localised app. The literal-string variant
 * ([`findVuetifyPairs`]) misses these because the value is computed.
 *
 * The merge engine resolves the i18n key against the lang/ index at
 * SDK time. Until that wiring lands, `extractI18nVuetifyPairs` is the
 * exposed scanner so downstream tooling can consume the pairs directly
 * (e.g. `semsql doctor` could surface i18n-bound but unresolved labels).
 */
export function extractI18nVuetifyPairs(ast: RootNode): I18nPair[] {
    return findI18nVuetifyPairs(ast);
}

function findI18nVuetifyPairs(ast: RootNode): I18nPair[] {
    const out: I18nPair[] = [];
    for (const el of walkElements(ast)) {
        if (!VUETIFY_TAG_RX.test(el.tag)) continue;
        const i18nKey = boundI18nLabelKey(el);
        if (i18nKey === undefined) continue;
        const vmodel = vModelExpression(el);
        if (vmodel === undefined) continue;
        out.push({
            i18nKey,
            vModel: vmodel,
            line: el.loc?.start.line ?? 1,
        });
    }
    return out;
}

/**
 * Detect an `:label="$t('key')"` (or `t('key')`) bound directive on
 * `el` and return the static `key`. Returns `undefined` for any other
 * shape — dynamic concatenation, missing argument, multi-arg `$t()`
 * (which carries replacements and so isn't statically safe), or a
 * non-string-literal first argument.
 */
function boundI18nLabelKey(el: ElementNode): string | undefined {
    for (const p of el.props) {
        if (p.type !== NODE_DIRECTIVE) continue;
        const dir = p as DirectiveNode;
        if (dir.name !== "bind") continue;
        const arg = dir.arg as { content?: string } | undefined;
        if (!arg || arg.content !== "label") continue;
        const exp = dir.exp;
        if (!exp) continue;
        const content = (exp as { content?: string }).content;
        if (typeof content !== "string") continue;
        const trimmed = content.trim();
        // Permissive but bounded matcher: `<helper>('key')` or
        // `<helper>("key")` where helper is `$t` or `t` and there is
        // exactly one string-literal argument. Whitespace tolerated.
        const m = trimmed.match(
            /^\$?t\(\s*(['"])([^'"\\]*(?:\\.[^'"\\]*)*)\1\s*\)$/,
        );
        if (!m) continue;
        const raw = m[2] ?? "";
        // Decode the same JS string escapes the Vue parser would.
        return raw.replace(/\\(.)/g, (_, ch: string) => {
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
    return undefined;
}

/**
 * Yield every `ElementNode` in `node`'s subtree in document order. When
 * `skipRoot` is true, the root itself is not yielded — useful for the
 * implicit-label walker, which wants only descendants.
 */
function* walkElements(
    node: RootNode | TemplateChildNode,
    skipRoot = false,
): Generator<ElementNode> {
    if (!skipRoot && isElement(node)) {
        yield node;
    }
    const children = childrenOf(node);
    for (const child of children) {
        yield* walkElements(child, false);
    }
}

function childrenOf(node: RootNode | TemplateChildNode): TemplateChildNode[] {
    // RootNode + ElementNode share a `children` field. v-if / v-for
    // wrappers (IfNode / ForNode) carry branches/children too — fold
    // them in so conditionally-rendered inputs are still walked.
    const anyNode = node as { children?: TemplateChildNode[]; branches?: { children: TemplateChildNode[] }[] };
    if (anyNode.branches) {
        const out: TemplateChildNode[] = [];
        for (const b of anyNode.branches) out.push(...(b.children ?? []));
        return out;
    }
    return anyNode.children ?? [];
}

function isElement(node: RootNode | TemplateChildNode): node is ElementNode {
    return (node as { type?: number }).type === NODE_ELEMENT;
}

/** Return the string value of `el`'s static attribute `name`, or undefined. */
function staticAttrValue(el: ElementNode, name: string): string | undefined {
    for (const p of el.props) {
        if (p.type === NODE_ATTRIBUTE && (p as AttributeNode).name === name) {
            return (p as AttributeNode).value?.content ?? "";
        }
    }
    return undefined;
}

/** Return the v-model expression on `el`, or undefined. */
function vModelExpression(el: ElementNode): string | undefined {
    for (const p of el.props) {
        if (p.type !== NODE_DIRECTIVE) continue;
        const dir = p as DirectiveNode;
        if (dir.name !== "model") continue;
        // Pure expression — no template-literal binding.
        const exp = dir.exp;
        if (!exp) continue;
        const content = (exp as { content?: string }).content;
        if (typeof content === "string" && content.length > 0) return content.trim();
    }
    return undefined;
}

/**
 * Concatenate every TEXT descendant into a single string. INTERPOLATION
 * (`{{ … }}`) nodes are deliberately excluded — they're computed at
 * runtime and would produce a label like `{{ tr.email }}` which is not
 * a vocabulary term.
 */
function textOf(node: RootNode | TemplateChildNode): string {
    if ((node as { type?: number }).type === NODE_TEXT) {
        return (node as { content: string }).content;
    }
    if ((node as { type?: number }).type === NODE_INTERPOLATION) {
        return ""; // skip dynamic bindings
    }
    let out = "";
    for (const c of childrenOf(node)) out += textOf(c);
    return out;
}

/**
 * Same as `textOf` but skips a list of subtree roots. Used by the
 * implicit-label walker so the inputs nested *inside* the label don't
 * contribute their attribute values to the label text.
 */
function textOfExcluding(
    node: RootNode | TemplateChildNode,
    excluded: readonly ElementNode[],
): string {
    if ((node as { type?: number }).type === NODE_TEXT) {
        return (node as { content: string }).content;
    }
    if ((node as { type?: number }).type === NODE_INTERPOLATION) {
        return "";
    }
    let out = "";
    for (const c of childrenOf(node)) {
        if (excluded.includes(c as ElementNode)) continue;
        out += textOfExcluding(c, excluded);
    }
    return out;
}

function collapseText(s: string): string {
    return s.replace(/\s+/g, " ").trim();
}

// ---------------------------------------------------------------------------
// Emission
// ---------------------------------------------------------------------------

function emitPair(
    file: string,
    pair: PairInfo,
    result: VueScanResult,
    options: VueScanOptions,
    extractor: string,
): void {
    const dotted = pair.vModel.split(".").map((s) => s.trim());

    let entity: string;
    let field: string;

    if (dotted.length < 2) {
        // Bare ref like `v-model="email"`. If the orchestrator passed
        // an entity index from the Pinia walker, try to promote: a
        // field declared in exactly one store wins; conflicts are
        // logged and skipped so the cascade never silently picks the
        // wrong entity.
        const promoted = promoteBareRef(pair.vModel, options.entityIndex);
        if (promoted === null) {
            result.skipped.push({
                file,
                reason: `v-model='${pair.vModel}' has no entity prefix; needs an ORM walker for context`,
            });
            return;
        }
        if (Array.isArray(promoted)) {
            result.skipped.push({
                file,
                reason: `v-model='${pair.vModel}' is ambiguous — declared by stores: ${promoted.join(", ")}`,
            });
            return;
        }
        entity = promoted.entity;
        field = promoted.field;
    } else {
        const [first, ...rest] = dotted;
        if (!first) return;
        entity = first;
        field = rest.join(".");
    }

    let canonicalEntity: string;
    let canonicalField: string;
    let label: string;
    try {
        canonicalEntity = sanitiseCanonical(entity);
        // Drop v-model index suffixes like [0] defensively before
        // sanitising. The form 'users[0].email' is rare but real;
        // the walker should not emit garbage if it appears.
        canonicalField = sanitiseCanonical(
            (field || entity).replace(/\[[^\]]*\]/g, ""),
        );
        label = sanitiseLabel(pair.labelText);
    } catch (e) {
        if (e instanceof SanitiserError) {
            result.skipped.push({ file, reason: e.message });
            return;
        }
        throw e;
    }

    result.fragments.push({
        term: label.toLowerCase(),
        canonical: { kind: "field", field: `${canonicalEntity}.${canonicalField}` },
        confidence: 0.95,
        locator: {
            file,
            line: pair.line,
            layer: SourceLayer.FormOrTableLabel,
            extractor,
        },
    });
}

/**
 * Resolve one i18n-bound vuetify pair against the lang index and emit
 * a vocabulary fragment. Mirrors [`emitPair`] for the literal-label
 * path; key differences:
 *
 *  - The label text comes from the lang index, not the template.
 *  - Confidence is 0.92 (between layer-5 raw 0.85 and layer-6 literal
 *    0.95) to reflect the i18n indirection — if the lang file changes,
 *    this binding is stale.
 *  - Unresolved keys land in `skipped` with a precise reason so
 *    `semsql doctor` can surface them.
 */
function emitI18nPair(
    file: string,
    pair: I18nPair,
    result: VueScanResult,
    options: VueScanOptions,
    langIndex: LangIndex,
): void {
    const entry = langIndex.get(pair.i18nKey);
    if (entry === undefined) {
        result.skipped.push({
            file,
            reason: `i18n key not in lang index: ${pair.i18nKey} (line ${pair.line})`,
        });
        return;
    }
    const dotted = pair.vModel.split(".").map((s) => s.trim());
    let entity: string;
    let field: string;
    if (dotted.length < 2) {
        const promoted = promoteBareRef(pair.vModel, options.entityIndex);
        if (promoted === null || Array.isArray(promoted)) return;
        entity = promoted.entity;
        field = promoted.field;
    } else {
        const [first, ...rest] = dotted;
        if (!first) return;
        entity = first;
        field = rest.join(".");
    }
    let canonicalEntity: string;
    let canonicalField: string;
    let label: string;
    try {
        canonicalEntity = sanitiseCanonical(entity);
        canonicalField = sanitiseCanonical(
            (field || entity).replace(/\[[^\]]*\]/g, ""),
        );
        label = sanitiseLabel(entry.label);
    } catch (e) {
        if (e instanceof SanitiserError) {
            result.skipped.push({ file, reason: e.message });
            return;
        }
        throw e;
    }
    result.fragments.push({
        term: label.toLowerCase(),
        canonical: {
            kind: "field",
            field: `${canonicalEntity}.${canonicalField}`,
        },
        confidence: 0.92,
        locator: {
            file,
            line: pair.line,
            layer: SourceLayer.FormOrTableLabel,
            extractor: `extractor-vue:vuetify-i18n:${entry.locale}`,
        },
    });
}

/**
 * Promote a bare v-model expression (`email`) to a qualified
 * `entity.field` pair using the orchestrator-supplied entity index.
 *
 * Returns:
 *   - `{ entity, field }` when exactly one entity in the index has
 *     this field — unambiguous promotion.
 *   - `string[]` of candidate entities when more than one declares it
 *     — caller logs the ambiguity and skips emission.
 *   - `null` when no index was supplied OR no entity declares it.
 *
 * The bare ref might itself contain dots that don't represent an
 * entity boundary (e.g. `form.email` where `form` is a local
 * reactive ref). We only consider the *last* segment for index
 * lookup — the leading segments are treated as a sub-path that the
 * v1.0 AST walker will resolve once it can introspect the script
 * setup block.
 */
export function promoteBareRef(
    bareRef: string,
    entityIndex: Map<string, Set<string>> | undefined,
): { entity: string; field: string } | string[] | null {
    if (!entityIndex || entityIndex.size === 0) return null;
    // For now, treat the entire bareRef as the field name. Sub-path
    // refs (`form.email.value`) get the Pinia v0.5 treatment: skip
    // and surface, to be revisited once SFC AST lands.
    const fieldName = bareRef.includes(".")
        ? bareRef.split(".").slice(-1)[0]!
        : bareRef;
    const candidates: string[] = [];
    for (const [entity, fields] of entityIndex) {
        if (fields.has(fieldName)) candidates.push(entity);
    }
    if (candidates.length === 1) {
        return { entity: candidates[0]!, field: fieldName };
    }
    if (candidates.length > 1) return candidates;
    return null;
}
