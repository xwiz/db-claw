import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { parse as parseSfc } from "@vue/compiler-sfc";
import {
	extractDefineModels,
	extractI18nVuetifyPairs,
	promoteBareRef,
	scanComponentModels,
	scanVueSfcs,
} from "./sfc.js";

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
		expect(f?.canonical.kind === "field" && f.canonical.field).toBe(
			"user.email",
		);
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
		expect(f?.canonical.kind === "field" && f.canonical.field).toBe(
			"user.email",
		);
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
			(f) =>
				f.canonical.kind === "field" && f.canonical.field === "user.status",
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
		const entityIndex = new Map([["user", new Set(["email", "isActive"])]]);
		const r = await scanVueSfcs(tmp, { entityIndex });
		const f = r.fragments[0];
		expect(f?.canonical.kind === "field" && f.canonical.field).toBe(
			"user.email",
		);
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
		// The AST walker collapses
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
		expect(f?.canonical.kind === "field" && f.canonical.field).toBe(
			"user.email",
		);
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
		expect(f?.canonical.kind === "field" && f.canonical.field).toBe(
			"user.email",
		);
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
		expect(r.skipped.filter((s) => s.reason.startsWith("SFC parse"))).toEqual(
			[],
		);
	});

	it("captures the absolute file line in locator.line", async () => {
		// Locator lines feed `semsql doctor` provenance — must point
		// at the LABEL's open tag, not the start of the template.
		const sfc =
			`<template>\n` +
			`  <div>\n` +
			`    <p>filler</p>\n` +
			`    <label for="email">Email</label>\n` + // line 4
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
		expect(f?.canonical.kind === "field" && f.canonical.field).toBe(
			"user.email",
		);
		expect(r.skipped.some((s) => s.reason.startsWith("SFC parse"))).toBe(true);
	});
});

describe("scanVueSfcs (Vuetify)", () => {
	it("emits fragments for v-text-field with static label + v-model", async () => {
		await write(
			"src/UserForm.vue",
			`<template>
              <v-form>
                <v-text-field label="Email Address" v-model="user.email" />
                <v-text-field label="Phone Number" v-model="user.phone" type="tel" />
              </v-form>
            </template>`,
		);
		const r = await scanVueSfcs(tmp);
		const map = new Map(
			r.fragments.map((f) => [
				f.term,
				f.canonical.kind === "field" ? f.canonical.field : "",
			]),
		);
		expect(map.get("email address")).toBe("user.email");
		expect(map.get("phone number")).toBe("user.phone");
		for (const f of r.fragments) {
			expect(f.locator.layer).toBe(6);
			expect(f.locator.extractor).toBe("extractor-vue:vuetify");
		}
	});

	it("recognises v-select / v-checkbox / v-switch / v-textarea", async () => {
		await write(
			"src/UserForm.vue",
			`<template>
              <v-select label="Status" v-model="user.status_code" :items="opts" />
              <v-checkbox label="Active" v-model="user.is_active" />
              <v-switch label="Two-Factor" v-model="user.two_factor" />
              <v-textarea label="Bio" v-model="user.bio" />
            </template>`,
		);
		const r = await scanVueSfcs(tmp);
		const fields = new Set(
			r.fragments.map((f) => f.canonical.kind === "field" && f.canonical.field),
		);
		expect(fields).toContain("user.status_code");
		expect(fields).toContain("user.is_active");
		expect(fields).toContain("user.two_factor");
		expect(fields).toContain("user.bio");
	});

	it("skips Vuetify with dynamic :label= bind", async () => {
		// `:label="..."` is a v-bind directive. AST exposes it as a
		// DirectiveNode, not a static AttributeNode — staticAttrValue
		// returns undefined. Walker correctly skips dynamic labels.
		await write(
			"src/UserForm.vue",
			`<template>
              <v-text-field :label="dyn" v-model="user.email" />
              <v-text-field v-bind:label="$t('users.email')" v-model="user.email" />
            </template>`,
		);
		const r = await scanVueSfcs(tmp);
		expect(r.fragments).toEqual([]);
	});

	it("skips Vuetify with empty label=''", async () => {
		await write(
			"src/UserForm.vue",
			`<template>
              <v-text-field label="" v-model="user.email" />
            </template>`,
		);
		const r = await scanVueSfcs(tmp);
		expect(r.fragments).toEqual([]);
	});

	it("skips Vuetify without v-model", async () => {
		// Display-only Vuetify components (e.g. v-card-title) carry
		// a label-like prop but no v-model — not vocabulary anchors.
		await write(
			"src/UserForm.vue",
			`<template>
              <v-text-field label="Email" />
            </template>`,
		);
		const r = await scanVueSfcs(tmp);
		expect(r.fragments).toEqual([]);
	});

	it("Vuetify pairs alongside <label>+v-model don't double-emit", async () => {
		// Mixed form: explicit-label idiom on one input + Vuetify on
		// another. Each walker contributes its own fragment; merge
		// engine downstream handles dedup.
		await write(
			"src/UserForm.vue",
			`<template>
              <form>
                <label for="email">Email</label>
                <input id="email" v-model="user.email" />

                <v-text-field label="Phone" v-model="user.phone" />
              </form>
            </template>`,
		);
		const r = await scanVueSfcs(tmp);
		const map = new Map(
			r.fragments.map((f) => [
				f.term,
				f.canonical.kind === "field" ? f.canonical.field : "",
			]),
		);
		expect(map.get("email")).toBe("user.email");
		expect(map.get("phone")).toBe("user.phone");
		expect(r.fragments.length).toBe(2);
	});

	it("Vuetify bare v-model promotes via Pinia entityIndex", async () => {
		await write(
			"src/UserForm.vue",
			`<template>
              <v-text-field label="Email" v-model="email" />
            </template>`,
		);
		const entityIndex = new Map([["user", new Set(["email", "phone"])]]);
		const r = await scanVueSfcs(tmp, { entityIndex });
		const f = r.fragments[0];
		expect(f).toBeDefined();
		expect(f?.canonical.kind === "field" && f.canonical.field).toBe(
			"user.email",
		);
		expect(f?.locator.extractor).toBe("extractor-vue:vuetify");
	});

	it("does not match non-Vuetify custom components even with label+v-model", async () => {
		// Match rule is restrictive: tag must start with `v-`. A
		// custom `<MyTextField>` carrying the same props is treated
		// as opaque (could be anything). Future framework adapters
		// can extend the prefix list.
		await write(
			"src/UserForm.vue",
			`<template>
              <MyTextField label="Email" v-model="user.email" />
              <CustomInput label="Phone" v-model="user.phone" />
            </template>`,
		);
		const r = await scanVueSfcs(tmp);
		expect(r.fragments).toEqual([]);
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

describe("scanVueSfcs — i18n binding via langIndex", () => {
	let dir: string;
	beforeEach(async () => {
		dir = await mkdtemp(path.join(tmpdir(), "semsql-sfc-i18n-"));
	});
	afterEach(async () => {
		await rm(dir, { recursive: true, force: true });
	});

	async function w(rel: string, body: string): Promise<void> {
		const full = path.join(dir, rel);
		await mkdir(path.dirname(full), { recursive: true });
		await writeFile(full, body, "utf8");
	}

	it("resolves :label=\"$t('key')\" via the supplied langIndex", async () => {
		await w(
			"src/components/UserForm.vue",
			`<template>
  <v-text-field :label="$t('users.full_name')" v-model="user.full_name" />
</template>`,
		);
		const langIndex = new Map([
			[
				"users.full_name",
				{ label: "Full Name", locale: "en", file: "x", line: 1 },
			],
		]);
		const r = await scanVueSfcs(dir, { langIndex });
		const fragments = r.fragments.filter((f) => f.canonical.kind === "field");
		expect(fragments).toHaveLength(1);
		const frag = fragments[0]!;
		expect(frag.term).toBe("full name");
		expect(frag.canonical).toEqual({
			kind: "field",
			field: "user.full_name",
		});
		expect(frag.confidence).toBeCloseTo(0.92);
		expect(frag.locator.extractor).toContain("vuetify-i18n:en");
	});

	it("records unresolved keys in skipped without crashing", async () => {
		await w(
			"src/components/Phantom.vue",
			`<template>
  <v-text-field :label="$t('does.not.exist')" v-model="user.x" />
</template>`,
		);
		const r = await scanVueSfcs(dir, { langIndex: new Map() });
		expect(r.fragments.filter((f) => f.canonical.kind === "field")).toEqual([]);
		expect(
			r.skipped.some((s) =>
				s.reason.includes("i18n key not in lang index: does.not.exist"),
			),
		).toBe(true);
	});

	it("does nothing when no langIndex supplied (back-compat)", async () => {
		await w(
			"src/components/UserForm.vue",
			`<template>
  <v-text-field :label="$t('users.full_name')" v-model="user.full_name" />
</template>`,
		);
		const r = await scanVueSfcs(dir);
		// No literal label, no resolved i18n key → no field fragment.
		expect(r.fragments.filter((f) => f.canonical.kind === "field")).toEqual([]);
	});
});

describe("extractI18nVuetifyPairs", () => {
	function parseTpl(tpl: string) {
		const sfc = `<template>${tpl}</template>`;
		const parsed = parseSfc(sfc, { filename: "test.vue" });
		if (!parsed.descriptor.template?.ast) {
			throw new Error("template parse failed");
		}
		return parsed.descriptor.template.ast;
	}

	it("captures vuetify-style :label=\"$t('key')\" + v-model pairs", () => {
		const ast = parseTpl(
			`<v-text-field :label="$t('users.email_label')" v-model="user.email" />`,
		);
		const pairs = extractI18nVuetifyPairs(ast);
		expect(pairs).toEqual([
			{ i18nKey: "users.email_label", vModel: "user.email", line: 1 },
		]);
	});

	it("accepts the bare `t(...)` helper too (Vue I18n composition API)", () => {
		const ast = parseTpl(
			`<v-text-field :label="t('users.full_name')" v-model="form.name" />`,
		);
		const pairs = extractI18nVuetifyPairs(ast);
		expect(pairs).toHaveLength(1);
		expect(pairs[0].i18nKey).toBe("users.full_name");
	});

	it("tolerates double-quoted i18n key literals", () => {
		const ast = parseTpl(
			`<v-text-field :label='$t("users.balance")' v-model="user.balance" />`,
		);
		const pairs = extractI18nVuetifyPairs(ast);
		expect(pairs[0]?.i18nKey).toBe("users.balance");
	});

	it("rejects $t() with replacements (multi-arg = dynamic)", () => {
		const ast = parseTpl(
			`<v-text-field :label="$t('users.greeting', { name })" v-model="user.name" />`,
		);
		expect(extractI18nVuetifyPairs(ast)).toEqual([]);
	});

	it("rejects $t() with a non-string-literal key", () => {
		const ast = parseTpl(
			`<v-text-field :label="$t(labelKey)" v-model="x.y" />`,
		);
		expect(extractI18nVuetifyPairs(ast)).toEqual([]);
	});

	it("ignores elements with no v-model", () => {
		const ast = parseTpl(`<v-text-field :label="$t('a.b')" />`);
		expect(extractI18nVuetifyPairs(ast)).toEqual([]);
	});

	it("ignores non-vuetify tags even when bound :label is present", () => {
		const ast = parseTpl(
			`<input :label="$t('users.email')" v-model="user.email" />`,
		);
		expect(extractI18nVuetifyPairs(ast)).toEqual([]);
	});

	it("captures multiple pairs from one template", () => {
		const ast = parseTpl(
			`<div>
                <v-text-field :label="$t('a.first')" v-model="form.first" />
                <v-text-field :label="$t('a.second')" v-model="form.second" />
            </div>`,
		);
		const pairs = extractI18nVuetifyPairs(ast);
		expect(pairs.map((p) => p.i18nKey)).toEqual(["a.first", "a.second"]);
		expect(pairs.map((p) => p.vModel)).toEqual(["form.first", "form.second"]);
	});
});

describe("extractDefineModels — Vue 3.4+ <script setup>", () => {
	it("captures the canonical `defineModel('name')` shape", () => {
		const src = `
            const email = defineModel('email')
            const balance = defineModel("balance", { default: 0 })
        `;
		const decls = extractDefineModels(src);
		expect(decls.map((d) => d.name)).toEqual(["email", "balance"]);
	});

	it("captures defineModel with TypeScript type arguments", () => {
		const src = `
            const email = defineModel<string>('email')
            const ts = defineModel<Record<string, number>>('totals')
        `;
		const decls = extractDefineModels(src);
		expect(decls.map((d) => d.name)).toEqual(["email", "totals"]);
	});

	it("captures defineModel() with no args (default modelValue)", () => {
		const src = `const value = defineModel()`;
		const decls = extractDefineModels(src);
		expect(decls).toHaveLength(1);
		expect(decls[0]!.name).toBe("");
	});

	it("captures defineModel<T>() with type args but no name", () => {
		const src = `const value = defineModel<string>()`;
		const decls = extractDefineModels(src);
		expect(decls).toHaveLength(1);
		expect(decls[0]!.name).toBe("");
	});

	it("rejects dynamic-name calls (variable, expression)", () => {
		const src = `
            const dynamic = defineModel(modelName)
            const concat = defineModel('a' + 'b')
        `;
		const decls = extractDefineModels(src);
		expect(decls).toEqual([]);
	});

	it("returns line numbers anchored at the defineModel keyword", () => {
		const src = `// line 1
const a = defineModel('a')
// line 3
const b = defineModel('b')`;
		const decls = extractDefineModels(src);
		expect(decls).toEqual([
			{ name: "a", line: 2 },
			{ name: "b", line: 4 },
		]);
	});

	it("returns empty for scripts without any defineModel reference", () => {
		const src = `
            import { ref } from 'vue'
            const x = ref(0)
            const y = computed(() => x.value * 2)
        `;
		expect(extractDefineModels(src)).toEqual([]);
	});

	it("captures multiple decls in one tight cluster", () => {
		const src = `
            const a = defineModel<string>('first_name')
            const b = defineModel<string>('last_name')
            const c = defineModel<number>('age')
        `;
		const decls = extractDefineModels(src);
		expect(decls.map((d) => d.name)).toEqual([
			"first_name",
			"last_name",
			"age",
		]);
	});

	it("captures kebab-case names verbatim (Vue accepts both)", () => {
		const src = `const x = defineModel('full-name')`;
		const decls = extractDefineModels(src);
		expect(decls[0]!.name).toBe("full-name");
	});
});

describe("scanComponentModels", () => {
	let dir: string;
	beforeEach(async () => {
		dir = await mkdtemp(path.join(tmpdir(), "semsql-cmodel-"));
	});
	afterEach(async () => {
		await rm(dir, { recursive: true, force: true });
	});
	async function w(rel: string, body: string): Promise<void> {
		const full = path.join(dir, rel);
		await mkdir(path.dirname(full), { recursive: true });
		await writeFile(full, body, "utf8");
	}

	it("indexes SFC files that declare defineModel(...)", async () => {
		await w(
			"src/components/UserForm.vue",
			`<script setup lang="ts">
const email = defineModel<string>('email')
const balance = defineModel<number>('balance')
</script>
<template><div /></template>`,
		);
		const r = await scanComponentModels(dir);
		const entries = Array.from(r.components.entries());
		expect(entries).toHaveLength(1);
		const [file, decls] = entries[0]!;
		expect(file).toContain("UserForm.vue");
		expect(decls.map((d) => d.name)).toEqual(["email", "balance"]);
	});

	it("skips SFCs with no script setup block", async () => {
		await w(
			"src/components/Static.vue",
			`<template><div>just markup</div></template>`,
		);
		const r = await scanComponentModels(dir);
		expect(r.components.size).toBe(0);
		expect(r.skipped).toEqual([]);
	});

	it("skips SFCs with a non-setup <script> block (defineModel is setup-only)", async () => {
		await w(
			"src/components/Options.vue",
			`<script>
export default { props: { email: String } }
</script>
<template><div /></template>`,
		);
		const r = await scanComponentModels(dir);
		expect(r.components.size).toBe(0);
	});

	it("walks nested component directories", async () => {
		await w(
			"src/components/forms/billing/BalanceField.vue",
			`<script setup>
const balance = defineModel('balance')
</script>
<template><input /></template>`,
		);
		const r = await scanComponentModels(dir);
		expect(r.components.size).toBe(1);
		const decls = Array.from(r.components.values())[0]!;
		expect(decls.map((d) => d.name)).toEqual(["balance"]);
	});

	it("ignores node_modules / dist / .vite", async () => {
		await w(
			"src/components/Real.vue",
			`<script setup>
const x = defineModel('x')
</script>`,
		);
		await w(
			"node_modules/lib/Lib.vue",
			`<script setup>
const ignored = defineModel('ignored')
</script>`,
		);
		const r = await scanComponentModels(dir);
		// Only `Real.vue` is recorded.
		expect(r.components.size).toBe(1);
		const file = Array.from(r.components.keys())[0]!;
		expect(file).toContain("Real.vue");
		expect(file).not.toContain("node_modules");
	});

	it("returns an empty index when no SFCs use defineModel", async () => {
		await w(
			"src/components/Plain.vue",
			`<script setup>
import { ref } from 'vue'
const x = ref(0)
</script>
<template><div /></template>`,
		);
		const r = await scanComponentModels(dir);
		expect(r.components.size).toBe(0);
	});
});
