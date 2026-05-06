import { mkdtemp, mkdir, writeFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import {
    extractColumns,
    extractTables,
    scanDrizzleSchemas,
} from "./drizzle.js";

let tmp: string;

beforeEach(async () => {
    tmp = await mkdtemp(path.join(tmpdir(), "semsql-drizzle-"));
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

describe("extractTables", () => {
    it("captures pgTable / mysqlTable / sqliteTable across helpers", () => {
        const text = `
            import { pgTable, mysqlTable, sqliteTable, integer, text } from "drizzle-orm/x";
            export const users = pgTable("users", { id: integer("id") });
            export const orders = mysqlTable("orders", { id: integer("id") });
            export const events = sqliteTable("events", { id: integer("id") });
        `;
        const tables = extractTables(text);
        const names = tables.map((t) => [t.varName, t.dbName]).sort();
        expect(names).toEqual([
            ["events", "events"],
            ["orders", "orders"],
            ["users", "users"],
        ]);
    });

    it("ignores TS-side helpers that look like *Table but aren't", () => {
        const text = `
            import { pgTable } from "drizzle-orm/pg-core";
            // Look-alike — should NOT match.
            const buildTable = (name: string) => pgTable(name, {});
            export const users = pgTable("users", { id: integer("id") });
        `;
        const tables = extractTables(text);
        expect(tables.map((t) => t.varName)).toEqual(["users"]);
    });

    it("survives nested braces in default expressions", () => {
        const text = `
            export const users = pgTable("users", {
              meta: jsonb("meta").default({ flags: { admin: false }, plan: "free" }),
              id: integer("id").primaryKey(),
            });
        `;
        const tables = extractTables(text);
        expect(tables.length).toBe(1);
        expect(tables[0]!.body).toContain("id: integer(\"id\")");
    });
});

describe("extractColumns", () => {
    it("ignores object-literal keys nested inside default() / $type() calls", () => {
        // The naive regex would match `flags: pick("admin")` here as
        // a top-level column, polluting the SemanticGraph with a
        // non-existent `entity.admin` field. Top-level-only scan must
        // skip everything inside a deeper-than-1 brace.
        const body = `
            id: integer("id").primaryKey(),
            meta: jsonb("meta").default({
              flags: pick("admin"),
              fallback: derive("legacy_admin"),
            }),
            email: text("email").notNull(),
        `;
        const cols = extractColumns(body);
        const tsNames = cols.map((c) => c.tsName).sort();
        expect(tsNames).toEqual(["email", "id", "meta"]);
    });

    it("captures property-name + first-string-arg pairs", () => {
        const body = `
            id: integer("id").primaryKey(),
            email: text("email").notNull(),
            isActive: boolean("is_active").default(false),
            tenantId: integer("tenant_id").notNull(),
            createdAt: timestamp("created_at").defaultNow(),
        `;
        const cols = extractColumns(body);
        const map = Object.fromEntries(cols.map((c) => [c.tsName, c.dbName]));
        expect(map).toEqual({
            id: "id",
            email: "email",
            isActive: "is_active",
            tenantId: "tenant_id",
            createdAt: "created_at",
        });
    });
});

describe("scanDrizzleSchemas", () => {
    const SAMPLE_SCHEMA = `
        import { pgTable, integer, text, boolean, timestamp } from "drizzle-orm/pg-core";

        export const users = pgTable("users", {
          id: integer("id").primaryKey(),
          email: text("email").notNull(),
          isActive: boolean("is_active").default(false),
          tenantId: integer("tenant_id").notNull(),
          createdAt: timestamp("created_at").defaultNow(),
        });
    `;

    it("emits ORM-layer field fragments with prettified labels", async () => {
        await write("src/db/schema.ts", SAMPLE_SCHEMA);
        const r = await scanDrizzleSchemas(tmp);
        const map = new Map(
            r.fragments.map((f) => [
                f.term,
                f.canonical.kind === "field" ? f.canonical.field : "",
            ]),
        );
        expect(map.get("email")).toBe("users.email");
        expect(map.get("is active")).toBe("users.is_active");
        expect(map.get("tenant")).toBe("users.tenant_id");
        expect(map.get("created at")).toBe("users.created_at");
        for (const f of r.fragments) {
            expect(f.locator.layer).toBe(2);
            expect(f.locator.extractor).toBe("extractor-nextjs:drizzle");
        }
    });

    it("scans every documented schema location", async () => {
        for (const loc of ["src/db/schema.ts", "lib/db/users.ts", "drizzle/orders.ts"]) {
            await write(loc, SAMPLE_SCHEMA);
        }
        const r = await scanDrizzleSchemas(tmp);
        // Three files × 5 emitted fragments each (id, email, isActive,
        // tenantId, createdAt). The `id` column survives the pretty-name
        // strip rule because `id` doesn't end in a separable `Id`/`_id`
        // suffix once the only segment IS `id`.
        expect(r.fragments.length).toBe(15);
    });

    it("flags var-name vs table-name mismatch in skipped log", async () => {
        await write(
            "src/db/schema.ts",
            `
            import { pgTable, integer } from "drizzle-orm/pg-core";
            export const accountTable = pgTable("users", { id: integer("id") });
            `,
        );
        const r = await scanDrizzleSchemas(tmp);
        expect(r.skipped.length).toBe(1);
        expect(r.skipped[0]!.reason).toContain("var name 'accountTable'");
        // Fragments still emit despite the warning — better partial vocab
        // than none.
        expect(r.fragments.find((f) => f.canonical.kind === "field")).toBeDefined();
    });

    it("skips files that don't import from drizzle-orm", async () => {
        await write(
            "src/db/non_schema.ts",
            `
            // Looks like a Drizzle schema but doesn't import from drizzle-orm.
            export const users = pgTable("users", { id: integer("id") });
            `,
        );
        const r = await scanDrizzleSchemas(tmp);
        expect(r.fragments).toEqual([]);
    });

    it("returns empty on a project with no drizzle dirs", async () => {
        const r = await scanDrizzleSchemas(tmp);
        expect(r.fragments).toEqual([]);
        expect(r.skipped).toEqual([]);
    });
});
