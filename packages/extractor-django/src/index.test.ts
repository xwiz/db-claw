import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { DjangoExtractor } from "./index.js";

let tmp: string;

beforeEach(async () => {
	tmp = await mkdtemp(path.join(tmpdir(), "semsql-django-detect-"));
});

afterEach(async () => {
	await rm(tmp, { recursive: true, force: true });
});

async function write(rel: string, body: string): Promise<void> {
	const full = path.join(tmp, rel);
	await mkdir(path.dirname(full), { recursive: true });
	await writeFile(full, body, "utf8");
}

describe("DjangoExtractor.detect", () => {
	it("detects manage.py at the project root", async () => {
		await write("manage.py", "#!/usr/bin/env python\n");
		const e = new DjangoExtractor();
		expect(await e.detect(tmp)).toBe(true);
	});

	it("detects django in pyproject.toml", async () => {
		await write(
			"pyproject.toml",
			`[project]
name = "thing"
dependencies = ["django>=5.0", "psycopg[binary]"]
`,
		);
		const e = new DjangoExtractor();
		expect(await e.detect(tmp)).toBe(true);
	});

	it("detects Django at the start of a requirements.txt line", async () => {
		await write("requirements.txt", "redis>=5\nDjango==5.1.2\n");
		expect(await new DjangoExtractor().detect(tmp)).toBe(true);
	});

	it("ignores django mentioned only in a transitive comment", async () => {
		// Substring match would false-positive on "# pre-django legacy
		// stuff"; the pyproject regex is word-boundary'd. requirements
		// path uses ^Django so a comment beats it.
		await write("requirements.txt", "# uses django-style routing\nflask\n");
		expect(await new DjangoExtractor().detect(tmp)).toBe(false);
	});

	it("returns false for an empty project", async () => {
		expect(await new DjangoExtractor().detect(tmp)).toBe(false);
	});
});

describe("DjangoExtractor.extract", () => {
	it("yields fragments via the models walker", async () => {
		await write("manage.py", "#!/usr/bin/env python\n");
		await write(
			"users/models.py",
			`from django.db import models
class User(models.Model):
    email = models.EmailField(verbose_name="Email")
`,
		);
		const out: string[] = [];
		for await (const frag of new DjangoExtractor().extract({ root: tmp })) {
			if (frag.canonical.kind === "field") out.push(frag.canonical.field);
		}
		expect(out).toContain("user.email");
	});
});
