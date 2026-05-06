import { mkdtemp, mkdir, writeFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import {
    extractEnumDeclarations,
    extractTableNameOverride,
    extractTopLevelClassName,
    inflectTableName,
    scanRailsEnums,
} from "./enums.js";

let tmp: string;

beforeEach(async () => {
    tmp = await mkdtemp(path.join(tmpdir(), "semsql-rails-enums-"));
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

describe("inflectTableName", () => {
    it("snake_cases CamelCase + pluralises with regular -s rule", () => {
        expect(inflectTableName("User")).toBe("users");
        expect(inflectTableName("Order")).toBe("orders");
    });

    it("preserves multi-word camelCase via underscore", () => {
        expect(inflectTableName("OrderItem")).toBe("order_items");
        expect(inflectTableName("UserAccount")).toBe("user_accounts");
    });

    it("applies -es to sibilant suffixes", () => {
        expect(inflectTableName("Address")).toBe("addresses");
        expect(inflectTableName("Box")).toBe("boxes");
        expect(inflectTableName("Buzz")).toBe("buzzes");
        expect(inflectTableName("Branch")).toBe("branches");
        expect(inflectTableName("Dish")).toBe("dishes");
    });

    it("applies -ies to consonant + y", () => {
        expect(inflectTableName("Story")).toBe("stories");
        expect(inflectTableName("Category")).toBe("categories");
    });

    it("preserves -ays / -eys / -oys", () => {
        // vowel + y → just add s
        expect(inflectTableName("Day")).toBe("days");
        expect(inflectTableName("Key")).toBe("keys");
    });
});

describe("extractTopLevelClassName", () => {
    it("captures the class declared at top level", () => {
        const text = `class User < ApplicationRecord\nend`;
        expect(extractTopLevelClassName(text)).toBe("User");
    });

    it("captures STI subclass with namespaced parent", () => {
        const text = `class AdminUser < Admin::ApplicationRecord\nend`;
        expect(extractTopLevelClassName(text)).toBe("AdminUser");
    });

    it("ignores commented-out class declarations", () => {
        const text = `# class Ghost < ApplicationRecord\nclass Real < ApplicationRecord\nend`;
        expect(extractTopLevelClassName(text)).toBe("Real");
    });

    it("returns null for files without a class", () => {
        expect(extractTopLevelClassName(`module Foo; end`)).toBeNull();
    });
});

describe("extractTableNameOverride", () => {
    it("captures string-literal override", () => {
        const text = `class User < ApplicationRecord
            self.table_name = "tbl_users_v1"
        end`;
        expect(extractTableNameOverride(text)).toBe("tbl_users_v1");
    });

    it("accepts single-quoted form", () => {
        expect(
            extractTableNameOverride(`self.table_name = 'legacy_users'`),
        ).toBe("legacy_users");
    });

    it("returns null for non-literal RHS", () => {
        const text = `self.table_name = Settings.table_name`;
        expect(extractTableNameOverride(text)).toBeNull();
    });

    it("ignores commented-out overrides", () => {
        const text = `# self.table_name = "fake"\nclass User\nend`;
        expect(extractTableNameOverride(text)).toBeNull();
    });

    it("returns null when no override is present", () => {
        expect(extractTableNameOverride(`class User; end`)).toBeNull();
    });
});

describe("extractEnumDeclarations", () => {
    it("parses keyword-style hash enums with integer values", () => {
        const text = `class User < ApplicationRecord
            enum status: { active: 0, archived: 1, banned: 2 }
        end`;
        const decls = extractEnumDeclarations(text);
        expect(decls.length).toBe(1);
        expect(decls[0]!.fieldName).toBe("status");
        expect([...decls[0]!.values]).toEqual([
            ["active", "0"],
            ["archived", "1"],
            ["banned", "2"],
        ]);
    });

    it("parses keyword-style hash enums with string values", () => {
        const text = `class User < ApplicationRecord
            enum status: { active: "active", archived: "archived" }
        end`;
        const decls = extractEnumDeclarations(text);
        expect(decls[0]!.values.get("archived")).toBe("archived");
    });

    it("parses Rails 7 positional-arg form", () => {
        const text = `class Order < ApplicationRecord
            enum :status, { pending: 0, paid: 1, refunded: 2 }
        end`;
        const decls = extractEnumDeclarations(text);
        expect(decls[0]!.fieldName).toBe("status");
        expect(decls[0]!.values.get("paid")).toBe("1");
    });

    it("parses array form with auto-numbering", () => {
        const text = `class Post < ApplicationRecord
            enum status: [:draft, :published, :archived]
        end`;
        const decls = extractEnumDeclarations(text);
        expect([...decls[0]!.values]).toEqual([
            ["draft", "0"],
            ["published", "1"],
            ["archived", "2"],
        ]);
    });

    it("parses Rails 7 positional array form", () => {
        const text = `enum :priority, [:low, :medium, :high]`;
        const decls = extractEnumDeclarations(text);
        expect([...decls[0]!.values]).toEqual([
            ["low", "0"],
            ["medium", "1"],
            ["high", "2"],
        ]);
    });

    it("captures multiple enum declarations on one model", () => {
        const text = `class User < ApplicationRecord
            enum status: { active: 0, archived: 1 }
            enum role: [:guest, :member, :admin]
        end`;
        const decls = extractEnumDeclarations(text);
        const fields = decls.map((d) => d.fieldName).sort();
        expect(fields).toEqual(["role", "status"]);
    });

    it("ignores enum-like text inside string literals", () => {
        const text = `class User < ApplicationRecord
            STATUS_DOC = "enum status: { active: 999 }"
            enum status: { active: 0 }
        end`;
        const decls = extractEnumDeclarations(text);
        // String-literal bodies are blanked during the comment-strip
        // pass, so the docstring's enum-like text cannot match.
        // Only the real declaration survives.
        expect(decls.length).toBe(1);
        expect(decls[0]!.values.get("active")).toBe("0");
    });

    it("ignores commented-out enum declarations", () => {
        const text = `class User < ApplicationRecord
            # enum status: { fake: 999 }
            enum status: { active: 0 }
        end`;
        const decls = extractEnumDeclarations(text);
        expect(decls.length).toBe(1);
        expect(decls[0]!.values.get("active")).toBe("0");
    });
});

describe("scanRailsEnums", () => {
    it("emits enum_value fragments tied to inflected entity name", async () => {
        await write(
            "app/models/user.rb",
            `class User < ApplicationRecord
                enum status: { active: 0, archived: 1 }
            end`,
        );
        const r = await scanRailsEnums(tmp);
        const archived = r.fragments.find((f) => f.term === "archived");
        expect(archived).toBeDefined();
        expect(archived?.canonical.kind).toBe("enum_value");
        if (archived?.canonical.kind === "enum_value") {
            expect(archived.canonical.enumName).toBe("users.status");
            expect(archived.canonical.rawValue).toBe("1");
        }
    });

    it("walks nested model directories", async () => {
        await write(
            "app/models/admin/role.rb",
            `class Role < ApplicationRecord
                enum tier: [:basic, :pro, :enterprise]
            end`,
        );
        const r = await scanRailsEnums(tmp);
        expect(r.fragments.length).toBe(3);
        const tier = r.fragments.find((f) => f.term === "enterprise");
        if (tier?.canonical.kind === "enum_value") {
            expect(tier.canonical.rawValue).toBe("2");
            expect(tier.canonical.enumName).toBe("roles.tier");
        }
    });

    it("returns empty when app/models is missing", async () => {
        const r = await scanRailsEnums(tmp);
        expect(r.fragments).toEqual([]);
    });

    it("ignores files without `enum` keyword (fast-path)", async () => {
        await write(
            "app/models/empty.rb",
            `class Empty < ApplicationRecord\nend`,
        );
        const r = await scanRailsEnums(tmp);
        expect(r.fragments).toEqual([]);
    });

    it("uses self.table_name override instead of inflector when present", async () => {
        await write(
            "app/models/legacy_user.rb",
            `class LegacyUser < ApplicationRecord
                self.table_name = "tbl_users_v1"
                enum status: { active: 0, archived: 1 }
            end`,
        );
        const r = await scanRailsEnums(tmp);
        const archived = r.fragments.find((f) => f.term === "archived");
        if (archived?.canonical.kind === "enum_value") {
            expect(archived.canonical.enumName).toBe("tbl_users_v1.status");
        } else {
            throw new Error("expected enum_value fragment");
        }
    });

    it("falls back to inflector when self.table_name is non-literal", async () => {
        await write(
            "app/models/dynamic.rb",
            `class Dynamic < ApplicationRecord
                self.table_name = Settings.table_name
                enum role: [:admin, :member]
            end`,
        );
        const r = await scanRailsEnums(tmp);
        const admin = r.fragments.find((f) => f.term === "admin");
        if (admin?.canonical.kind === "enum_value") {
            expect(admin.canonical.enumName).toBe("dynamics.role");
        } else {
            throw new Error("expected enum_value fragment");
        }
    });

    it("emits ORM-layer fragments at confidence 0.85", async () => {
        await write(
            "app/models/user.rb",
            `class User < ApplicationRecord
                enum status: { active: 0 }
            end`,
        );
        const r = await scanRailsEnums(tmp);
        for (const f of r.fragments) {
            expect(f.locator.layer).toBe(2);
            expect(f.locator.extractor).toBe("extractor-rails:enums");
            expect(f.confidence).toBe(0.85);
        }
    });
});
