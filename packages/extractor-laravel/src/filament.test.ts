import { mkdtemp, rm, mkdir, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import {
    extractMakeLabelPairsRaw,
    modelClassToEntityCanonical,
    parseResourceProperties,
    scanFilamentResources,
} from "./filament.js";
import type { LangIndex } from "./lang.js";

let tmp: string;

beforeEach(async () => {
    tmp = await mkdtemp(path.join(tmpdir(), "semsql-fil-"));
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

describe("modelClassToEntityCanonical", () => {
    it.each([
        ["User", "users"],
        ["OrderItem", "order_items"],
        ["App\\Models\\User", "users"],
        ["App\\Models\\Person", "people"],
        ["Category", "categories"],
        ["Box", "boxes"],
        ["Leaf", "leaves"],
        // Laravel `Str::snake` parity for ALL_CAPS prefixes — every char
        // before an uppercase gets a `_` suffix.
        ["URLPath", "u_r_l_paths"],
        ["APIKey", "a_p_i_keys"],
        // Conservative pluralisation — words ending in -f/-fe that do NOT
        // take -ves stay -fs / -fes.
        ["Chief", "chiefs"],
        ["Roof", "roofs"],
        ["Belief", "beliefs"],
        // Latin / unchanged plurals.
        ["Datum", "data"],
        ["Criterion", "criteria"],
        ["Sheep", "sheep"],
        ["Series", "series"],
    ])("maps %s → %s", (cls, expected) => {
        expect(modelClassToEntityCanonical(cls)).toBe(expected);
    });
});

describe("parseResourceProperties", () => {
    it("captures all four properties when present", () => {
        const text = `<?php
            namespace App\\Filament\\Resources;
            class StudentResource extends Resource {
                protected static ?string $model = User::class;
                protected static ?string $modelLabel = 'Student';
                protected static ?string $pluralModelLabel = 'Students';
                protected static ?string $navigationLabel = 'All Students';
            }
        `;
        const p = parseResourceProperties(text);
        expect(p.model).toBe("User");
        expect(p.singularLabel).toBe("Student");
        expect(p.pluralLabel).toBe("Students");
        expect(p.navLabel).toBe("All Students");
        expect(p.singularLine).toBeGreaterThan(1);
    });

    it("survives missing properties without throwing", () => {
        const text = `<?php
            class FooResource extends Resource {
                protected static ?string $model = Foo::class;
            }
        `;
        const p = parseResourceProperties(text);
        expect(p.model).toBe("Foo");
        expect(p.singularLabel).toBeUndefined();
    });

    it("captures method-form label getters (Filament v3+)", () => {
        const text = `<?php
            class StudentResource extends Resource {
                protected static ?string $model = User::class;
                public static function getModelLabel(): string {
                    return 'Student';
                }
                public static function getPluralModelLabel(): string {
                    return 'Students';
                }
                public static function getNavigationLabel(): string {
                    return 'All Students';
                }
            }
        `;
        const p = parseResourceProperties(text);
        expect(p.model).toBe("User");
        expect(p.singularLabel).toBe("Student");
        expect(p.pluralLabel).toBe("Students");
        expect(p.navLabel).toBe("All Students");
    });

    it("method form overrides static-property form", () => {
        const text = `<?php
            class StudentResource extends Resource {
                protected static ?string $model = User::class;
                protected static ?string $modelLabel = 'OldStudent';
                public static function getModelLabel(): string {
                    return 'Student';
                }
            }
        `;
        const p = parseResourceProperties(text);
        expect(p.singularLabel).toBe("Student");
    });

    it("accepts public/private static and untyped declarations", () => {
        const text = `<?php
            class StudentResource extends Resource {
                public static $model = User::class;
                private static string $modelLabel = 'Student';
            }
        `;
        const p = parseResourceProperties(text);
        expect(p.model).toBe("User");
        expect(p.singularLabel).toBe("Student");
    });
});

describe("extractMakeLabelPairsRaw", () => {
    it("captures field + label even with chained modifiers in between", () => {
        const text = `
            Forms\\Components\\TextInput::make('name')->label('Full Name')
            Forms\\Components\\Select::make('status_code')
                ->required()
                ->options([1 => 'A'])
                ->label('Status')
        `;
        const hits = extractMakeLabelPairsRaw(text);
        const got = hits.map((h) => [h.field, h.label]);
        expect(got).toEqual(
            expect.arrayContaining([
                ["name", "Full Name"],
                ["status_code", "Status"],
            ]),
        );
    });

    it("ignores ::make calls with no ->label", () => {
        const text = `
            TextInput::make('orphan')->required()
            TextInput::make('labelled')->label('Y')
        `;
        const hits = extractMakeLabelPairsRaw(text);
        expect(hits.map((h) => h.field)).toEqual(["labelled"]);
    });

    it("handles modifiers with nested parens (AST-only — regex misses these)", () => {
        const text = `
            Forms\\Components\\TextInput::make('name')
                ->visible(fn () => $auth->isAdmin())
                ->default(now())
                ->options([1 => 'A', 2 => 'B'])
                ->label('Full Name')
        `;
        const hits = extractMakeLabelPairsRaw(text);
        expect(hits.map((h) => [h.field, h.label])).toEqual([["name", "Full Name"]]);
    });

    it("rejects label() with a non-static-string argument", () => {
        const text = `
            Forms\\Components\\TextInput::make('foo')->label(__('users.foo'))
            Forms\\Components\\TextInput::make('bar')->label('Bar')
        `;
        const hits = extractMakeLabelPairsRaw(text);
        // Only the static-string label is captured; the i18n call is
        // deferred to v0.5 (key resolution against lang/).
        expect(hits.map((h) => h.field)).toEqual(["bar"]);
    });

    it("handles split-line chains across many modifiers", () => {
        const text = `
            Tables\\Columns\\TextColumn::make('email')
                ->searchable()
                ->sortable()
                ->copyable()
                ->toggleable(isToggledHiddenByDefault: true)
                ->label('Email Address')
        `;
        const hits = extractMakeLabelPairsRaw(text);
        expect(hits.map((h) => [h.field, h.label])).toEqual([
            ["email", "Email Address"],
        ]);
    });
});

describe("scanFilamentResources", () => {
    it("emits FormOrTableLabel-layer fragments for a Resource class", async () => {
        await write(
            "app/Filament/Resources/StudentResource.php",
            `<?php
            namespace App\\Filament\\Resources;
            class StudentResource extends Resource {
                protected static ?string $model = User::class;
                protected static ?string $modelLabel = 'Student';
                protected static ?string $pluralModelLabel = 'Students';
            }`,
        );
        const r = await scanFilamentResources(tmp);
        expect(r.fragments.length).toBe(2);
        for (const f of r.fragments) {
            expect(f.locator.layer).toBe(6);
            expect(f.canonical.kind).toBe("entity");
            if (f.canonical.kind === "entity") {
                expect(f.canonical.entity).toBe("users");
            }
        }
        const terms = r.fragments.map((f) => f.term).sort();
        expect(terms).toEqual(["student", "students"]);
    });

    it("returns empty for a project without a Filament directory", async () => {
        const r = await scanFilamentResources(tmp);
        expect(r.fragments).toEqual([]);
    });

    it("skips non-Resource files in the same directory", async () => {
        await write(
            "app/Filament/Resources/Helper.php",
            `<?php class Helper {}`,
        );
        const r = await scanFilamentResources(tmp);
        // File doesn't match `*Resource.php` so it's not even loaded.
        expect(r.fragments).toEqual([]);
    });

    it("skips files missing $model", async () => {
        await write(
            "app/Filament/Resources/StubResource.php",
            `<?php
            class StubResource extends Resource {
                protected static ?string $modelLabel = 'Stub';
            }`,
        );
        const r = await scanFilamentResources(tmp);
        expect(r.fragments).toEqual([]);
        expect(r.skipped.length).toBe(1);
    });

    it("emits field-level fragments for ->label() calls", async () => {
        await write(
            "app/Filament/Resources/StudentResource.php",
            `<?php
            namespace App\\Filament\\Resources;
            class StudentResource extends Resource {
                protected static ?string $model = User::class;
                protected static ?string $modelLabel = 'Student';
                public static function form(Form $form): Form {
                    return $form->schema([
                        Forms\\Components\\TextInput::make('name')->label('Full Name'),
                        Forms\\Components\\Select::make('status_code')
                            ->required()
                            ->options([1 => 'Pending', 2 => 'Active'])
                            ->label('Account Status'),
                    ]);
                }
                public static function table(Table $table): Table {
                    return $table->columns([
                        Tables\\Columns\\TextColumn::make('email')->label('Email Address'),
                    ]);
                }
            }`,
        );
        const r = await scanFilamentResources(tmp);
        const fieldFrags = r.fragments.filter((f) => f.canonical.kind === "field");
        const map = new Map(
            fieldFrags.map((f) => [
                f.term,
                f.canonical.kind === "field" ? f.canonical.field : "",
            ]),
        );
        expect(map.get("full name")).toBe("users.name");
        expect(map.get("account status")).toBe("users.status_code");
        expect(map.get("email address")).toBe("users.email");
    });

    it("rejects non-canonical labels via sanitiser", async () => {
        await write(
            "app/Filament/Resources/StudentResource.php",
            `<?php
            class StudentResource extends Resource {
                protected static ?string $model = User::class;
                protected static ?string $modelLabel = '';
            }`,
        );
        const r = await scanFilamentResources(tmp);
        // Empty label fails sanitisation; nothing emitted.
        expect(r.fragments).toEqual([]);
        expect(r.skipped.length).toBeGreaterThan(0);
    });
});

describe("scanFilamentResources — i18n-bound labels", () => {
    function indexFrom(entries: Record<string, string>): LangIndex {
        const m: LangIndex = new Map();
        for (const [k, v] of Object.entries(entries)) {
            m.set(k, { label: v, locale: "en", file: "lang/en/users.php", line: 1 });
        }
        return m;
    }

    it("resolves ->label(__('lang.key')) chains via the lang index", async () => {
        await write(
            "app/Filament/Resources/UserResource.php",
            `<?php
            namespace App\\Filament\\Resources;

            use App\\Models\\User;
            use Filament\\Resources\\Resource;

            class UserResource extends Resource {
                protected static ?string $model = User::class;

                public static function form($form) {
                    return $form->schema([
                        TextInput::make('full_name')
                            ->required()
                            ->label(__('users.full_name')),
                    ]);
                }
            }`,
        );
        const langIndex = indexFrom({ "users.full_name": "Full Name" });
        const r = await scanFilamentResources(tmp, undefined, langIndex);
        const fieldFrags = r.fragments.filter(
            (f) => f.canonical.kind === "field",
        );
        expect(fieldFrags).toHaveLength(1);
        const frag = fieldFrags[0]!;
        expect(frag.term).toBe("full name");
        expect(frag.canonical).toEqual({
            kind: "field",
            field: "users.full_name",
        });
        expect(frag.locator.layer).toBe(6); // FormOrTableLabel
        expect(frag.locator.extractor).toContain("make-label-i18n:en");
        // Confidence reflects the indirection — between layer-5 raw
        // (0.85) and layer-6 literal (0.95).
        expect(frag.confidence).toBeGreaterThan(0.85);
        expect(frag.confidence).toBeLessThan(0.95);
    });

    it("records unresolved i18n keys in skipped without crashing", async () => {
        await write(
            "app/Filament/Resources/UserResource.php",
            `<?php
            class UserResource extends Resource {
                protected static ?string $model = User::class;
                public static function form($form) {
                    return $form->schema([
                        TextInput::make('phantom')->label(__('users.does_not_exist')),
                    ]);
                }
            }`,
        );
        const r = await scanFilamentResources(
            tmp,
            undefined,
            indexFrom({ "users.full_name": "Full Name" }),
        );
        expect(r.fragments.filter((f) => f.canonical.kind === "field")).toEqual([]);
        expect(
            r.skipped.some((s) =>
                s.reason.includes("i18n key not in lang index: users.does_not_exist"),
            ),
        ).toBe(true);
    });

    it("does nothing when no lang index supplied (back-compat)", async () => {
        await write(
            "app/Filament/Resources/UserResource.php",
            `<?php
            class UserResource extends Resource {
                protected static ?string $model = User::class;
                public static function form($form) {
                    return $form->schema([
                        TextInput::make('full_name')->label(__('users.full_name')),
                    ]);
                }
            }`,
        );
        // No third arg → i18n pass skipped entirely.
        const r = await scanFilamentResources(tmp);
        expect(r.fragments.filter((f) => f.canonical.kind === "field")).toEqual([]);
    });
});
