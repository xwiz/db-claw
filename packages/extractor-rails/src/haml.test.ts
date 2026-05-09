import { mkdtemp, mkdir, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import type { LangIndex, LangIndexEntry } from "@semsql/extractor-sdk";
import { scanHaml } from "./haml.js";

let tmp: string;

beforeEach(async () => {
    tmp = await mkdtemp(path.join(tmpdir(), "semsql-haml-"));
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

function indexFrom(entries: Record<string, string>): LangIndex {
    const m: LangIndex = new Map();
    for (const [k, v] of Object.entries(entries)) {
        const e: LangIndexEntry = {
            label: v,
            locale: "en",
            file: "config/locales/en.yml",
            line: 1,
        };
        m.set(k, e);
    }
    return m;
}

describe("scanHaml — inline static labels", () => {
    it("captures `%label{ for: \"user_email\" } Email Address`", async () => {
        await write(
            "app/views/users/_form.haml",
            `%label{ for: "user_email" } Email Address`,
        );
        const r = await scanHaml(tmp);
        expect(r.fragments).toHaveLength(1);
        const frag = r.fragments[0]!;
        expect(frag.term).toBe("email address");
        expect(frag.canonical).toEqual({
            kind: "field",
            field: "user.email",
        });
        expect(frag.confidence).toBe(0.95);
        expect(frag.locator.layer).toBe(6);
    });

    it("strips trailing colon from inline labels", async () => {
        await write(
            "app/views/users/_form.haml",
            `%label{ for: "user_email" } Email Address:`,
        );
        const r = await scanHaml(tmp);
        expect(r.fragments[0]!.term).toBe("email address");
    });

    it("captures multiple labels in one Haml file", async () => {
        await write(
            "app/views/users/_form.haml",
            `%label{ for: "user_email" } Email
%label{ for: "user_full_name" } Full Name
%label{ for: "user_balance" } Balance`,
        );
        const r = await scanHaml(tmp);
        expect(r.fragments.map((f) => f.canonical)).toEqual([
            { kind: "field", field: "user.email" },
            { kind: "field", field: "user.full_name" },
            { kind: "field", field: "user.balance" },
        ]);
    });

    it("accepts the legacy hash-rocket attribute syntax", async () => {
        await write(
            "app/views/users/_form.haml",
            `%label{ "for" => "user_email" } Email`,
        );
        const r = await scanHaml(tmp);
        expect(r.fragments).toHaveLength(1);
        expect(r.fragments[0]!.canonical).toEqual({
            kind: "field",
            field: "user.email",
        });
    });

    it("captures `%label.required{...}` class shorthand", async () => {
        await write(
            "app/views/users/_form.haml",
            `%label.required{ for: "user_email" } Email`,
        );
        const r = await scanHaml(tmp);
        expect(r.fragments).toHaveLength(1);
    });
});

describe("scanHaml — inline ruby expression", () => {
    it("resolves inline `}= t('key')` via the lang index", async () => {
        await write(
            "app/views/users/_form.haml",
            `%label{ for: "user_email" }= t('users.email_label')`,
        );
        const langIndex = indexFrom({
            "users.email_label": "Email Address",
        });
        const r = await scanHaml(tmp, { langIndex });
        expect(r.fragments).toHaveLength(1);
        const frag = r.fragments[0]!;
        expect(frag.term).toBe("email address");
        expect(frag.confidence).toBe(0.92);
        expect(frag.locator.extractor).toContain("haml:label-i18n:en");
    });

    it("drops inline `}= dynamic_expression` when no t() call is found", async () => {
        await write(
            "app/views/users/_form.haml",
            `%label{ for: "user_email" }= some_helper_call`,
        );
        const r = await scanHaml(tmp);
        expect(r.fragments).toEqual([]);
    });
});

describe("scanHaml — block-form continuation", () => {
    it("captures static text on the next indented line", async () => {
        await write(
            "app/views/users/_form.haml",
            `%label{ for: "user_email" }
  Email Address`,
        );
        const r = await scanHaml(tmp);
        expect(r.fragments).toHaveLength(1);
        expect(r.fragments[0]!.term).toBe("email address");
    });

    it("captures `= t('key')` on the next indented line", async () => {
        await write(
            "app/views/users/_form.haml",
            `%label{ for: "user_email" }
  = t('users.email_label')`,
        );
        const langIndex = indexFrom({ "users.email_label": "Email Address" });
        const r = await scanHaml(tmp, { langIndex });
        expect(r.fragments).toHaveLength(1);
        expect(r.fragments[0]!.term).toBe("email address");
        expect(r.fragments[0]!.confidence).toBe(0.92);
    });

    it("ignores continuation lines that aren't indented past the label", async () => {
        await write(
            "app/views/users/_form.haml",
            `%label{ for: "user_email" }
Email Address`,
        );
        const r = await scanHaml(tmp);
        expect(r.fragments).toEqual([]);
    });

    it("only consumes the FIRST contentful continuation line", async () => {
        await write(
            "app/views/users/_form.haml",
            `%label{ for: "user_email" }
  First
  Second`,
        );
        const r = await scanHaml(tmp);
        expect(r.fragments).toHaveLength(1);
        expect(r.fragments[0]!.term).toBe("first");
    });
});

describe("scanHaml — guards + dialect tolerance", () => {
    it("ignores non-label tags", async () => {
        await write(
            "app/views/users/_form.haml",
            `%div{ for: "user_email" } Not a label`,
        );
        const r = await scanHaml(tmp);
        expect(r.fragments).toEqual([]);
    });

    it("ignores `for` attributes that don't match entity_field shape", async () => {
        await write(
            "app/views/users/_form.haml",
            `%label{ for: "anything-with-dashes" } Anything
%label{ for: "UserEmail" } CamelCase`,
        );
        const r = await scanHaml(tmp);
        expect(r.fragments).toEqual([]);
    });

    it("walks nested view directories", async () => {
        await write(
            "app/views/admin/users/edit.haml",
            `%label{ for: "user_email" } Email`,
        );
        const r = await scanHaml(tmp);
        expect(r.fragments).toHaveLength(1);
        expect(r.fragments[0]!.locator.file).toContain("admin");
    });

    it("ignores .erb / .slim files in the same directory", async () => {
        await write(
            "app/views/users/_form.html.erb",
            `<label for="user_email">Email</label>`,
        );
        await write(
            "app/views/users/_form.slim",
            `label for="user_email" Email`,
        );
        await write(
            "app/views/users/_form.haml",
            `%label{ for: "user_email" } Email`,
        );
        const r = await scanHaml(tmp);
        // Only the .haml file produces a fragment.
        expect(r.fragments).toHaveLength(1);
        expect(r.fragments[0]!.locator.file).toContain(".haml");
    });

    it("returns empty when app/views is missing", async () => {
        const r = await scanHaml(tmp);
        expect(r.fragments).toEqual([]);
    });
});
