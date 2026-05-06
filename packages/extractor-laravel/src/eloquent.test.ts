import { mkdtemp, rm, mkdir, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { parseModelProperties, scanEloquentModels } from "./eloquent.js";

let tmp: string;

beforeEach(async () => {
    tmp = await mkdtemp(path.join(tmpdir(), "semsql-eloq-"));
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

describe("parseModelProperties", () => {
    it("captures $table, $fillable, $casts on a typical model", () => {
        const text = `<?php
            namespace App\\Models;
            class User extends Model {
                protected $table = 'users_legacy';
                protected $fillable = ['name', 'email', 'is_active'];
                protected $casts = [
                    'is_active' => 'bool',
                    'preferences' => 'array',
                ];
            }
        `;
        const p = parseModelProperties(text);
        expect(p.table).toBe("users_legacy");
        expect(p.fillable.map((f) => f.name)).toEqual(["name", "email", "is_active"]);
        expect(p.casts).toEqual({
            is_active: "bool",
            preferences: "array",
        });
    });

    it("handles typed declarations and visibility modifiers", () => {
        const text = `<?php
            class Foo extends Model {
                public string $table = 'foo';
                public array $fillable = ['x', 'y'];
            }
        `;
        const p = parseModelProperties(text);
        expect(p.table).toBe("foo");
        expect(p.fillable.map((f) => f.name)).toEqual(["x", "y"]);
    });

    it("returns empty when no properties present", () => {
        const text = `<?php class Empty extends Model {}`;
        const p = parseModelProperties(text);
        expect(p.fillable).toEqual([]);
        expect(p.casts).toEqual({});
        expect(p.table).toBeUndefined();
    });

    it("survives nested arrays and trailing commas", () => {
        const text = `<?php
            class Foo extends Model {
                protected $fillable = [
                    'a',
                    'b',
                ];
                protected $casts = [
                    'meta' => 'json',
                    'nested_default' => 'array',
                ];
            }
        `;
        const p = parseModelProperties(text);
        expect(p.fillable.map((f) => f.name)).toEqual(["a", "b"]);
        expect(p.casts.meta).toBe("json");
        expect(p.casts.nested_default).toBe("array");
    });
});

describe("scanEloquentModels", () => {
    it("emits ORM-layer field fragments with prettified labels", async () => {
        await write(
            "app/Models/User.php",
            `<?php
            namespace App\\Models;
            class User extends Model {
                protected $fillable = ['email', 'is_active', 'tenant_id'];
            }`,
        );
        const r = await scanEloquentModels(tmp);
        const map = new Map(
            r.fragments.map((f) => [
                f.term,
                f.canonical.kind === "field" ? f.canonical.field : "",
            ]),
        );
        expect(map.get("email")).toBe("users.email");
        expect(map.get("is active")).toBe("users.is_active");
        // `_id` suffix is stripped when prettifying.
        expect(map.get("tenant")).toBe("users.tenant_id");
        for (const f of r.fragments) {
            expect(f.locator.layer).toBe(2);
        }
    });

    it("respects $table override over class-name convention", async () => {
        await write(
            "app/Models/Person.php",
            `<?php
            namespace App\\Models;
            class Person extends Model {
                protected $table = 'staff_members';
                protected $fillable = ['name'];
            }`,
        );
        const r = await scanEloquentModels(tmp);
        expect(r.classToEntity.get("Person")).toBe("staff_members");
        expect(r.classToEntity.get("App\\Models\\Person")).toBe("staff_members");
        const fieldFrag = r.fragments.find((f) => f.canonical.kind === "field");
        expect(fieldFrag?.canonical.kind === "field" && fieldFrag.canonical.field).toBe(
            "staff_members.name",
        );
    });

    it("ignores files that don't extend a known Model base", async () => {
        await write("app/Models/Helper.php", `<?php class Helper {}`);
        await write(
            "app/Models/Service.php",
            `<?php class Service extends \\Illuminate\\Support\\ServiceProvider {}`,
        );
        const r = await scanEloquentModels(tmp);
        expect(r.fragments).toEqual([]);
        expect(r.classToEntity.size).toBe(0);
    });

    it("skips Filament/Http/Console subdirs to keep the walk proportional", async () => {
        await write(
            "app/Filament/Resources/StudentResource.php",
            `<?php class Student extends Model { protected $fillable = ['x']; }`,
        );
        await write(
            "app/Models/User.php",
            `<?php class User extends Model { protected $fillable = ['email']; }`,
        );
        const r = await scanEloquentModels(tmp);
        // Only User contributes; the Filament dir is skipped.
        expect(r.classToEntity.get("User")).toBe("users");
        expect(r.classToEntity.has("Student")).toBe(false);
    });

    it("emits casts-only fields when $fillable is absent", async () => {
        await write(
            "app/Models/Setting.php",
            `<?php class Setting extends Model {
                protected $casts = ['value' => 'json'];
            }`,
        );
        const r = await scanEloquentModels(tmp);
        const f = r.fragments.find(
            (f) => f.canonical.kind === "field" && f.canonical.field === "settings.value",
        );
        expect(f).toBeDefined();
        expect(f?.locator.extractor).toContain("eloquent:casts");
    });

    it("Filament walker resolves $model via Eloquent classToEntity index", async () => {
        // Eloquent model has $table override, Filament Resource references it.
        await write(
            "app/Models/User.php",
            `<?php class User extends Model { protected $table = 'app_users'; }`,
        );
        await write(
            "app/Filament/Resources/UserResource.php",
            `<?php
            class UserResource extends Resource {
                protected static ?string $model = User::class;
                protected static ?string $modelLabel = 'Account';
            }`,
        );

        const eloquent = await scanEloquentModels(tmp);
        expect(eloquent.classToEntity.get("User")).toBe("app_users");

        const { scanFilamentResources } = await import("./filament.js");
        const filament = await scanFilamentResources(tmp, eloquent.classToEntity);
        const account = filament.fragments.find((f) => f.term === "account");
        expect(account?.canonical.kind === "entity" && account.canonical.entity).toBe(
            "app_users",
        );
    });
});
