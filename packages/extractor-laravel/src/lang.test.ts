import { mkdtemp, rm, mkdir, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { scanLangDir } from "./lang.js";

let tmp: string;

beforeEach(async () => {
    tmp = await mkdtemp(path.join(tmpdir(), "semsql-lang-"));
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

describe("scanLangDir", () => {
    it("parses flat PHP key/value pairs at the top level", async () => {
        await write(
            "en/models.php",
            `<?php
            return [
                'user' => 'Student',
                'tenant' => 'Organization',
            ];`,
        );
        const r = await scanLangDir(tmp);
        const targets = r.fragments.map((f) => ({
            term: f.term,
            entity:
                f.canonical.kind === "entity"
                    ? f.canonical.entity
                    : "<not-entity>",
        }));
        expect(targets).toEqual(
            expect.arrayContaining([
                { term: "student", entity: "user" },
                { term: "organization", entity: "tenant" },
            ]),
        );
    });

    it("parses nested 'models' arrays with singular/plural keys", async () => {
        await write(
            "en/models.php",
            `<?php
            return [
                'models' => [
                    'user' => [
                        'singular' => 'Student',
                        'plural' => 'Students',
                    ],
                ],
            ];`,
        );
        const r = await scanLangDir(tmp);
        const userFrags = r.fragments.filter(
            (f) => f.canonical.kind === "entity" && f.canonical.entity === "user",
        );
        expect(userFrags.length).toBe(2);
        expect(userFrags.map((f) => f.term)).toEqual(
            expect.arrayContaining(["student", "students"]),
        );
    });

    it("parses lang/<locale>.json files", async () => {
        await write(
            "en.json",
            JSON.stringify({ models: { user: { singular: "Student", plural: "Students" } } }),
        );
        const r = await scanLangDir(tmp);
        const term = r.fragments.find(
            (f) => f.term === "students" && f.canonical.kind === "entity",
        );
        expect(term).toBeDefined();
        expect(term?.locator.extractor).toContain("json");
    });

    it("rejects keys that fail canonical sanitisation", async () => {
        await write(
            "en/models.php",
            `<?php
            return [
                'bad-name' => 'Anything',
                'good_name' => 'Student',
            ];`,
        );
        const r = await scanLangDir(tmp);
        const goodCount = r.fragments.filter(
            (f) =>
                f.canonical.kind === "entity" && f.canonical.entity === "good_name",
        ).length;
        expect(goodCount).toBe(1);
        expect(r.skipped.some((s) => s.reason.includes("invalid canonical"))).toBe(
            true,
        );
    });

    it("strips PHP comments before parsing", async () => {
        await write(
            "en/models.php",
            `<?php
            // single-line
            /* multi
               line */
            return [
                # hash
                'user' => 'Student',
            ];`,
        );
        const r = await scanLangDir(tmp);
        expect(r.fragments.length).toBe(1);
    });

    it("returns empty for missing dir without throwing", async () => {
        const r = await scanLangDir(path.join(tmp, "no-such-dir"));
        expect(r.fragments).toEqual([]);
        expect(r.skipped).toEqual([]);
    });

    it("captures provenance: file, line, locale, extractor name", async () => {
        const file = await write(
            "fr/models.php",
            `<?php
            return [
                'user' => 'Étudiant',
            ];`,
        );
        const r = await scanLangDir(tmp);
        expect(r.fragments.length).toBe(1);
        const frag = r.fragments[0]!;
        expect(frag.locator.file).toBe(file);
        expect(frag.locator.line).toBeGreaterThan(0);
        expect(frag.locator.extractor).toContain("fr");
        expect(frag.locator.layer).toBe(5); // I18n
    });
});
