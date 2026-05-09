/**
 * Rails I18n locale walker — `config/locales/**\/*.yml`.
 *
 * Rails apps drive form labels and entity names through the I18n
 * locale files almost universally. The conventional shape:
 *
 *     en:
 *       activerecord:
 *         models:
 *           user: "User"
 *           user_account:
 *             one: "User account"
 *             other: "User accounts"
 *         attributes:
 *           user:
 *             email: "Email Address"
 *             is_active: "Active"
 *             tenant_id: "Tenant"
 *       helpers:
 *         label:
 *           user:
 *             email: "Your email"
 *
 * What we extract:
 *
 *  - **Entity labels** from `activerecord.models.<model>` → entity
 *    fragment at layer 5 (UserSurface). When the value is itself a
 *    pluralisation map (`one`/`other`/`zero`/`few`/`many`/`two`),
 *    the singular ("one") form is used as the canonical label;
 *    plurals are emitted as additional terms aliased to the same
 *    canonical entity.
 *  - **Field labels** from `activerecord.attributes.<model>.<attr>`
 *    → field fragment at layer 5. The key path identifies the
 *    canonical entity and field name; the value is the human label.
 *  - **Helper-label overrides** from `helpers.label.<form>.<attr>`
 *    → field fragment at layer 6 (FormOrTableLabel) since these
 *    only fire on actual form rendering and reflect runtime UI
 *    truth more closely than the model-level AR labels.
 *
 * Skipped at v0.5:
 *
 *  - `errors.*` keys — runtime validation messages, not vocabulary.
 *  - `simple_form.*` and other gem-specific namespaces — handled
 *    by their respective adapter walkers in v1.0.
 *  - Interpolation placeholders like `%{count}` are stripped from
 *    labels via `sanitiseLabel`.
 *
 * The walker uses `js-yaml` because hand-rolling YAML over Rails
 * locale files trips on multi-line scalars, anchors, and the
 * occasional Ruby symbol key — `js-yaml` handles the canonical
 * Rails subset cleanly.
 */

import { promises as fs } from "node:fs";
import path from "node:path";
import yaml from "js-yaml";

import {
    sanitiseCanonical,
    sanitiseLabel,
    SanitiserError,
    SourceLayer,
    type LangIndex,
    type LangIndexEntry,
    type VocabFragment,
} from "@semsql/extractor-sdk";

export type { LangIndex, LangIndexEntry };

/** Result of walking the locales directory. */
export interface LocalesScanResult {
    fragments: VocabFragment[];
    /** Files we recognised as locale YAML but couldn't parse. */
    skipped: Array<{ file: string; reason: string }>;
    /**
     * Raw Rails I18n key → label index. Maps the dotted Rails I18n key
     * (locale stripped — `activerecord.models.user`) to its
     * preferred-locale label. View-side walkers (`t('users.email')`
     * helpers in ERB / Slim / Haml templates) consult this index when
     * resolving i18n-bound vocabulary.
     */
    index: LangIndex;
}

/** Locales we treat as authoritative for canonical names. English by
 *  default; the orchestrator can pass `preferredLocale` to override. */
export interface LocalesScanOptions {
    /** Locale code that wins over others when the same key has
     *  multiple translations. Default `"en"`. */
    preferredLocale?: string;
}

const PLURAL_KEYS = new Set(["zero", "one", "two", "few", "many", "other"]);
const PRIMARY_PLURAL = "one";

/**
 * Walk every YAML file under `config/locales/`. The orchestrator
 * uses fragments from this walker to disambiguate column-name
 * matches that the schema.rb walker emits with snake_case-derived
 * labels — the locale label always reflects the human surface.
 */
export async function scanLocales(
    root: string,
    options: LocalesScanOptions = {},
): Promise<LocalesScanResult> {
    const result: LocalesScanResult = {
        fragments: [],
        skipped: [],
        index: new Map<string, LangIndexEntry>(),
    };
    const preferred = options.preferredLocale ?? "en";
    const localesDir = path.join(root, "config", "locales");
    await walkYaml(localesDir, result, preferred);
    return result;
}

async function walkYaml(
    dir: string,
    result: LocalesScanResult,
    preferred: string,
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
            await walkYaml(full, result, preferred);
        } else if (/\.ya?ml$/.test(entry)) {
            await scanFile(full, result, preferred);
        }
    }
}

async function scanFile(
    file: string,
    result: LocalesScanResult,
    preferred: string,
): Promise<void> {
    const text = await fs.readFile(file, "utf8");
    let parsed: unknown;
    try {
        parsed = yaml.load(text);
    } catch (e) {
        result.skipped.push({
            file,
            reason: `yaml parse: ${e instanceof Error ? e.message : String(e)}`,
        });
        return;
    }
    if (!isPlainObject(parsed)) return;

    // Locale files are typically rooted at the locale code:
    //   en: { ... }
    //   fr: { ... }
    // We process every top-level locale key, but only emit fragments
    // when the locale matches `preferred`. Other locales contribute
    // alias terms that point at the same canonical name.
    for (const [locale, body] of Object.entries(parsed)) {
        if (!isPlainObject(body)) continue;
        const isPreferred = locale === preferred;
        emitFromLocale(file, body, isPreferred, result);
        // Index pass — record every leaf string under its
        // locale-stripped dotted Rails I18n key so view-side
        // `t('users.email')`-class lookups resolve. Locale priority:
        // preferred wins; otherwise first-write-wins.
        recordIndexLeaves(body, [], file, locale, preferred, result);
    }
}

function recordIndexLeaves(
    node: unknown,
    keyStack: string[],
    file: string,
    locale: string,
    preferred: string,
    result: LocalesScanResult,
): void {
    if (typeof node === "string") {
        if (keyStack.length === 0) return;
        const dotted = keyStack.join(".");
        const incoming: LangIndexEntry = {
            label: node,
            locale,
            file,
            line: 1, // js-yaml doesn't surface per-key positions; line = 1 is best-effort.
        };
        const existing = result.index.get(dotted);
        if (existing === undefined) {
            result.index.set(dotted, incoming);
            return;
        }
        // Preferred locale wins; otherwise keep first-seen for stability.
        if (existing.locale !== preferred && locale === preferred) {
            result.index.set(dotted, incoming);
        }
        return;
    }
    if (typeof node === "number" || typeof node === "boolean") {
        // Stringify scalars too — `t('users.count')` returning `0`
        // is a valid lookup; the i18n binding consumer treats labels
        // as strings.
        if (keyStack.length === 0) return;
        const dotted = keyStack.join(".");
        const incoming: LangIndexEntry = {
            label: String(node),
            locale,
            file,
            line: 1,
        };
        const existing = result.index.get(dotted);
        if (existing === undefined) {
            result.index.set(dotted, incoming);
        } else if (existing.locale !== preferred && locale === preferred) {
            result.index.set(dotted, incoming);
        }
        return;
    }
    if (!isPlainObject(node)) return;
    for (const [k, v] of Object.entries(node)) {
        recordIndexLeaves(v, [...keyStack, k], file, locale, preferred, result);
    }
}

function emitFromLocale(
    file: string,
    body: Record<string, unknown>,
    isPreferred: boolean,
    result: LocalesScanResult,
): void {
    // -------- activerecord.models -----------------------------
    const arModels = digPath<Record<string, unknown>>(body, [
        "activerecord",
        "models",
    ]);
    if (arModels) {
        for (const [modelKey, modelLabel] of Object.entries(arModels)) {
            emitEntityLabel(file, modelKey, modelLabel, isPreferred, result);
        }
    }

    // -------- activerecord.attributes -------------------------
    const arAttrs = digPath<Record<string, unknown>>(body, [
        "activerecord",
        "attributes",
    ]);
    if (arAttrs) {
        for (const [modelKey, attrs] of Object.entries(arAttrs)) {
            if (!isPlainObject(attrs)) continue;
            for (const [attrKey, attrLabel] of Object.entries(attrs)) {
                emitFieldLabel(
                    file,
                    modelKey,
                    attrKey,
                    attrLabel,
                    isPreferred,
                    SourceLayer.I18n,
                    "extractor-rails:locales:activerecord",
                    result,
                );
            }
        }
    }

    // -------- helpers.label -----------------------------------
    const helpersLabel = digPath<Record<string, unknown>>(body, [
        "helpers",
        "label",
    ]);
    if (helpersLabel) {
        for (const [formKey, attrs] of Object.entries(helpersLabel)) {
            if (!isPlainObject(attrs)) continue;
            for (const [attrKey, attrLabel] of Object.entries(attrs)) {
                emitFieldLabel(
                    file,
                    formKey,
                    attrKey,
                    attrLabel,
                    isPreferred,
                    SourceLayer.FormOrTableLabel,
                    "extractor-rails:locales:helpers",
                    result,
                );
            }
        }
    }
}

function emitEntityLabel(
    file: string,
    modelKey: string,
    rawValue: unknown,
    isPreferred: boolean,
    result: LocalesScanResult,
): void {
    if (!isPreferred) return;
    let canonical: string;
    try {
        canonical = sanitiseCanonical(modelKey);
    } catch (e) {
        if (e instanceof SanitiserError) {
            result.skipped.push({ file, reason: e.message });
            return;
        }
        throw e;
    }
    const labels = collectLabels(rawValue);
    for (const labelText of labels) {
        let cleaned: string;
        try {
            cleaned = sanitiseLabel(labelText);
        } catch {
            continue;
        }
        result.fragments.push({
            term: cleaned.toLowerCase(),
            canonical: { kind: "entity", entity: canonical },
            confidence: 0.9,
            locator: {
                file,
                line: 0,
                layer: SourceLayer.I18n,
                extractor: "extractor-rails:locales:activerecord",
            },
        });
    }
}

function emitFieldLabel(
    file: string,
    modelKey: string,
    attrKey: string,
    rawValue: unknown,
    isPreferred: boolean,
    layer: SourceLayer,
    extractor: string,
    result: LocalesScanResult,
): void {
    if (!isPreferred) return;
    let canonicalEntity: string;
    let canonicalField: string;
    try {
        canonicalEntity = sanitiseCanonical(modelKey);
        canonicalField = sanitiseCanonical(attrKey);
    } catch (e) {
        if (e instanceof SanitiserError) {
            result.skipped.push({ file, reason: e.message });
            return;
        }
        throw e;
    }
    const labels = collectLabels(rawValue);
    for (const labelText of labels) {
        let cleaned: string;
        try {
            cleaned = sanitiseLabel(stripInterpolations(labelText));
        } catch {
            continue;
        }
        result.fragments.push({
            term: cleaned.toLowerCase(),
            canonical: { kind: "field", field: `${canonicalEntity}.${canonicalField}` },
            confidence: layer === SourceLayer.FormOrTableLabel ? 0.95 : 0.9,
            locator: { file, line: 0, layer, extractor },
        });
    }
}

/**
 * Pull display labels out of a value that may be a plain string,
 * a CLDR-style plural map, or an object holding both. Returns the
 * canonical singular ("one") first when present, then any other
 * plurals so they can be emitted as alias terms.
 */
function collectLabels(value: unknown): string[] {
    if (typeof value === "string") return [value];
    if (!isPlainObject(value)) return [];
    const labels: string[] = [];
    if (typeof value[PRIMARY_PLURAL] === "string") {
        labels.push(value[PRIMARY_PLURAL] as string);
    }
    for (const [k, v] of Object.entries(value)) {
        if (k === PRIMARY_PLURAL) continue;
        if (PLURAL_KEYS.has(k) && typeof v === "string") labels.push(v);
    }
    return labels;
}

/** Drop Rails I18n placeholders so they don't pollute the term index. */
function stripInterpolations(s: string): string {
    return s.replace(/%\{[^}]+\}/g, "").replace(/\s+/g, " ").trim();
}

function digPath<T>(root: Record<string, unknown>, segs: string[]): T | null {
    let cur: unknown = root;
    for (const s of segs) {
        if (!isPlainObject(cur)) return null;
        cur = cur[s];
    }
    return (cur ?? null) as T | null;
}

function isPlainObject(v: unknown): v is Record<string, unknown> {
    return typeof v === "object" && v !== null && !Array.isArray(v);
}
