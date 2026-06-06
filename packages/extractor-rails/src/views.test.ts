import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import type { LangIndex, LangIndexEntry } from "@semsql/extractor-sdk";
import { inferEntityFromPath, scanViews } from "./views.js";

let tmp: string;

beforeEach(async () => {
	tmp = await mkdtemp(path.join(tmpdir(), "semsql-erb-"));
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
		const entry: LangIndexEntry = {
			label: v,
			locale: "en",
			file: "config/locales/en.yml",
			line: 1,
		};
		m.set(k, entry);
	}
	return m;
}

describe("scanViews — static labels", () => {
	it('emits a field fragment for <label for="user_email">Email Address</label>', async () => {
		await write(
			"app/views/users/_form.html.erb",
			`<label for="user_email">Email Address</label>`,
		);
		const r = await scanViews(tmp);
		expect(r.fragments).toHaveLength(1);
		const frag = r.fragments[0]!;
		expect(frag.term).toBe("email address");
		expect(frag.canonical).toEqual({
			kind: "field",
			field: "user.email",
		});
		expect(frag.confidence).toBe(0.95);
		expect(frag.locator.layer).toBe(6); // FormOrTableLabel
		expect(frag.locator.extractor).toContain("views:label");
	});

	it("strips trailing colon from labels", async () => {
		await write(
			"app/views/users/_form.html.erb",
			`<label for="user_email">Email Address:</label>`,
		);
		const r = await scanViews(tmp);
		expect(r.fragments[0]!.term).toBe("email address");
	});

	it("captures multiple labels in one file", async () => {
		await write(
			"app/views/users/_form.html.erb",
			`<label for="user_email">Email</label>
             <label for="user_full_name">Full Name</label>
             <label for="user_balance">Balance</label>`,
		);
		const r = await scanViews(tmp);
		expect(r.fragments.map((f) => f.canonical)).toEqual([
			{ kind: "field", field: "user.email" },
			{ kind: "field", field: "user.full_name" },
			{ kind: "field", field: "user.balance" },
		]);
	});

	it("ignores labels without a `for` attribute", async () => {
		await write(
			"app/views/users/_form.html.erb",
			`<label>just a label</label>`,
		);
		const r = await scanViews(tmp);
		expect(r.fragments).toEqual([]);
	});

	it("ignores `for` attributes that don't match entity_field shape", async () => {
		await write(
			"app/views/users/_form.html.erb",
			`<label for="some-arbitrary-id">…</label>
             <label for="2bad_id">…</label>
             <label for="UserEmail">…</label>`,
		);
		const r = await scanViews(tmp);
		expect(r.fragments).toEqual([]);
	});

	it("returns empty when app/views is missing", async () => {
		const r = await scanViews(tmp);
		expect(r.fragments).toEqual([]);
	});
});

describe("scanViews — i18n binding via LangIndex", () => {
	it("resolves <%= t('key') %> labels through the supplied index", async () => {
		await write(
			"app/views/users/_form.html.erb",
			`<label for="user_email"><%= t('activerecord.attributes.user.email') %></label>`,
		);
		const langIndex = indexFrom({
			"activerecord.attributes.user.email": "Email Address",
		});
		const r = await scanViews(tmp, { langIndex });
		expect(r.fragments).toHaveLength(1);
		const frag = r.fragments[0]!;
		expect(frag.term).toBe("email address");
		expect(frag.canonical).toEqual({
			kind: "field",
			field: "user.email",
		});
		expect(frag.confidence).toBe(0.92);
		expect(frag.locator.extractor).toContain("views:label-i18n:en");
	});

	it("accepts the I18n.t() qualified form", async () => {
		await write(
			"app/views/users/_form.html.erb",
			`<label for="user_email"><%= I18n.t("activerecord.attributes.user.email") %></label>`,
		);
		const langIndex = indexFrom({
			"activerecord.attributes.user.email": "Email",
		});
		const r = await scanViews(tmp, { langIndex });
		expect(r.fragments).toHaveLength(1);
		expect(r.fragments[0]!.term).toBe("email");
	});

	it("falls through to static text when the i18n key is unresolved", async () => {
		await write(
			"app/views/users/_form.html.erb",
			`<label for="user_email"><%= t('not.cached') %> Backup Text</label>`,
		);
		const r = await scanViews(tmp, { langIndex: indexFrom({}) });
		// Strips ERB tag, falls back to "Backup Text".
		expect(r.fragments[0]!.term).toBe("backup text");
		expect(r.fragments[0]!.confidence).toBe(0.95);
	});

	it("drops i18n-only labels when no langIndex supplied AND no static fallback", async () => {
		await write(
			"app/views/users/_form.html.erb",
			`<label for="user_email"><%= t('users.email') %></label>`,
		);
		const r = await scanViews(tmp);
		expect(r.fragments).toEqual([]);
	});

	it("prefers the first resolvable t() call when multiple appear", async () => {
		await write(
			"app/views/users/_form.html.erb",
			`<label for="user_email"><%= t('first.unknown') %> <%= t('second.known') %></label>`,
		);
		const langIndex = indexFrom({ "second.known": "Email Address" });
		const r = await scanViews(tmp, { langIndex });
		expect(r.fragments[0]!.term).toBe("email address");
	});
});

describe("inferEntityFromPath", () => {
	it("singularises plural directory names", () => {
		// OS-specific separators - pass POSIX-style for portability.
		expect(inferEntityFromPath("/repo/app/views/users/_form.html.erb")).toBe(
			"user",
		);
		expect(inferEntityFromPath("/repo/app/views/order_items/_form.haml")).toBe(
			"order_item",
		);
	});

	it("handles `-ies → -y` plurals", () => {
		expect(
			inferEntityFromPath("/repo/app/views/categories/edit.html.erb"),
		).toBe("category");
		expect(inferEntityFromPath("/repo/app/views/cities/index.html.erb")).toBe(
			"city",
		);
	});

	it("handles `-es` plurals (addresses, classes)", () => {
		expect(inferEntityFromPath("/repo/app/views/addresses/edit.html.erb")).toBe(
			"address",
		);
		expect(inferEntityFromPath("/repo/app/views/classes/edit.html.erb")).toBe(
			"class",
		);
	});

	it("handles irregular plurals", () => {
		expect(inferEntityFromPath("/repo/app/views/people/index.html.erb")).toBe(
			"person",
		);
		expect(inferEntityFromPath("/repo/app/views/children/index.html.erb")).toBe(
			"child",
		);
	});

	it("returns null for shared / layout directories", () => {
		expect(
			inferEntityFromPath("/repo/app/views/layouts/application.html.erb"),
		).toBeNull();
		expect(
			inferEntityFromPath("/repo/app/views/shared/_header.html.erb"),
		).toBeNull();
	});

	it("returns null when path is outside app/views/", () => {
		expect(inferEntityFromPath("/repo/lib/something.rb")).toBeNull();
	});

	it("normalises Windows path separators", () => {
		expect(
			inferEntityFromPath("C:\\repo\\app\\views\\users\\_form.html.erb"),
		).toBe("user");
	});
});

describe("scanViews — form-builder f.label", () => {
	it("captures f.label literal-string form via path-inferred entity", async () => {
		await write(
			"app/views/users/_form.html.erb",
			`<%= form_for @user do |f| %>
              <%= f.label :email, "Email Address" %>
              <%= f.label :full_name, "Full Name" %>
            <% end %>`,
		);
		const r = await scanViews(tmp);
		const fields = r.fragments.filter((f) => f.canonical.kind === "field");
		expect(fields).toHaveLength(2);
		expect(fields.map((f) => f.canonical)).toEqual([
			{ kind: "field", field: "user.email" },
			{ kind: "field", field: "user.full_name" },
		]);
		expect(fields[0]!.confidence).toBe(0.9);
		expect(fields[0]!.locator.extractor).toContain("f-label");
	});

	it("captures f.label i18n form via langIndex", async () => {
		await write(
			"app/views/users/_form.html.erb",
			`<%= form_for @user do |f| %>
              <%= f.label :email, t('users.email_label') %>
            <% end %>`,
		);
		const langIndex = indexFrom({
			"users.email_label": "Email Address",
		});
		const r = await scanViews(tmp, { langIndex });
		const fields = r.fragments.filter((f) => f.canonical.kind === "field");
		expect(fields).toHaveLength(1);
		expect(fields[0]!.term).toBe("email address");
		expect(fields[0]!.confidence).toBe(0.88);
		expect(fields[0]!.locator.extractor).toContain("f-label-i18n:en");
	});

	it("accepts varied form-builder var names (form, ff)", async () => {
		await write(
			"app/views/users/_form.html.erb",
			`<%= form_with model: @user do |form| %>
              <%= form.label :email, "Email" %>
            <% end %>
            <%= form_for @user do |ff| %>
              <%= ff.label :balance, "Balance" %>
            <% end %>`,
		);
		const r = await scanViews(tmp);
		expect(r.fragments).toHaveLength(2);
	});

	it("path inference handles plural pluralisations correctly", async () => {
		await write(
			"app/views/categories/_form.html.erb",
			`<%= f.label :name, "Name" %>`,
		);
		const r = await scanViews(tmp);
		expect(r.fragments[0]!.canonical).toEqual({
			kind: "field",
			field: "category.name",
		});
	});

	it("skips f.label calls in shared/layout dirs (no entity context)", async () => {
		await write(
			"app/views/shared/_form.html.erb",
			`<%= f.label :email, "Email" %>`,
		);
		const r = await scanViews(tmp);
		expect(r.fragments).toEqual([]);
	});

	it("ignores unresolved i18n keys (no fragment, no fallback)", async () => {
		await write(
			"app/views/users/_form.html.erb",
			`<%= f.label :email, t('does.not.exist') %>`,
		);
		const r = await scanViews(tmp, { langIndex: indexFrom({}) });
		// Form-builder i18n shape is silently skipped on unresolved
		// keys — there's no static fallback like the <label> path has.
		expect(r.fragments).toEqual([]);
	});
});

describe("scanViews — recursion + dialect tolerance", () => {
	it("walks nested view directories", async () => {
		await write(
			"app/views/admin/users/edit.html.erb",
			`<label for="user_email">Email</label>`,
		);
		const r = await scanViews(tmp);
		expect(r.fragments).toHaveLength(1);
		expect(r.fragments[0]!.locator.file).toContain("admin");
	});

	it("handles single-quoted `for` attributes", async () => {
		await write(
			"app/views/users/_form.html.erb",
			`<label for='user_email'>Email</label>`,
		);
		const r = await scanViews(tmp);
		expect(r.fragments).toHaveLength(1);
	});

	it("ignores non-erb files in the same directory", async () => {
		await write(
			"app/views/users/_form.html.haml",
			`%label{ for: "user_email" } Email`,
		);
		await write(
			"app/views/users/_other.html.erb",
			`<label for="user_email">Email</label>`,
		);
		const r = await scanViews(tmp);
		// Only the .erb file produces a fragment.
		expect(r.fragments).toHaveLength(1);
	});
});
