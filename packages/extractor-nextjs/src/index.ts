/**
 * Next.js extractor — v0.2 priority adapter.
 *
 * Coverage in v0.2:
 *
 *  - **Drizzle** schema files (`pgTable` / `mysqlTable` / `sqliteTable`)
 *    via {@link scanDrizzleSchemas}. Emits ORM-layer (=2) field
 *    fragments with prettified TS-name labels.
 *
 * Roadmap:
 *
 *  - v0.5: Prisma `schema.prisma` parser (Prisma's own DSL — small
 *    enough for a hand-rolled lexer).
 *  - v0.5: Zod-form-schema → field-existence fragments at layer 4 (API
 *    resource): server-action zod schemas typically declare the same
 *    shape as the rendered React form.
 *  - v0.5: `next-intl` JSON dictionaries via @semsql/extractor-i18n.
 *  - v1.0: React `<label>` adjacent to `<input name="...">` for layer 6
 *    extraction (analogous to Filament's form walker).
 *
 * Detection: presence of `next` in package.json `dependencies` /
 * `devDependencies`. A monorepo with multiple Next.js apps therefore
 * detects at the workspace root iff any package depends on Next; the
 * walker visits the conventional schema dirs underneath.
 */

import { promises as fs } from "node:fs";
import path from "node:path";

import type { Extractor, ExtractCtx, VocabFragment } from "@semsql/extractor-sdk";

import { scanDrizzleSchemas } from "./drizzle.js";
import { scanPrismaSchemas } from "./prisma.js";

export {
    scanDrizzleSchemas,
    extractTables,
    extractColumns,
    type DrizzleScanResult,
} from "./drizzle.js";

export {
    scanPrismaSchemas,
    parsePrismaSchema,
    extractModelBlocks,
    extractFields,
    extractTableMap,
    type PrismaScanResult,
} from "./prisma.js";

export class NextjsExtractor implements Extractor {
    public readonly name = "extractor-nextjs";

    async detect(root: string): Promise<boolean> {
        const pkg = path.join(root, "package.json");
        try {
            const text = await fs.readFile(pkg, "utf8");
            const parsed = JSON.parse(text) as {
                dependencies?: Record<string, string>;
                devDependencies?: Record<string, string>;
            };
            const deps = {
                ...(parsed.dependencies ?? {}),
                ...(parsed.devDependencies ?? {}),
            };
            return "next" in deps;
        } catch {
            return false;
        }
    }

    async *extract(ctx: ExtractCtx): AsyncIterable<VocabFragment> {
        const drizzle = await scanDrizzleSchemas(ctx.root);
        for (const f of drizzle.fragments) yield f;
        const prisma = await scanPrismaSchemas(ctx.root);
        for (const f of prisma.fragments) yield f;
    }
}

export const NEXTJS_VERSION = "0.1.0-dev";
