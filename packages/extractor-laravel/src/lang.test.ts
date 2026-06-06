import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
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
				f.canonical.kind === "entity" ? f.canonical.entity : "<not-entity>",
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
			JSON.stringify({
				models: { user: { singular: "Student", plural: "Students" } },
			}),
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

describe("scanLangDir — lang index", () => {
	it("populates the index with PHP group-prefixed keys", async () => {
		await write(
			"en/users.php",
			`<?php
            return [
                'full_name' => 'Full Name',
                'email_label' => 'Email Address',
            ];`,
		);
		const r = await scanLangDir(tmp);
		// Group is the file basename — Laravel's __() helper resolves
		// `users.full_name` against `lang/en/users.php`.
		expect(r.index.get("users.full_name")?.label).toBe("Full Name");
		expect(r.index.get("users.email_label")?.label).toBe("Email Address");
		expect(r.index.get("users.full_name")?.locale).toBe("en");
	});

	it("populates the index from flat JSON (no group prefix)", async () => {
		await write(
			"en.json",
			JSON.stringify({
				"users.full_name": "Full Name",
				"billing.balance": "Account Balance",
			}),
		);
		const r = await scanLangDir(tmp);
		expect(r.index.get("users.full_name")?.label).toBe("Full Name");
		expect(r.index.get("billing.balance")?.label).toBe("Account Balance");
	});

	it("prefers the `en` locale on conflicts", async () => {
		await write("fr/users.php", `<?php return ['full_name' => 'Nom Complet'];`);
		await write("en/users.php", `<?php return ['full_name' => 'Full Name'];`);
		const r = await scanLangDir(tmp);
		const entry = r.index.get("users.full_name");
		expect(entry?.label).toBe("Full Name");
		expect(entry?.locale).toBe("en");
	});

	it("first-write-wins when no `en` locale present", async () => {
		await write(
			"de/users.php",
			`<?php return ['full_name' => 'Vollständiger Name'];`,
		);
		await write("fr/users.php", `<?php return ['full_name' => 'Nom Complet'];`);
		const r = await scanLangDir(tmp);
		const entry = r.index.get("users.full_name");
		expect(entry).toBeDefined();
		// Either fr or de wins depending on filesystem iteration order;
		// the contract is "first write wins, en overrides", so we
		// accept whichever non-en label arrived first.
		expect(["Nom Complet", "Vollständiger Name"]).toContain(entry!.label);
		expect(entry!.locale).not.toBe("en");
	});

	it("indexes nested-directory PHP files using the path-derived group prefix", async () => {
		// Laravel resolves `auth.passwords.reset` via `lang/<locale>/auth/passwords.php`.
		// The walker must include the subdirectory in the index key.
		await write(
			"en/auth/passwords.php",
			`<?php
            return [
                'reset' => 'Reset Password',
                'sent'  => 'Reset Link Sent',
            ];`,
		);
		const r = await scanLangDir(tmp);
		expect(r.index.get("auth.passwords.reset")?.label).toBe("Reset Password");
		expect(r.index.get("auth.passwords.sent")?.label).toBe("Reset Link Sent");
		// Must NOT collapse to bare `passwords.reset` — that's the
		// pre-fix bug class the regression test catches.
		expect(r.index.get("passwords.reset")).toBeUndefined();
	});

	it("indexes deeply nested directories — auth/api/tokens.php", async () => {
		await write(
			"en/auth/api/tokens.php",
			`<?php return ['expired' => 'Token Expired'];`,
		);
		const r = await scanLangDir(tmp);
		expect(r.index.get("auth.api.tokens.expired")?.label).toBe("Token Expired");
	});

	it("records nested PHP keys under their group prefix", async () => {
		await write(
			"en/users.php",
			`<?php
            return [
                'name' => [
                    'first' => 'First Name',
                    'last' => 'Last Name',
                ],
            ];`,
		);
		const r = await scanLangDir(tmp);
		expect(r.index.get("users.name.first")?.label).toBe("First Name");
		expect(r.index.get("users.name.last")?.label).toBe("Last Name");
	});
});
