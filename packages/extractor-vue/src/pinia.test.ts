import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { extractStoreFields, extractStores, scanPiniaStores } from "./pinia.js";

let tmp: string;

beforeEach(async () => {
	tmp = await mkdtemp(path.join(tmpdir(), "semsql-pinia-"));
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

const OPTIONS_STORE = `
import { defineStore } from "pinia";

export const useUserStore = defineStore("user", {
    state: () => ({
        email: "",
        isActive: false,
        tenantId: null as number | null,
    }),
    actions: {
        async login() {
            // ...
        },
    },
});
`;

const SETUP_STORE = `
import { defineStore } from "pinia";
import { ref, computed } from "vue";

export const useOrderStore = defineStore("order", () => {
    const total = ref(0);
    const status = ref("pending");
    const itemCount = computed(() => 0);

    function reset() {
        total.value = 0;
    }

    return { total, status, itemCount, reset };
});
`;

describe("extractStores", () => {
	it("captures options-form stores by name + body", () => {
		const stores = extractStores(OPTIONS_STORE);
		expect(stores.length).toBe(1);
		expect(stores[0]!.storeName).toBe("user");
		expect(stores[0]!.isSetupForm).toBe(false);
	});

	it("captures setup-form stores", () => {
		const stores = extractStores(SETUP_STORE);
		expect(stores.length).toBe(1);
		expect(stores[0]!.storeName).toBe("order");
		expect(stores[0]!.isSetupForm).toBe(true);
	});

	it("does not pick up `defineStore(` inside a comment", () => {
		const text = `
            // example: defineStore("ghost", { ... })
            export const useReal = defineStore("real", { state: () => ({ x: 1 }) });
        `;
		const stores = extractStores(text);
		expect(stores.map((s) => s.storeName)).toEqual(["real"]);
	});
});

describe("extractStoreFields", () => {
	it("returns top-level keys from options-form state factory", () => {
		const store = extractStores(OPTIONS_STORE)[0]!;
		const keys = extractStoreFields(store.body, false);
		expect(keys.sort()).toEqual(["email", "isActive", "tenantId"]);
	});

	it("returns top-level keys from setup-form return", () => {
		const store = extractStores(SETUP_STORE)[0]!;
		const keys = extractStoreFields(store.body, true);
		// `reset` is a function — included because we don't try to
		// type-check; the merge engine drops obvious non-field names
		// via downstream conflict heuristics.
		expect(keys).toContain("total");
		expect(keys).toContain("status");
		expect(keys).toContain("itemCount");
	});

	it("does not include keys nested in inner object literals", () => {
		const body = `
            state: () => ({
                profile: { name: "x", role: "y" },
                email: "",
            })
        `;
		const keys = extractStoreFields(body, false);
		expect(keys).toContain("profile");
		expect(keys).toContain("email");
		expect(keys).not.toContain("name");
		expect(keys).not.toContain("role");
	});

	it("returns empty when no state factory or return statement", () => {
		expect(extractStoreFields("getters: { x: () => 1 }", false)).toEqual([]);
		expect(extractStoreFields("const x = 1;", true)).toEqual([]);
	});
});

describe("scanPiniaStores", () => {
	it("walks every conventional Pinia store dir", async () => {
		await write("src/stores/user.ts", OPTIONS_STORE);
		await write("stores/order.ts", SETUP_STORE);
		const r = await scanPiniaStores(tmp);
		const fields = r.fragments.map(
			(f) => f.canonical.kind === "field" && f.canonical.field,
		);
		// Canonical names preserve TS-side casing; sanitiseCanonical
		// is identity for valid identifiers. Snake-casing happens at
		// graph-merge time when DB-introspection fragments win on
		// confidence. The display label, separately, IS prettified.
		expect(fields).toContain("user.email");
		expect(fields).toContain("user.isActive");
		expect(fields).toContain("order.total");
	});

	it("populates entityIndex for v-model promotion", async () => {
		await write("src/stores/user.ts", OPTIONS_STORE);
		const r = await scanPiniaStores(tmp);
		const userFields = r.entityIndex.get("user");
		expect(userFields).toBeDefined();
		expect(userFields!.has("email")).toBe(true);
		expect(userFields!.has("tenantId")).toBe(true);
		expect(userFields!.has("isActive")).toBe(true);
	});

	it("emits ORM-layer fragments at confidence 0.5", async () => {
		await write("src/stores/user.ts", OPTIONS_STORE);
		const r = await scanPiniaStores(tmp);
		for (const f of r.fragments) {
			expect(f.locator.layer).toBe(2);
			expect(f.locator.extractor).toBe("extractor-vue:pinia");
			expect(f.confidence).toBe(0.5);
		}
	});

	it("ignores non-store TS files in store dirs", async () => {
		await write("src/stores/index.ts", `export * from "./user";`);
		const r = await scanPiniaStores(tmp);
		expect(r.fragments).toEqual([]);
	});

	it("skips empty defineStore (no recognisable state)", async () => {
		await write(
			"src/stores/empty.ts",
			`import { defineStore } from "pinia";
            export const useEmpty = defineStore("empty", { actions: {} });`,
		);
		const r = await scanPiniaStores(tmp);
		expect(r.fragments).toEqual([]);
		expect(r.skipped.length).toBe(1);
	});
});
