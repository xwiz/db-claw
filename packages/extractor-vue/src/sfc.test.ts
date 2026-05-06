import { mkdtemp, mkdir, writeFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { promoteBareRef, scanVueSfcs } from "./sfc.js";

let tmp: string;

beforeEach(async () => {
    tmp = await mkdtemp(path.join(tmpdir(), "semsql-vue-"));
});

afterEach(async () => {
    await rm(tmp, { recursive: true, force: true });
});

async function write(rel: string, body: string): Promise<string> {
    const full = path.join(tmp, rel);
    await mkdir(path.dirname(full), { recursive: true });
    await writeFile(full, body, "utf8");
    return full;
}

const FORM_SFC = `<template>
  <form>
    <label for="email">Email Address</label>
    <input id="email" v-model="user.email" />

    <label for="is_active">Account Status</label>
    <input id="is_active" v-model="user.is_active" type="checkbox" />
  </form>
</template>

<script setup lang="ts">
import { reactive } from "vue";
const user = reactive({ email: "", is_active: false });
</script>
`;

describe("scanVueSfcs", () => {
    it("emits FormOrTableLabel-layer fragments for explicit pairs", async () => {
        await write("src/components/UserForm.vue", FORM_SFC);
        const r = await scanVueSfcs(tmp);
        const map = new Map(
            r.fragments.map((f) => [
                f.term,
                f.canonical.kind === "field" ? f.canonical.field : "",
            ]),
        );
        expect(map.get("email address")).toBe("user.email");
        expect(map.get("account status")).toBe("user.is_active");
        for (const f of r.fragments) {
            expect(f.locator.layer).toBe(6);
            expect(f.locator.extractor).toBe("extractor-vue:label-vmodel");
        }
    });

    it("walks every conventional Vue source dir", async () => {
        for (const loc of [
            "src/UserForm.vue",
            "pages/Settings.vue",
            "components/Profile.vue",
            "views/Dashboard.vue",
        ]) {
            await write(loc, FORM_SFC);
        }
        const r = await scanVueSfcs(tmp);
        // 4 files × 2 fragments each = 8.
        expect(r.fragments.length).toBe(8);
    });

    it("skips bare v-model refs without entity context", async () => {
        await write(
            "src/Form.vue",
            `<template>
              <form>
                <label for="x">Bare</label>
                <input id="x" v-model="email" />
              </form>
            </template>`,
        );
        const r = await scanVueSfcs(tmp);
        expect(r.fragments).toEqual([]);
        expect(r.skipped.length).toBe(1);
        expect(r.skipped[0]!.reason).toContain("no entity prefix");
    });

    it("ignores SFCs with only a <script> block", async () => {
        await write(
            "src/util.vue",
            `<script setup lang="ts">
            export const greet = () => "hi";
            </script>`,
        );
        const r = await scanVueSfcs(tmp);
        expect(r.fragments).toEqual([]);
    });

    it("ignores label/input pairs whose ids don't match", async () => {
        await write(
            "src/Form.vue",
            `<template>
              <label for="email">Email</label>
              <input id="username" v-model="user.username" />
            </template>`,
        );
        const r = await scanVueSfcs(tmp);
        expect(r.fragments).toEqual([]);
    });

    it("handles attribute order — v-model before id", async () => {
        await write(
            "src/Form.vue",
            `<template>
              <label for="email">Email</label>
              <input v-model="user.email" id="email" />
            </template>`,
        );
        const r = await scanVueSfcs(tmp);
        const f = r.fragments.find((f) => f.term === "email");
        expect(f?.canonical.kind === "field" && f.canonical.field).toBe("user.email");
    });

    it("strips inner HTML / icons from label text", async () => {
        await write(
            "src/Form.vue",
            `<template>
              <label for="x">
                <Icon name="mail" />
                Email Address
              </label>
              <input id="x" v-model="user.email" />
            </template>`,
        );
        const r = await scanVueSfcs(tmp);
        const f = r.fragments.find((f) => f.term === "email address");
        expect(f).toBeDefined();
    });

    it("returns empty for projects without Vue files", async () => {
        const r = await scanVueSfcs(tmp);
        expect(r.fragments).toEqual([]);
    });

    it("emits a fragment for the implicit-label idiom (input nested in label)", async () => {
        await write(
            "src/Form.vue",
            `<template>
              <form>
                <label>
                  Email Address
                  <input v-model="user.email" />
                </label>
              </form>
            </template>`,
        );
        const r = await scanVueSfcs(tmp);
        const f = r.fragments.find((f) => f.term === "email address");
        expect(f).toBeDefined();
        expect(f?.canonical.kind === "field" && f.canonical.field).toBe("user.email");
    });

    it("does not double-emit when label has both for= and a nested input", async () => {
        // The explicit walker handles this — the implicit walker
        // must skip labels with `for=` so we don't double-count.
        await write(
            "src/Form.vue",
            `<template>
              <label for="email">
                Email
                <input id="email" v-model="user.email" />
              </label>
            </template>`,
        );
        const r = await scanVueSfcs(tmp);
        const matches = r.fragments.filter(
            (f) => f.canonical.kind === "field" && f.canonical.field === "user.email",
        );
        expect(matches.length).toBe(1);
    });

    it("handles a label wrapping multiple inputs (radio group)", async () => {
        await write(
            "src/Form.vue",
            `<template>
              <label>
                Status
                <input type="radio" v-model="user.status" value="active" />
                <input type="radio" v-model="user.status" value="inactive" />
              </label>
            </template>`,
        );
        const r = await scanVueSfcs(tmp);
        const statusFrags = r.fragments.filter(
            (f) => f.canonical.kind === "field" && f.canonical.field === "user.status",
        );
        // Both inputs emit; merge engine downstream deduplicates.
        expect(statusFrags.length).toBe(2);
    });

    it("promotes bare v-model ref via Pinia entityIndex", async () => {
        await write(
            "src/Form.vue",
            `<template>
              <label for="email">Email</label>
              <input id="email" v-model="email" />
            </template>`,
        );
        const entityIndex = new Map([
            ["user", new Set(["email", "isActive"])],
        ]);
        const r = await scanVueSfcs(tmp, { entityIndex });
        const f = r.fragments[0];
        expect(f?.canonical.kind === "field" && f.canonical.field).toBe("user.email");
        expect(r.skipped).toEqual([]);
    });

    it("logs ambiguous bare v-model when multiple stores claim the field", async () => {
        await write(
            "src/Form.vue",
            `<template>
              <label for="x">Email</label>
              <input id="x" v-model="email" />
            </template>`,
        );
        const entityIndex = new Map([
            ["user", new Set(["email"])],
            ["contact", new Set(["email", "phone"])],
        ]);
        const r = await scanVueSfcs(tmp, { entityIndex });
        expect(r.fragments).toEqual([]);
        expect(r.skipped[0]?.reason).toContain("ambiguous");
        expect(r.skipped[0]?.reason).toContain("user");
        expect(r.skipped[0]?.reason).toContain("contact");
    });

    it("falls back to skip when entityIndex is empty", async () => {
        await write(
            "src/Form.vue",
            `<template>
              <label for="x">X</label>
              <input id="x" v-model="email" />
            </template>`,
        );
        const r = await scanVueSfcs(tmp, { entityIndex: new Map() });
        expect(r.fragments).toEqual([]);
        expect(r.skipped[0]?.reason).toContain("no entity prefix");
    });
});

describe("scanVueSfcs (AST-only edge cases)", () => {
    it("handles attributes split across newlines", async () => {
        // The regex walker missed this — the v1.0 AST walker collapses
        // whitespace inside attribute lists naturally.
        await write(
            "src/Form.vue",
            `<template>
              <label
                for="email"
              >
                Email
              </label>
              <input
                id="email"
                type="email"
                v-model="user.email"
                placeholder="name@example.com"
              />
            </template>`,
        );
        const r = await scanVueSfcs(tmp);
        const f = r.fragments.find((f) => f.term === "email");
        expect(f?.canonical.kind === "field" && f.canonical.field).toBe("user.email");
    });

    it("walks inputs nested in v-if branches", async () => {
        await write(
            "src/Form.vue",
            `<template>
              <div v-if="open">
                <label for="email">Email</label>
                <input id="email" v-model="user.email" />
              </div>
            </template>`,
        );
        const r = await scanVueSfcs(tmp);
        const f = r.fragments.find((f) => f.term === "email");
        expect(f?.canonical.kind === "field" && f.canonical.field).toBe("user.email");
    });

    it("walks inputs nested in v-for templates", async () => {
        await write(
            "src/Form.vue",
            `<template>
              <template v-for="row in rows" :key="row.id">
                <label :for="'name-' + row.id">Name</label>
                <input id="name-1" v-model="row.name" />
              </template>
            </template>`,
        );
        const r = await scanVueSfcs(tmp);
        // Static id="name-1" pairs with static for via interpolation
        // failure (`:for=` is dynamic so not paired); the explicit
        // walker only resolves static for/id pairs. The implicit walker
        // doesn't apply here either (the input isn't nested in the
        // label). This case correctly produces zero fragments — the
        // assertion below is the regression guard against a future
        // change that "helpfully" fabricates a pair from a dynamic
        // attribute.
        expect(r.fragments).toEqual([]);
    });

    it("ignores dynamic :for / :id (expressions, not literals)", async () => {
        // Bound attrs can't be cross-referenced statically — Vue
        // compiler exposes them as DirectiveNode (name='bind'), our
        // walker only consults static AttributeNode. Verifies we
        // don't pair a dynamic :for with a static id by accident.
        await write(
            "src/Form.vue",
            `<template>
              <label :for="dynamicId">Email</label>
              <input id="dynamicId" v-model="user.email" />
            </template>`,
        );
        const r = await scanVueSfcs(tmp);
        expect(r.fragments).toEqual([]);
    });

    it("excludes interpolated text from the label", async () => {
        // {{ tr.email }} would have been included by the regex walker;
        // the AST walker skips INTERPOLATION nodes so we don't emit
        // a vocabulary term that's only a runtime computed string.
        await write(
            "src/Form.vue",
            `<template>
              <label for="email">
                Email — {{ tr.email }}
              </label>
              <input id="email" v-model="user.email" />
            </template>`,
        );
        const r = await scanVueSfcs(tmp);
        const f = r.fragments[0];
        expect(f).toBeDefined();
        // Interpolation stripped, em-dash + spaces collapsed.
        expect(f!.term).not.toContain("{{");
        expect(f!.term).not.toContain("tr.email");
        expect(f!.term).toContain("email");
    });

    it("handles multiple v-models on Vue 3 multi-bind shorthand", async () => {
        // `v-model:foo="x"` (named v-model on a custom component) is
        // surfaced by the AST as a DirectiveNode with name='model';
        // the expression `x` is what we extract as the bare ref.
        await write(
            "src/Form.vue",
            `<template>
              <UserPicker v-model:selection="user.selectedId" />
            </template>`,
        );
        // No <label> here, so the explicit / implicit walkers find no
        // pair. The walker must still parse the file without throwing.
        const r = await scanVueSfcs(tmp);
        expect(r.fragments).toEqual([]);
        expect(r.skipped.filter((s) => s.reason.startsWith("SFC parse"))).toEqual([]);
    });

    it("captures the absolute file line in locator.line", async () => {
        // Locator lines feed `semsql doctor` provenance — must point
        // at the LABEL's open tag, not the start of the template.
        const sfc =
            `<template>\n` +
            `  <div>\n` +
            `    <p>filler</p>\n` +
            `    <label for="email">Email</label>\n` +   // line 4
            `    <input id="email" v-model="user.email" />\n` +
            `  </div>\n` +
            `</template>\n`;
        await write("src/Form.vue", sfc);
        const r = await scanVueSfcs(tmp);
        const f = r.fragments[0];
        expect(f).toBeDefined();
        expect(f!.locator.line).toBe(4);
    });

    it("does not pair labels and inputs on empty for/id", async () => {
        // Boolean-style attrs `for` / `id` (or explicitly empty) must
        // not accidentally pair via shared empty-string index entries.
        await write(
            "src/Form.vue",
            `<template>
              <form>
                <label for="">Email</label>
                <input id="" v-model="user.email" />
              </form>
            </template>`,
        );
        const r = await scanVueSfcs(tmp);
        // No explicit pair; the implicit walker still considers the
        // empty-`for` label, but the input lives outside the label —
        // so no implicit pair either.
        expect(r.fragments).toEqual([]);
    });

    it("surfaces SFC parse warnings via skipped without dropping the run", async () => {
        // Mismatched closer — Vue parser emits a warning but still
        // returns a usable AST. The walker must record the warning
        // but keep extracting from whatever was parsed.
        await write(
            "src/Form.vue",
            `<template>
              <div>
                <label for="email">Email</label>
                <input id="email" v-model="user.email" />
              </span>
            </template>`,
        );
        const r = await scanVueSfcs(tmp);
        const f = r.fragments.find((f) => f.term === "email");
        expect(f?.canonical.kind === "field" && f.canonical.field).toBe("user.email");
        expect(r.skipped.some((s) => s.reason.startsWith("SFC parse"))).toBe(true);
    });
});

describe("promoteBareRef", () => {
    it("returns the unambiguous match when exactly one entity has the field", () => {
        const idx = new Map([
            ["user", new Set(["email"])],
            ["order", new Set(["total"])],
        ]);
        expect(promoteBareRef("email", idx)).toEqual({
            entity: "user",
            field: "email",
        });
    });

    it("returns the candidate list when multiple entities claim the field", () => {
        const idx = new Map([
            ["user", new Set(["email"])],
            ["contact", new Set(["email"])],
        ]);
        expect(promoteBareRef("email", idx)).toEqual(["user", "contact"]);
    });

    it("returns null when no entity has the field", () => {
        const idx = new Map([["user", new Set(["email"])]]);
        expect(promoteBareRef("phone", idx)).toBeNull();
    });

    it("uses the last dotted segment for index lookup", () => {
        const idx = new Map([["user", new Set(["email"])]]);
        // `form.email` → look up `email` against the index. Promoted
        // because exactly one entity has it.
        expect(promoteBareRef("form.email", idx)).toEqual({
            entity: "user",
            field: "email",
        });
    });

    it("returns null when index is undefined", () => {
        expect(promoteBareRef("email", undefined)).toBeNull();
    });
});
