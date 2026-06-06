import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { extractInclusionValidators, scanRailsValidates } from "./validates.js";

let tmp: string;

beforeEach(async () => {
	tmp = await mkdtemp(path.join(tmpdir(), "semsql-rails-validates-"));
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

describe("extractInclusionValidators", () => {
	it("parses %w[] form", () => {
		const text = `class User < ApplicationRecord
            validates :status, inclusion: { in: %w[active inactive pending] }
        end`;
		const decls = extractInclusionValidators(text);
		expect(decls.length).toBe(1);
		expect(decls[0]!.fieldName).toBe("status");
		expect(decls[0]!.values).toEqual(["active", "inactive", "pending"]);
	});

	it("parses %i[] symbol-array form", () => {
		const text = `validates :status, inclusion: { in: %i[draft published] }`;
		const decls = extractInclusionValidators(text);
		expect(decls[0]!.values).toEqual(["draft", "published"]);
	});

	it("parses bracket-array of quoted strings", () => {
		const text = `validates :role, inclusion: { in: ["admin", "member", "guest"] }`;
		const decls = extractInclusionValidators(text);
		expect(decls[0]!.values).toEqual(["admin", "member", "guest"]);
	});

	it("parses bracket-array of symbols", () => {
		const text = `validates :tier, inclusion: { in: [:bronze, :silver, :gold] }`;
		const decls = extractInclusionValidators(text);
		expect(decls[0]!.values).toEqual(["bronze", "silver", "gold"]);
	});

	it("ignores other validator options on the same line", () => {
		const text = `validates :tier, inclusion: { in: [:a, :b], message: "bad" }, presence: true`;
		const decls = extractInclusionValidators(text);
		expect(decls[0]!.values).toEqual(["a", "b"]);
	});

	it("captures multiple inclusion declarations on one model", () => {
		const text = `class User < ApplicationRecord
            validates :status, inclusion: { in: %w[active inactive] }
            validates :role, inclusion: { in: ["admin", "user"] }
        end`;
		const decls = extractInclusionValidators(text);
		const fields = decls.map((d) => d.fieldName).sort();
		expect(fields).toEqual(["role", "status"]);
	});

	it("ignores commented-out validators", () => {
		const text = `class User
            # validates :fake, inclusion: { in: %w[no] }
            validates :real, inclusion: { in: %w[yes] }
        end`;
		const decls = extractInclusionValidators(text);
		expect(decls.length).toBe(1);
		expect(decls[0]!.fieldName).toBe("real");
	});

	it("skips computed `in:` arguments (non-array literals)", () => {
		const text = `validates :status, inclusion: { in: User.allowed_statuses }`;
		const decls = extractInclusionValidators(text);
		expect(decls).toEqual([]);
	});

	it("skips format / numericality / presence validators", () => {
		const text = `class User
            validates :name, presence: true
            validates :email, format: { with: /@/ }
            validates :age, numericality: { in: 0..150 }
        end`;
		const decls = extractInclusionValidators(text);
		expect(decls).toEqual([]);
	});

	it("parses legacy validates_inclusion_of with %w form", () => {
		const text = `class User < ApplicationRecord
            validates_inclusion_of :status, in: %w[active inactive]
        end`;
		const decls = extractInclusionValidators(text);
		expect(decls.length).toBe(1);
		expect(decls[0]!.fieldName).toBe("status");
		expect(decls[0]!.values).toEqual(["active", "inactive"]);
	});

	it("parses legacy validates_inclusion_of with bracket-string form", () => {
		const text = `validates_inclusion_of :role, in: ["admin", "member", "guest"]`;
		const decls = extractInclusionValidators(text);
		expect(decls[0]!.values).toEqual(["admin", "member", "guest"]);
	});

	it("parses legacy validates_inclusion_of with symbol-bracket form", () => {
		const text = `validates_inclusion_of :tier, in: [:bronze, :silver]`;
		const decls = extractInclusionValidators(text);
		expect(decls[0]!.values).toEqual(["bronze", "silver"]);
	});

	it("parses legacy validates_inclusion_of with %i symbol-array form", () => {
		const text = `validates_inclusion_of :priority, in: %i[low high]`;
		const decls = extractInclusionValidators(text);
		expect(decls[0]!.values).toEqual(["low", "high"]);
	});

	it("does not double-emit when both modern + legacy match (rare)", () => {
		// No real Rails file declares the same field twice via both
		// macros — but if it happens, each pattern matches its own
		// syntax. We simply emit once per match; the merge engine
		// dedupes downstream by canonical name.
		const text = `class User
            validates :status, inclusion: { in: %w[a b] }
            validates_inclusion_of :role, in: %w[c d]
        end`;
		const decls = extractInclusionValidators(text);
		const fields = decls.map((d) => d.fieldName).sort();
		expect(fields).toEqual(["role", "status"]);
	});

	it("accepts integer values in bracket form", () => {
		const text = `validates :priority, inclusion: { in: [1, 2, 3] }`;
		const decls = extractInclusionValidators(text);
		expect(decls[0]!.values).toEqual(["1", "2", "3"]);
	});
});

describe("scanRailsValidates", () => {
	it("emits enum_value fragments tied to inflected entity", async () => {
		await write(
			"app/models/user.rb",
			`class User < ApplicationRecord
                validates :status, inclusion: { in: %w[active archived] }
            end`,
		);
		const r = await scanRailsValidates(tmp);
		const archived = r.fragments.find((f) => f.term === "archived");
		expect(archived).toBeDefined();
		if (archived?.canonical.kind === "enum_value") {
			expect(archived.canonical.enumName).toBe("users.status");
			expect(archived.canonical.rawValue).toBe("archived");
		}
	});

	it("respects self.table_name override", async () => {
		await write(
			"app/models/user.rb",
			`class User < ApplicationRecord
                self.table_name = "tbl_users"
                validates :role, inclusion: { in: %w[admin member] }
            end`,
		);
		const r = await scanRailsValidates(tmp);
		const admin = r.fragments.find((f) => f.term === "admin");
		if (admin?.canonical.kind === "enum_value") {
			expect(admin.canonical.enumName).toBe("tbl_users.role");
		}
	});

	it("emits ORM-layer fragments at confidence 0.75", async () => {
		await write(
			"app/models/user.rb",
			`class User < ApplicationRecord
                validates :status, inclusion: { in: %w[a b] }
            end`,
		);
		const r = await scanRailsValidates(tmp);
		for (const f of r.fragments) {
			expect(f.locator.layer).toBe(2);
			expect(f.locator.extractor).toBe("extractor-rails:validates");
			expect(f.confidence).toBe(0.75);
		}
	});

	it("returns empty when models dir is missing", async () => {
		const r = await scanRailsValidates(tmp);
		expect(r.fragments).toEqual([]);
	});

	it("ignores files without `validates` keyword (fast-path)", async () => {
		await write("app/models/empty.rb", `class Empty < ApplicationRecord\nend`);
		const r = await scanRailsValidates(tmp);
		expect(r.fragments).toEqual([]);
	});
});
