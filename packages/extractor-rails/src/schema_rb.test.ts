import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import {
	extractColumns,
	extractCreateTableBlocks,
	parseSchemaRb,
	scanSchemaRb,
} from "./schema_rb.js";

const SCHEMA = `
ActiveRecord::Schema[7.1].define(version: 2024_05_01_000000) do
  enable_extension "pgcrypto"

  create_table "users", force: :cascade do |t|
    t.string   "email", null: false
    t.boolean  "is_active", default: false, null: false
    t.integer  "tenant_id"
    t.references "manager", foreign_key: { to_table: :users }
    t.timestamps
    t.index ["tenant_id"], name: "index_users_on_tenant_id"
  end

  create_table "orders", id: :uuid, default: -> { "gen_random_uuid()" }, force: :cascade do |t|
    t.uuid     "user_id"
    t.decimal  "total", precision: 10, scale: 2
    t.string   "status", default: "pending"
    t.datetime "placed_at"
  end
end
`;

let tmp: string;

beforeEach(async () => {
	tmp = await mkdtemp(path.join(tmpdir(), "semsql-rails-"));
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

describe("extractCreateTableBlocks", () => {
	it("captures every create_table block by name + body", () => {
		const blocks = extractCreateTableBlocks(SCHEMA);
		const names = blocks.map((b) => b.tableName).sort();
		expect(names).toEqual(["orders", "users"]);
	});

	it("does not pick up create_table inside a comment", () => {
		const text = `
          # create_table "fake", force: :cascade do |t|
          #   t.string "email"
          # end
          ActiveRecord::Schema[7.1].define do
            create_table "real" do |t|
              t.string "name"
            end
          end
        `;
		const blocks = extractCreateTableBlocks(text);
		expect(blocks.map((b) => b.tableName)).toEqual(["real"]);
	});

	it("does not pick up create_table inside a string literal", () => {
		const text = `
          ActiveRecord::Schema.define do
            puts "create_table \\"fake\\" do |t| end"
            create_table "real" do |t|
              t.string "name"
            end
          end
        `;
		const blocks = extractCreateTableBlocks(text);
		expect(blocks.map((b) => b.tableName)).toEqual(["real"]);
	});

	it("matches the right `end` even when nested if/case appears in the body", () => {
		// Postfix `if` modifier — common in custom schema.rb edits.
		const text = `
          ActiveRecord::Schema.define do
            create_table "users" do |t|
              t.string "email" if true
              t.string "name"
            end
          end
        `;
		const blocks = extractCreateTableBlocks(text);
		expect(blocks.length).toBe(1);
		expect(blocks[0]!.body).toContain('t.string "name"');
	});
});

describe("extractColumns", () => {
	it("captures every t.<type> column with its DB name", () => {
		const block = extractCreateTableBlocks(SCHEMA).find(
			(b) => b.tableName === "users",
		)!;
		const cols = extractColumns(block.body);
		const names = cols.map((c) => c.dbName).sort();
		// Includes references-derived `manager_id` and timestamps.
		expect(names).toContain("email");
		expect(names).toContain("is_active");
		expect(names).toContain("tenant_id");
		expect(names).toContain("manager_id");
		expect(names).toContain("created_at");
		expect(names).toContain("updated_at");
	});

	it("flags references columns and emits the FK column name", () => {
		const block = extractCreateTableBlocks(SCHEMA).find(
			(b) => b.tableName === "users",
		)!;
		const cols = extractColumns(block.body);
		const manager = cols.find((c) => c.dbName === "manager_id")!;
		expect(manager.isReference).toBe(true);
		expect(manager.typeHelper).toBe("references");
	});

	it("skips index, primary_key, and other non-column helpers", () => {
		const block = extractCreateTableBlocks(SCHEMA).find(
			(b) => b.tableName === "users",
		)!;
		const cols = extractColumns(block.body);
		const types = cols.map((c) => c.typeHelper);
		expect(types).not.toContain("index");
		expect(types).not.toContain("primary_key");
	});

	it("expands t.timestamps to created_at + updated_at", () => {
		const cols = extractColumns(`
            t.string "email"
            t.timestamps
        `);
		expect(cols.map((c) => c.dbName).sort()).toEqual([
			"created_at",
			"email",
			"updated_at",
		]);
	});
});

describe("parseSchemaRb", () => {
	it("emits ORM-layer fragments at confidence 0.8", () => {
		const r = parseSchemaRb("db/schema.rb", SCHEMA);
		for (const f of r.fragments) {
			expect(f.locator.layer).toBe(2);
			expect(f.locator.extractor).toBe("extractor-rails:schema.rb");
			expect(f.confidence).toBe(0.8);
		}
	});

	it("strips trailing _id for foreign-key labels", () => {
		const r = parseSchemaRb("db/schema.rb", SCHEMA);
		const tenantFrag = r.fragments.find(
			(f) =>
				f.canonical.kind === "field" && f.canonical.field === "users.tenant_id",
		);
		expect(tenantFrag?.term).toBe("tenant");
	});

	it("emits canonical entity from the create_table arg", () => {
		const r = parseSchemaRb("db/schema.rb", SCHEMA);
		const orderFields = r.fragments.filter(
			(f) =>
				f.canonical.kind === "field" && f.canonical.field.startsWith("orders."),
		);
		expect(orderFields.length).toBeGreaterThan(0);
	});
});

describe("scanSchemaRb", () => {
	it("walks db/schema.rb if present", async () => {
		await write("db/schema.rb", SCHEMA);
		const r = await scanSchemaRb(tmp);
		expect(r.fragments.length).toBeGreaterThan(0);
	});

	it("returns empty when schema.rb is missing", async () => {
		const r = await scanSchemaRb(tmp);
		expect(r.fragments).toEqual([]);
		expect(r.skipped).toEqual([]);
	});
});
