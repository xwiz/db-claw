/**
 * Django extractor — v0.5 cut.
 *
 * Detects via `manage.py` at the project root or `django` in
 * `pyproject.toml` / `requirements.txt`. Walks every `models.py` file
 * under the project for `class Foo(models.Model)` definitions and
 * emits Orm-layer (=2) entity + field fragments.
 *
 * Roadmap:
 *
 *  - v0.5.x: `choices=[...]` enum extraction.
 *  - v0.6: DRF serializer walker — `serializers.py` with
 *    `ModelSerializer` `Meta.fields` / `Meta.read_only_fields`.
 *  - v0.6: Django admin (`admin.py`) — `list_display`,
 *    `verbose_name` overrides, `fieldsets`.
 *  - v1.0: cross-app vocabulary resolution (entity namespaces).
 */

import { promises as fs } from "node:fs";
import path from "node:path";

import type { Extractor, ExtractCtx, VocabFragment } from "@semsql/extractor-sdk";

import { scanDjangoAdmin } from "./admin.js";
import { scanDjangoModels } from "./models.js";
import { scanDjangoSerializers } from "./serializers.js";

export {
    scanDjangoModels,
    toSnakeCase,
    type DjangoModelsScanResult,
} from "./models.js";

export {
    scanDjangoSerializers,
    type DjangoSerializersScanResult,
} from "./serializers.js";

export {
    scanDjangoAdmin,
    type DjangoAdminScanResult,
} from "./admin.js";

export class DjangoExtractor implements Extractor {
    public readonly name = "extractor-django";

    async detect(root: string): Promise<boolean> {
        // 1. Most direct signal: `manage.py` next to the project root.
        if (await fileExists(path.join(root, "manage.py"))) return true;

        // 2. pyproject.toml mentioning `django` (PEP 621 deps or poetry).
        if (await fileMentions(path.join(root, "pyproject.toml"), /\bdjango\b/i)) {
            return true;
        }

        // 3. Classic requirements.txt.
        if (await fileMentions(path.join(root, "requirements.txt"), /^django\b/im)) {
            return true;
        }

        return false;
    }

    async *extract(ctx: ExtractCtx): AsyncIterable<VocabFragment> {
        const models = await scanDjangoModels(ctx.root);
        for (const f of models.fragments) yield f;
        const serializers = await scanDjangoSerializers(ctx.root);
        for (const f of serializers.fragments) yield f;
        const admin = await scanDjangoAdmin(ctx.root);
        for (const f of admin.fragments) yield f;
    }
}

export const DJANGO_VERSION = "0.6.0-dev";

async function fileExists(p: string): Promise<boolean> {
    try {
        const st = await fs.stat(p);
        return st.isFile();
    } catch {
        return false;
    }
}

async function fileMentions(p: string, rx: RegExp): Promise<boolean> {
    try {
        const text = await fs.readFile(p, "utf8");
        return rx.test(text);
    } catch {
        return false;
    }
}
