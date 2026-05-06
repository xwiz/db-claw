/**
 * Rails extractor — v0.1 cut.
 *
 * Detects via `Gemfile` containing a `rails` or `activerecord` gem.
 * Walks `db/schema.rb` for ActiveRecord-canonical column declarations.
 *
 * Roadmap:
 *
 *  - v0.5: `db/migrate/*.rb` walker for incremental schema change
 *    visibility — schema.rb is point-in-time truth, but migrations
 *    carry semantic intent (column-rename history).
 *  - v0.5: `config/locales/*.yml` via @semsql/extractor-i18n —
 *    Rails apps overwhelmingly use translation keys for form labels.
 *  - v0.5: model `enum` declarations in `app/models/*.rb` — Rails
 *    enums map a symbol to an integer or string and are the
 *    primary value-vocabulary surface in idiomatic Rails apps.
 *  - v1.0: tree-sitter-ruby walker that handles `validates`,
 *    `scope`, and STI inheritance (Single-Table Inheritance) so
 *    base-class column inference matches what AR actually does at
 *    runtime.
 */

import { promises as fs } from "node:fs";
import path from "node:path";

import type { Extractor, ExtractCtx, VocabFragment } from "@semsql/extractor-sdk";

import { scanSchemaRb } from "./schema_rb.js";
import { scanLocales } from "./locales.js";
import { scanRailsEnums } from "./enums.js";
import { scanRailsValidates } from "./validates.js";

export {
    scanSchemaRb,
    parseSchemaRb,
    extractCreateTableBlocks,
    extractColumns,
    type SchemaRbScanResult,
} from "./schema_rb.js";

export {
    scanLocales,
    type LocalesScanResult,
    type LocalesScanOptions,
} from "./locales.js";

export {
    scanRailsEnums,
    extractTopLevelClassName,
    extractTableNameOverride,
    extractEnumDeclarations,
    inflectTableName,
    type EnumScanResult,
} from "./enums.js";

export {
    scanRailsValidates,
    extractInclusionValidators,
    type ValidatesScanResult,
} from "./validates.js";

export class RailsExtractor implements Extractor {
    public readonly name = "extractor-rails";

    async detect(root: string): Promise<boolean> {
        const gemfile = path.join(root, "Gemfile");
        try {
            const text = await fs.readFile(gemfile, "utf8");
            // Match the gem name only when it's the *exact* string
            // between the quotes — `\b` lets `gem 'rails-i18n'` slip
            // through because `\b` matches at the `s|-` boundary.
            // Rails apps invariably declare `gem 'rails'` (or
            // `'activerecord'` for AR-only setups) as a separate
            // line; this stricter check is what we want.
            return /\bgem\s+(['"])(rails|activerecord)\1/i.test(text);
        } catch {
            return false;
        }
    }

    async *extract(ctx: ExtractCtx): AsyncIterable<VocabFragment> {
        const schema = await scanSchemaRb(ctx.root);
        for (const f of schema.fragments) yield f;
        const locales = await scanLocales(ctx.root);
        for (const f of locales.fragments) yield f;
        const enums = await scanRailsEnums(ctx.root);
        for (const f of enums.fragments) yield f;
        const validates = await scanRailsValidates(ctx.root);
        for (const f of validates.fragments) yield f;
    }
}

export const RAILS_VERSION = "0.1.0-dev";
