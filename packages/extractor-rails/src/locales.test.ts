import { mkdtemp, mkdir, writeFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { scanLocales } from "./locales.js";

let tmp: string;

beforeEach(async () => {
    tmp = await mkdtemp(path.join(tmpdir(), "semsql-rails-locales-"));
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

const EN_YML = `
en:
  activerecord:
    models:
      user: "User"
      user_account:
        one: "User account"
        other: "User accounts"
    attributes:
      user:
        email: "Email Address"
        is_active: "Active"
        tenant_id: "Tenant"
  helpers:
    label:
      user:
        email: "Your email"
`;

const FR_YML = `
fr:
  activerecord:
    models:
      user: "Utilisateur"
    attributes:
      user:
        email: "Adresse e-mail"
`;

describe("scanLocales", () => {
    it("extracts entity labels from activerecord.models", async () => {
        await write("config/locales/en.yml", EN_YML);
        const r = await scanLocales(tmp);
        const userEnt = r.fragments.find(
            (f) =>
                f.canonical.kind === "entity" &&
                f.canonical.entity === "user" &&
                f.term === "user",
        );
        expect(userEnt).toBeDefined();
        expect(userEnt?.locator.layer).toBe(5); // UserSurface
    });

    it("emits both singular + plural forms for plural-map entity labels", async () => {
        await write("config/locales/en.yml", EN_YML);
        const r = await scanLocales(tmp);
        const accountTerms = r.fragments
            .filter(
                (f) =>
                    f.canonical.kind === "entity" &&
                    f.canonical.entity === "user_account",
            )
            .map((f) => f.term)
            .sort();
        expect(accountTerms).toContain("user account");
        expect(accountTerms).toContain("user accounts");
    });

    it("extracts field labels from activerecord.attributes", async () => {
        await write("config/locales/en.yml", EN_YML);
        const r = await scanLocales(tmp);
        const emailFrag = r.fragments.find(
            (f) =>
                f.canonical.kind === "field" &&
                f.canonical.field === "user.email" &&
                f.term === "email address",
        );
        expect(emailFrag).toBeDefined();
        expect(emailFrag?.locator.layer).toBe(5);
        expect(emailFrag?.locator.extractor).toBe(
            "extractor-rails:locales:activerecord",
        );
    });

    it("emits helpers.label entries at FormOrTableLabel layer with higher confidence", async () => {
        await write("config/locales/en.yml", EN_YML);
        const r = await scanLocales(tmp);
        const helperFrag = r.fragments.find(
            (f) =>
                f.canonical.kind === "field" &&
                f.canonical.field === "user.email" &&
                f.term === "your email",
        );
        expect(helperFrag).toBeDefined();
        expect(helperFrag?.locator.layer).toBe(6);
        expect(helperFrag?.confidence).toBe(0.95);
    });

    it("ignores non-preferred locales by default", async () => {
        await write("config/locales/en.yml", EN_YML);
        await write("config/locales/fr.yml", FR_YML);
        const r = await scanLocales(tmp);
        const fr = r.fragments.find((f) => f.term === "utilisateur");
        expect(fr).toBeUndefined();
    });

    it("respects preferredLocale override", async () => {
        await write("config/locales/fr.yml", FR_YML);
        const r = await scanLocales(tmp, { preferredLocale: "fr" });
        const utilisateur = r.fragments.find((f) => f.term === "utilisateur");
        expect(utilisateur).toBeDefined();
    });

    it("strips %{interpolation} placeholders from field labels", async () => {
        await write(
            "config/locales/en.yml",
            `
en:
  activerecord:
    attributes:
      user:
        balance: "Balance %{currency}"
`,
        );
        const r = await scanLocales(tmp);
        const balance = r.fragments.find(
            (f) =>
                f.canonical.kind === "field" &&
                f.canonical.field === "user.balance",
        );
        expect(balance?.term).toBe("balance");
    });

    it("walks nested locale directories", async () => {
        await write(
            "config/locales/admin/en.yml",
            `
en:
  activerecord:
    attributes:
      role:
        name: "Role Name"
`,
        );
        const r = await scanLocales(tmp);
        expect(r.fragments.length).toBeGreaterThan(0);
        const roleFrag = r.fragments.find(
            (f) =>
                f.canonical.kind === "field" && f.canonical.field === "role.name",
        );
        expect(roleFrag).toBeDefined();
    });

    it("ignores non-AR / non-helpers keys (errors, simple_form, etc.)", async () => {
        await write(
            "config/locales/en.yml",
            `
en:
  errors:
    messages:
      blank: "can't be blank"
  simple_form:
    placeholders:
      user:
        email: "Type your email"
`,
        );
        const r = await scanLocales(tmp);
        expect(r.fragments).toEqual([]);
    });

    it("returns empty when config/locales is missing", async () => {
        const r = await scanLocales(tmp);
        expect(r.fragments).toEqual([]);
        expect(r.skipped).toEqual([]);
    });

    it("logs malformed YAML to skipped without crashing", async () => {
        await write("config/locales/bad.yml", "en:\n  : : :\n  oops");
        const r = await scanLocales(tmp);
        expect(r.fragments).toEqual([]);
        expect(r.skipped.length).toBeGreaterThan(0);
        expect(r.skipped[0]?.reason).toContain("yaml parse");
    });
});

describe("scanLocales — lang index", () => {
    it("populates the index with locale-stripped Rails I18n keys", async () => {
        await write(
            "config/locales/en.yml",
            `en:
  activerecord:
    models:
      user: "User"
    attributes:
      user:
        email: "Email Address"
        is_active: "Active"
  helpers:
    label:
      user:
        email: "Your email"
`,
        );
        const r = await scanLocales(tmp);
        expect(r.index.get("activerecord.models.user")?.label).toBe("User");
        expect(
            r.index.get("activerecord.attributes.user.email")?.label,
        ).toBe("Email Address");
        expect(r.index.get("helpers.label.user.email")?.label).toBe(
            "Your email",
        );
        expect(r.index.get("activerecord.models.user")?.locale).toBe("en");
    });

    it("prefers `en` over other locales on conflicts", async () => {
        await write(
            "config/locales/fr.yml",
            `fr:
  activerecord:
    models:
      user: "Utilisateur"
`,
        );
        await write(
            "config/locales/en.yml",
            `en:
  activerecord:
    models:
      user: "User"
`,
        );
        const r = await scanLocales(tmp);
        const entry = r.index.get("activerecord.models.user");
        expect(entry?.label).toBe("User");
        expect(entry?.locale).toBe("en");
    });

    it("respects an alternative `preferredLocale`", async () => {
        await write(
            "config/locales/en.yml",
            `en:
  activerecord:
    models:
      user: "User"
`,
        );
        await write(
            "config/locales/de.yml",
            `de:
  activerecord:
    models:
      user: "Benutzer"
`,
        );
        const r = await scanLocales(tmp, { preferredLocale: "de" });
        const entry = r.index.get("activerecord.models.user");
        expect(entry?.label).toBe("Benutzer");
        expect(entry?.locale).toBe("de");
    });

    it("indexes scalars (numbers/booleans) as strings", async () => {
        await write(
            "config/locales/en.yml",
            `en:
  pagination:
    items_per_page: 25
    show_borders: true
`,
        );
        const r = await scanLocales(tmp);
        expect(r.index.get("pagination.items_per_page")?.label).toBe("25");
        expect(r.index.get("pagination.show_borders")?.label).toBe("true");
    });

    it("indexes deeply nested keys", async () => {
        await write(
            "config/locales/en.yml",
            `en:
  views:
    pages:
      home:
        title: "Home"
        subtitle: "Welcome"
`,
        );
        const r = await scanLocales(tmp);
        expect(r.index.get("views.pages.home.title")?.label).toBe("Home");
        expect(r.index.get("views.pages.home.subtitle")?.label).toBe(
            "Welcome",
        );
    });
});
