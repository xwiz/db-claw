/**
 * i18n parsers — ICU MessageFormat (JSON), gettext (.po), Rails YAML, Laravel lang.
 *
 * These are *building blocks* used by every framework adapter, not standalone
 * extractors. They produce raw `(key → label)` maps; the framework adapter
 * decides which keys are entity/field/enum vocabulary.
 *
 * v0.2 ships ICU JSON + Laravel PHP-array. v0.5 adds gettext + Rails YAML.
 */

export interface I18nEntry {
    /** Translation key, dotted: `"models.user.singular"`. */
    key: string;
    /** Locale: `"en"`, `"fr"`, `"de"`, ... */
    locale: string;
    /** Value (may contain ICU placeholders — the adapter strips them). */
    value: string;
    /** Source file for provenance. */
    file: string;
    /** Source line — best effort, may be approximate. */
    line: number;
}

export const I18N_VERSION = "0.1.0-dev";
