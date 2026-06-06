import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { scanVueLocales } from "./lang.js";

let tmp: string;

beforeEach(async () => {
	tmp = await mkdtemp(path.join(tmpdir(), "semsql-vlang-"));
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

describe("scanVueLocales", () => {
	it("indexes flat src/locales/<locale>.json files", async () => {
		await write(
			"src/locales/en.json",
			JSON.stringify({
				"users.full_name": "Full Name",
				"billing.balance": "Account Balance",
			}),
		);
		const r = await scanVueLocales(tmp);
		expect(r.index.get("users.full_name")?.label).toBe("Full Name");
		expect(r.index.get("billing.balance")?.label).toBe("Account Balance");
		expect(r.index.get("users.full_name")?.locale).toBe("en");
	});

	it("indexes nested locales/<locale>/<group>.json files with both raw and prefixed shapes", async () => {
		await write(
			"locales/en/users.json",
			JSON.stringify({
				full_name: "Full Name",
				email_label: "Email Address",
			}),
		);
		const r = await scanVueLocales(tmp);
		// Both shapes resolved — vue-i18n callers may use either.
		expect(r.index.get("full_name")?.label).toBe("Full Name");
		expect(r.index.get("users.full_name")?.label).toBe("Full Name");
	});

	it("walks nested objects to dotted keys", async () => {
		await write(
			"src/locales/en.json",
			JSON.stringify({
				users: {
					name: { first: "First Name", last: "Last Name" },
				},
			}),
		);
		const r = await scanVueLocales(tmp);
		expect(r.index.get("users.name.first")?.label).toBe("First Name");
		expect(r.index.get("users.name.last")?.label).toBe("Last Name");
	});

	it("prefers `en` on locale conflicts", async () => {
		await write(
			"src/locales/fr.json",
			JSON.stringify({ "users.full_name": "Nom Complet" }),
		);
		await write(
			"src/locales/en.json",
			JSON.stringify({ "users.full_name": "Full Name" }),
		);
		const r = await scanVueLocales(tmp);
		const entry = r.index.get("users.full_name");
		expect(entry?.label).toBe("Full Name");
		expect(entry?.locale).toBe("en");
	});

	it("records malformed JSON in skipped without crashing", async () => {
		await write("src/locales/en.json", "{ not valid json");
		const r = await scanVueLocales(tmp);
		expect(r.skipped.length).toBe(1);
		expect(r.skipped[0]!.reason).toContain("invalid JSON");
	});

	it("returns an empty index when no locales root exists", async () => {
		const r = await scanVueLocales(tmp);
		expect(r.index.size).toBe(0);
		expect(r.skipped).toEqual([]);
	});

	it("indexes deeply-nested-directory JSON groups under the path-derived prefix", async () => {
		// `locales/en/auth/login.json` should resolve as `auth.login.<key>`.
		await write(
			"src/locales/en/auth/login.json",
			JSON.stringify({ title: "Sign In", subtitle: "Welcome back" }),
		);
		const r = await scanVueLocales(tmp);
		expect(r.index.get("auth.login.title")?.label).toBe("Sign In");
		expect(r.index.get("auth.login.subtitle")?.label).toBe("Welcome back");
		// Must not collapse to bare `login.title` — that's the
		// pre-fix bug class the regression test catches.
		expect(r.index.get("login.title")).toBeUndefined();
	});

	it("recognises i18n/ and lang/ root variants", async () => {
		await write("i18n/en.json", JSON.stringify({ "users.email": "Email" }));
		await write("lang/en.json", JSON.stringify({ "users.balance": "Balance" }));
		const r = await scanVueLocales(tmp);
		expect(r.index.get("users.email")?.label).toBe("Email");
		expect(r.index.get("users.balance")?.label).toBe("Balance");
	});
});
