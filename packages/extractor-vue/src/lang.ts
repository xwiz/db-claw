/**
 * Vue 3 / Nuxt locales walker.
 *
 * vue-i18n stores translations in JSON files at conventional paths:
 *
 *     src/locales/en.json
 *     src/locales/en/users.json
 *     locales/en.json
 *     i18n/en.json
 *     lang/en.json
 *
 * The walker reads every JSON file under those roots, builds a
 * dotted-key → label index, and prefers `en` on conflicts (Vue's de-facto
 * default locale). The index has the same shape as the SDK's [`LangIndex`]
 * so the SFC walker resolves `:label="$t('users.email')"` chains
 * (see [`extractI18nVuetifyPairs`]) against it cross-package without
 * any coupling.
 */

import { promises as fs } from "node:fs";
import path from "node:path";
import type { LangIndex, LangIndexEntry } from "@semsql/extractor-sdk";

export type { LangIndex, LangIndexEntry };

/** Per-locale walk root. Vue/Nuxt projects place locales under one of these. */
const LOCALES_ROOTS = [
    path.join("src", "locales"),
    "locales",
    "i18n",
    "lang",
] as const;

/** Result of one walk over a project's locales. */
export interface VueLangScanResult {
    /** Resolved key → entry index. */
    index: LangIndex;
    /** Files we recognised but couldn't fully parse. */
    skipped: Array<{ file: string; reason: string }>;
}

/**
 * Walk the conventional Vue 3 locales roots under `root` and build a
 * lang index. Tolerant of missing roots — projects that don't use
 * vue-i18n simply produce an empty index.
 */
export async function scanVueLocales(root: string): Promise<VueLangScanResult> {
    const result: VueLangScanResult = {
        index: new Map<string, LangIndexEntry>(),
        skipped: [],
    };
    for (const sub of LOCALES_ROOTS) {
        await walkLocaleRoot(path.join(root, sub), result);
    }
    return result;
}

async function walkLocaleRoot(
    dir: string,
    result: VueLangScanResult,
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
            // `locales/<locale>/...` shape — locale is the directory name.
            const locale = entry;
            await walkLocaleDir(full, full, locale, result);
        } else if (entry.endsWith(".json")) {
            // `locales/<locale>.json` shape — locale is the basename.
            const locale = path.basename(entry, ".json");
            await readJsonFile(full, locale, result);
        }
    }
}

async function walkLocaleDir(
    dir: string,
    localeRoot: string,
    locale: string,
    result: VueLangScanResult,
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
            await walkLocaleDir(full, localeRoot, locale, result); // nested groups
        } else if (entry.endsWith(".json")) {
            // `locales/<locale>/<group>.json` — vue-i18n's group convention.
            // Keys inside resolve as `<group>.<key>` per vue-i18n conventions
            // when the loader uses namespaced groups; we record both shapes
            // (group-prefixed + raw) so either lookup works.
            //
            // Group prefix is the path from localeRoot to file, joined by
            // dots, with the `.json` extension stripped — so deeply nested
            // groups (`locales/en/auth/login.json`) resolve correctly as
            // `auth.login.<key>`.
            const groupPrefix = vueGroupPrefix(localeRoot, full);
            await readJsonFile(full, locale, result, groupPrefix);
        }
    }
}

function vueGroupPrefix(localeRoot: string, file: string): string {
    const rel = path.relative(localeRoot, file);
    const noExt = rel.endsWith(".json") ? rel.slice(0, -5) : rel;
    const segs = noExt.split(/[\\/]/).filter((s) => s.length > 0);
    return segs.join(".");
}

async function readJsonFile(
    file: string,
    locale: string,
    result: VueLangScanResult,
    groupPrefix?: string,
): Promise<void> {
    let text: string;
    try {
        text = await fs.readFile(file, "utf8");
    } catch (e) {
        result.skipped.push({ file, reason: `read error: ${(e as Error).message}` });
        return;
    }
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
    walk(data, [], file, locale, result);
    if (groupPrefix !== undefined) {
        // Also index under the group-prefixed shape so callers that use
        // `$t('users.email')` against `locales/en/users.json` resolve.
        walk(data, [groupPrefix], file, locale, result);
    }
}

function walk(
    node: unknown,
    keyStack: string[],
    file: string,
    locale: string,
    result: VueLangScanResult,
): void {
    if (typeof node === "string") {
        recordIndex(keyStack, node, file, locale, result, /*line*/ 1);
        return;
    }
    if (!isPlainObject(node)) return;
    for (const [k, v] of Object.entries(node)) {
        walk(v, [...keyStack, k], file, locale, result);
    }
}

function recordIndex(
    keyStack: string[],
    value: string,
    file: string,
    locale: string,
    result: VueLangScanResult,
    line: number,
): void {
    if (keyStack.length === 0) return;
    const dotted = keyStack.join(".");
    const incoming: LangIndexEntry = { label: value, locale, file, line };
    const existing = result.index.get(dotted);
    if (existing === undefined) {
        result.index.set(dotted, incoming);
        return;
    }
    // Locale priority: `en` wins on conflict; otherwise first-write-wins.
    if (existing.locale !== "en" && locale === "en") {
        result.index.set(dotted, incoming);
    }
}

function isPlainObject(x: unknown): x is Record<string, unknown> {
    return typeof x === "object" && x !== null && !Array.isArray(x);
}
