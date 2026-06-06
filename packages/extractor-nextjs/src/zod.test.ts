import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import {
	entityFromSchemaName,
	findFieldsWithDescribe,
	findZodObjectBlocks,
	scanZodSchemas,
} from "./zod.js";

let tmp: string;

beforeEach(async () => {
	tmp = await mkdtemp(path.join(tmpdir(), "semsql-nextjs-zod-"));
});

afterEach(async () => {
	await rm(tmp, { recursive: true, force: true });
});

async function write(rel: string, body: string): Promise<void> {
	const full = path.join(tmp, rel);
	await mkdir(path.dirname(full), { recursive: true });
	await writeFile(full, body, "utf8");
}

describe("scanZodSchemas", () => {
	it('emits ApiResource-layer fragments for `field: z.<chain>.describe("...")`', async () => {
		await write(
			"schemas/user.ts",
			`import { z } from "zod";

export const userSchema = z.object({
    email: z.string().email().describe("Email Address"),
    isActive: z.boolean().describe("Account Status"),
});
`,
		);
		const r = await scanZodSchemas(tmp);
		const map = new Map(
			r.fragments.map((f) => [
				f.term,
				f.canonical.kind === "field" ? f.canonical.field : "",
			]),
		);
		expect(map.get("email address")).toBe("user.email");
		expect(map.get("account status")).toBe("user.is_active");
		for (const f of r.fragments) {
			expect(f.locator.layer).toBe(4); // ApiResource
			expect(f.locator.extractor).toBe("extractor-nextjs:zod");
		}
	});

	it("strips Schema/Validator/Insert/Update/Form suffixes", async () => {
		await write(
			"validators/post.ts",
			`import { z } from "zod";
export const PostInsertSchema = z.object({
    title: z.string().describe("Title"),
});
export const postFormValidator = z.object({
    body: z.string().describe("Body"),
});
`,
		);
		const r = await scanZodSchemas(tmp);
		const fields = new Set(
			r.fragments.map((f) =>
				f.canonical.kind === "field" ? f.canonical.field : "",
			),
		);
		expect(fields).toContain("post.title");
		expect(fields).toContain("post.body");
	});

	it("walks every conventional Next.js source dir", async () => {
		for (const loc of [
			"src/schemas/a.ts",
			"lib/b.ts",
			"actions/c.ts",
			"validators/d.ts",
			"app/api/e.ts",
		]) {
			await write(
				loc,
				`import { z } from "zod";
export const fooSchema = z.object({ bar: z.string().describe("Bar") });
`,
			);
		}
		const r = await scanZodSchemas(tmp);
		expect(r.fragments.length).toBe(5);
	});

	it("handles single-quoted property keys and string args", async () => {
		await write(
			"schemas/user.ts",
			`import { z } from "zod";
export const userSchema = z.object({
    'email': z.string().describe('Email'),
    "is_admin": z.boolean().describe("Is Admin"),
});
`,
		);
		const r = await scanZodSchemas(tmp);
		const fields = new Set(
			r.fragments.map((f) =>
				f.canonical.kind === "field" ? f.canonical.field : "",
			),
		);
		expect(fields).toContain("user.email");
		expect(fields).toContain("user.is_admin");
	});

	it("ignores fields without .describe()", async () => {
		await write(
			"schemas/user.ts",
			`import { z } from "zod";
export const userSchema = z.object({
    email: z.string().email(),
    name: z.string(),
});
`,
		);
		const r = await scanZodSchemas(tmp);
		expect(r.fragments).toEqual([]);
	});

	it("rejects describe(<expression>) when arg is not a string literal", async () => {
		await write(
			"schemas/user.ts",
			`import { z } from "zod";
const lbl = "Email";
export const userSchema = z.object({
    email: z.string().describe(lbl),
});
`,
		);
		const r = await scanZodSchemas(tmp);
		expect(r.fragments).toEqual([]);
	});

	it("survives nested z.object schemas without crashing", async () => {
		await write(
			"schemas/user.ts",
			`import { z } from "zod";
export const userSchema = z.object({
    email: z.string().describe("Email"),
    address: z.object({
        city: z.string().describe("City"),
    }),
});
`,
		);
		const r = await scanZodSchemas(tmp);
		// Top-level email and city both surface (city via nested
		// z.object that the block walker re-discovers as its own
		// schema block; entity name comes from the outer var, so
		// city lands on user.address, but this reader just emits
		// the top-level field). Assert the top-level field at
		// minimum; nested-path tracking is documented out-of-scope.
		const terms = r.fragments.map((f) => f.term);
		expect(terms).toContain("email");
	});

	it("handles describe() string with escaped quotes", async () => {
		await write(
			"schemas/user.ts",
			`import { z } from "zod";
export const userSchema = z.object({
    nickname: z.string().describe("User\\'s nickname"),
});
`,
		);
		const r = await scanZodSchemas(tmp);
		const f = r.fragments[0]!;
		expect(f).toBeDefined();
		expect(f.term).toBe("user's nickname");
	});

	it("captures absolute file line in locator", async () => {
		const src =
			`import { z } from "zod";\n` +
			`\n` +
			`export const userSchema = z.object({\n` +
			`    email: z.string().describe("Email"),\n` + // line 4
			`});\n`;
		await write("schemas/user.ts", src);
		const r = await scanZodSchemas(tmp);
		expect(r.fragments[0]!.locator.line).toBe(4);
	});

	it("skips node_modules and .next dirs", async () => {
		await write(
			"node_modules/zod/lib/types.ts",
			`export const xSchema = z.object({ a: z.string().describe("A") });`,
		);
		await write(
			".next/server/foo.ts",
			`export const ySchema = z.object({ b: z.string().describe("B") });`,
		);
		const r = await scanZodSchemas(tmp);
		expect(r.fragments).toEqual([]);
	});

	it("returns empty for projects with no Zod schemas", async () => {
		await write("src/index.ts", "export const greet = () => 'hi';");
		const r = await scanZodSchemas(tmp);
		expect(r.fragments).toEqual([]);
	});
});

describe("entityFromSchemaName", () => {
	it.each([
		["userSchema", "user"],
		["UserSchema", "user"],
		["UserInsertSchema", "user"],
		["UserUpdateValidator", "user"],
		["postFormValidator", "post"],
		["users", "users"],
		["Posts", "posts"],
		["UserProfile", "user_profile"],
	])("%s → %s", (input, expected) => {
		expect(entityFromSchemaName(input)).toBe(expected);
	});

	it("returns null when stripping yields empty", () => {
		expect(entityFromSchemaName("Schema")).toBeNull();
		expect(entityFromSchemaName("Validator")).toBeNull();
	});
});

describe("findZodObjectBlocks", () => {
	it("balances braces inside template strings + nested objects", () => {
		const src = `
const x = z.object({
    a: z.string().describe("a {nested} \${var}"),
    b: z.object({ c: z.string() }),
});
const y = z.object({ d: z.number() });
`;
		const blocks = findZodObjectBlocks(src);
		expect(blocks.length).toBe(2);
		expect(blocks[0]!.schemaName).toBe("x");
		expect(blocks[1]!.schemaName).toBe("y");
	});
});

describe("findFieldsWithDescribe", () => {
	it("only captures fields whose chain ends in .describe(string)", () => {
		const body = `
    a: z.string().describe("Alpha"),
    b: z.number(),
    c: z.string().min(1).describe("Charlie").optional(),
`;
		const hits = findFieldsWithDescribe(body, 0);
		const fields = hits.map((h) => [h.fieldName, h.label]);
		expect(fields).toEqual([
			["a", "Alpha"],
			["c", "Charlie"],
		]);
	});
});
