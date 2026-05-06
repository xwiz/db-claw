/**
 * Vue 3 extractor — v1.0 (AST-driven).
 *
 * Detects via `vue` in package.json. Walks `src/`, `pages/`,
 * `components/`, `views/`, `app/` for `.vue` files; emits FormOrTableLabel
 * (=6) field fragments from `<label for="x">Text</label>` paired with
 * `<input id="x" v-model="entity.field"/>`, plus the implicit-label
 * idiom `<label>Text <input v-model="..."/></label>`.
 *
 * The walker is AST-driven via `@vue/compiler-sfc`, so attribute order,
 * multi-line attribute lists, conditional templates (`v-if`/`v-for`),
 * and nested `<template>` wrappers all parse correctly without
 * per-construct regex.
 *
 * Pinia stores are inspected first so the SFC walker can promote
 * bare-ref `v-model="email"` to `user.email` using the returned entity
 * index.
 *
 * Roadmap:
 *
 *  - v1.1: Vuetify component awareness (`<v-text-field label="Email">`).
 *  - v1.1: Resolve sub-path refs (`form.email.value`) through the
 *    script-setup AST so locally-named reactive refs map back to the
 *    underlying store entity.
 */

import { promises as fs } from "node:fs";
import path from "node:path";

import type { Extractor, ExtractCtx, VocabFragment } from "@semsql/extractor-sdk";

import { scanVueSfcs } from "./sfc.js";
import { scanPiniaStores } from "./pinia.js";

export {
    scanVueSfcs,
    type VueScanResult,
} from "./sfc.js";

export {
    scanPiniaStores,
    extractStores,
    extractStoreFields,
    type PiniaScanResult,
} from "./pinia.js";

export class VueExtractor implements Extractor {
    public readonly name = "extractor-vue";

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
            return "vue" in deps;
        } catch {
            return false;
        }
    }

    async *extract(ctx: ExtractCtx): AsyncIterable<VocabFragment> {
        // Scan Pinia stores first so the SFC walker can promote
        // bare-ref `v-model="email"` to `user.email` using the
        // returned entity index. The two passes are independent
        // otherwise — both emit ORM-layer fragments.
        const pinia = await scanPiniaStores(ctx.root);
        for (const f of pinia.fragments) yield f;
        const sfcs = await scanVueSfcs(ctx.root, { entityIndex: pinia.entityIndex });
        for (const f of sfcs.fragments) yield f;
    }
}

export const VUE_VERSION = "0.1.0-dev";
