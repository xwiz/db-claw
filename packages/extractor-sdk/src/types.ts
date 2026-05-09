/**
 * Shared types for every extractor adapter.
 *
 * The shape mirrors `schemas/semantic_graph.proto` — `SourceLayer` is the
 * same enum, `Locator` matches `SourceLocator`. Keep the two in lock-step.
 */

/** Priority cascade — higher number wins on conflict. */
export const SourceLayer = {
    DbSchema: 1,
    Orm: 2,
    AppConstant: 3,
    ApiResource: 4,
    I18n: 5,
    /** Highest priority — what users actually read on screen. */
    FormOrTableLabel: 6,
} as const;
export type SourceLayer = (typeof SourceLayer)[keyof typeof SourceLayer];

/** Where in the project a vocabulary fragment was discovered. */
export interface Locator {
    /** Path relative to the project root. */
    file: string;
    /** 1-indexed line number. */
    line: number;
    /** Optional column. */
    column?: number;
    /** Layer this entry belongs to. */
    layer: SourceLayer;
    /** Human-readable extractor name, e.g. `"extractor-laravel:filament-form"`. */
    extractor: string;
}

/** What a vocabulary term canonically refers to. Exactly one variant set. */
export type Canonical =
    | { kind: "entity"; entity: string }
    | { kind: "field"; field: string /* dotted: "users.created_at" */ }
    | { kind: "enum_value"; enumName: string; rawValue: string }
    | { kind: "relationship"; from: string; to: string };

/** Confidence in the mapping, `[0, 1]`. */
export type Confidence = number;

/** One vocabulary fragment emitted by an extractor. */
export interface VocabFragment {
    /** User-facing term, e.g. `"Students"`, `"Joined Date"`. */
    term: string;
    canonical: Canonical;
    confidence: Confidence;
    locator: Locator;
}

/** Context passed to every extractor on `extract()`. */
export interface ExtractCtx {
    /** Project root (absolute path). */
    root: string;
    /** Optional dialect / framework hint, e.g. `"filament-v3"`. */
    flavour?: string;
}

/** Protocol every framework adapter implements. */
export interface Extractor {
    /** Stable adapter name — appears in every fragment locator. */
    name: string;

    /** Returns true iff this adapter applies to the given project. */
    detect(root: string): Promise<boolean>;

    /** Yields fragments. Order is irrelevant; the merge engine sorts by layer. */
    extract(ctx: ExtractCtx): AsyncIterable<VocabFragment>;
}

/**
 * One resolved i18n entry — a (key, label) pair plus enough provenance
 * to surface in `semsql doctor` reports.
 *
 * Lang/locale walkers (Laravel `lang/`, Vue `src/locales/`, Rails
 * `config/locales/`) populate a [`LangIndex`] that the framework
 * adapters consult when they encounter `__('key')` / `$t('key')`
 * helpers. The shared type lives in the SDK so every adapter can
 * cross-pollinate without coupling on a specific framework's reader.
 */
export interface LangIndexEntry {
    /** Label as written in the source (sanitiser-pre). */
    label: string;
    /** Locale this entry won the priority cascade for. */
    locale: string;
    /** Source file. */
    file: string;
    /** 1-indexed line, best-effort. */
    line: number;
}

/** Dotted-key → resolved-entry map. */
export type LangIndex = Map<string, LangIndexEntry>;
