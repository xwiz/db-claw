import { describe, expect, it } from "vitest";
import { mergeFragments } from "./merge.js";
import { SourceLayer, type VocabFragment } from "./types.js";

function frag(
    term: string,
    entity: string,
    layer: SourceLayer,
    file = "x.php",
    line = 1,
): VocabFragment {
    return {
        term,
        canonical: { kind: "entity", entity },
        confidence: 1,
        locator: { file, line, layer, extractor: "test" },
    };
}

describe("mergeFragments", () => {
    it("higher layer wins, lower layers go to superseded", () => {
        const out = mergeFragments([
            frag("Students", "users", SourceLayer.DbSchema),
            frag("Students", "users", SourceLayer.FormOrTableLabel),
            frag("Students", "users", SourceLayer.I18n),
        ]);
        expect(out.entries.length).toBe(1);
        expect(out.entries[0]?.locator.layer).toBe(SourceLayer.FormOrTableLabel);
        expect(out.entries[0]?.superseded.length).toBe(2);
    });

    it("flags conflicts when two layer-tied candidates disagree", () => {
        const out = mergeFragments([
            frag("Org", "tenants", SourceLayer.FormOrTableLabel, "a.tsx", 10),
            frag("Org", "users", SourceLayer.FormOrTableLabel, "b.tsx", 20),
        ]);
        expect(out.conflicts.length).toBe(1);
        expect(out.conflicts[0]?.candidates.length).toBe(2);
    });

    it("does not flag conflict when layers differ", () => {
        const out = mergeFragments([
            frag("Org", "tenants", SourceLayer.FormOrTableLabel),
            frag("Org", "users", SourceLayer.DbSchema),
        ]);
        expect(out.conflicts.length).toBe(0);
        expect(out.entries.length).toBe(2);
    });

    it("tie-breaks by confidence at the same layer", () => {
        // Two extractors both emit Orm-layer fragments for the same
        // (term, canonical) pair — Drizzle is more confident than
        // Pinia, so Drizzle's locator must win.
        const drizzle: VocabFragment = {
            term: "email",
            canonical: { kind: "field", field: "users.email" },
            confidence: 0.7,
            locator: {
                file: "drizzle.ts",
                line: 10,
                layer: SourceLayer.Orm,
                extractor: "drizzle",
            },
        };
        const pinia: VocabFragment = {
            term: "email",
            canonical: { kind: "field", field: "users.email" },
            confidence: 0.5,
            locator: {
                file: "pinia.ts",
                line: 20,
                layer: SourceLayer.Orm,
                extractor: "pinia",
            },
        };
        const out = mergeFragments([pinia, drizzle]);
        expect(out.entries.length).toBe(1);
        expect(out.entries[0]?.locator.extractor).toBe("drizzle");
        expect(out.entries[0]?.confidence).toBe(0.7);
        expect(out.entries[0]?.superseded[0]?.extractor).toBe("pinia");
    });

    it("tie-breaks deterministically by file/line when layer + confidence tie", () => {
        // Same layer, same confidence — must still pick the same
        // winner across runs. We sort by file ASC, line ASC.
        const a: VocabFragment = {
            term: "x",
            canonical: { kind: "entity", entity: "users" },
            confidence: 1,
            locator: {
                file: "a.ts",
                line: 5,
                layer: SourceLayer.Orm,
                extractor: "x",
            },
        };
        const b: VocabFragment = {
            term: "x",
            canonical: { kind: "entity", entity: "users" },
            confidence: 1,
            locator: {
                file: "b.ts",
                line: 1,
                layer: SourceLayer.Orm,
                extractor: "y",
            },
        };
        // Ordering of inputs should not matter.
        const out1 = mergeFragments([a, b]);
        const out2 = mergeFragments([b, a]);
        expect(out1.entries[0]?.locator.file).toBe("a.ts");
        expect(out2.entries[0]?.locator.file).toBe("a.ts");
    });

    it("merges Rails enum + locale fragments for the same enum_value", () => {
        const ormEnum: VocabFragment = {
            term: "archived",
            canonical: {
                kind: "enum_value",
                enumName: "users.status",
                rawValue: "1",
            },
            confidence: 0.85,
            locator: {
                file: "user.rb",
                line: 3,
                layer: SourceLayer.Orm,
                extractor: "rails:enums",
            },
        };
        const i18nLabel: VocabFragment = {
            term: "archived",
            canonical: {
                kind: "enum_value",
                enumName: "users.status",
                rawValue: "1",
            },
            confidence: 0.9,
            locator: {
                file: "en.yml",
                line: 7,
                layer: SourceLayer.I18n,
                extractor: "rails:locales",
            },
        };
        const out = mergeFragments([ormEnum, i18nLabel]);
        expect(out.entries.length).toBe(1);
        // I18n layer (5) outranks Orm layer (2).
        expect(out.entries[0]?.locator.layer).toBe(SourceLayer.I18n);
        expect(out.entries[0]?.superseded.length).toBe(1);
    });

    it("does not collapse different rawValue under the same enum name", () => {
        // 'archived' might map to "1" in one walker (integer-backed)
        // and "archived" in another (string-backed migration). These
        // are DIFFERENT canonical targets and must NOT collapse.
        const intBacked: VocabFragment = {
            term: "archived",
            canonical: {
                kind: "enum_value",
                enumName: "users.status",
                rawValue: "1",
            },
            confidence: 0.85,
            locator: {
                file: "a.rb",
                line: 1,
                layer: SourceLayer.Orm,
                extractor: "x",
            },
        };
        const stringBacked: VocabFragment = {
            term: "archived",
            canonical: {
                kind: "enum_value",
                enumName: "users.status",
                rawValue: "archived",
            },
            confidence: 0.85,
            locator: {
                file: "b.rb",
                line: 1,
                layer: SourceLayer.Orm,
                extractor: "y",
            },
        };
        const out = mergeFragments([intBacked, stringBacked]);
        expect(out.entries.length).toBe(2);
        // Same term, two distinct canonical targets at the same
        // layer → conflict.
        expect(out.conflicts.length).toBe(1);
    });
});
