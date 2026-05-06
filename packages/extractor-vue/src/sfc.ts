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
        emitPair(file, pair, result, options);
    }
    for (const pair of findImplicitPairs(ast)) {
        emitPair(file, pair, result, options);
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
            extractor: "extractor-vue:label-vmodel",
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
