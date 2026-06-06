import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { scanDjangoAdmin } from "./admin.js";

let tmp: string;

beforeEach(async () => {
	tmp = await mkdtemp(path.join(tmpdir(), "semsql-django-admin-"));
});

afterEach(async () => {
	await rm(tmp, { recursive: true, force: true });
});

async function write(rel: string, body: string): Promise<void> {
	const full = path.join(tmp, rel);
	await mkdir(path.dirname(full), { recursive: true });
	await writeFile(full, body, "utf8");
}

describe("scanDjangoAdmin", () => {
	it("emits ApiResource-layer labels via @admin.display(description=)", async () => {
		await write(
			"users/admin.py",
			`from django.contrib import admin
from .models import User

@admin.register(User)
class UserAdmin(admin.ModelAdmin):
    list_display = ('email', 'status_display')

    @admin.display(description='Account Status')
    def status_display(self, obj):
        return obj.get_status_display()
`,
		);
		const r = await scanDjangoAdmin(tmp);
		expect(r.fragments.length).toBe(1);
		const f = r.fragments[0]!;
		expect(f.term).toBe("account status");
		expect(f.canonical.kind === "field" && f.canonical.field).toBe(
			"user.status_display",
		);
		expect(f.locator.layer).toBe(4); // ApiResource
		expect(f.locator.extractor).toBe("extractor-django:admin");
	});

	it("emits via legacy <method>.short_description = '...' assignment", async () => {
		await write(
			"x/admin.py",
			`from django.contrib import admin

@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    def total_display(self, obj):
        return f"\${obj.total / 100:.2f}"
    total_display.short_description = "Total (USD)"
`,
		);
		const r = await scanDjangoAdmin(tmp);
		const f = r.fragments[0]!;
		expect(f).toBeDefined();
		expect(f.term).toBe("total (usd)");
		expect(f.canonical.kind === "field" && f.canonical.field).toBe(
			"order.total_display",
		);
	});

	it("decorator wins when both decorator and short_description are present", async () => {
		await write(
			"x/admin.py",
			`from django.contrib import admin

@admin.register(User)
class UserAdmin(admin.ModelAdmin):
    @admin.display(description='Decorated Label')
    def col(self, obj):
        return obj.x
    col.short_description = "Legacy Label"
`,
		);
		const r = await scanDjangoAdmin(tmp);
		const labels = r.fragments.map((f) => f.term);
		expect(labels).toContain("decorated label");
		expect(labels).not.toContain("legacy label");
	});

	it("binds via admin.site.register(Model, AdminClass) module call", async () => {
		await write(
			"x/admin.py",
			`from django.contrib import admin

class UserAdmin(admin.ModelAdmin):
    def col(self, obj):
        return obj.x
    col.short_description = "Account Status"

admin.site.register(User, UserAdmin)
`,
		);
		const r = await scanDjangoAdmin(tmp);
		const f = r.fragments[0]!;
		expect(f).toBeDefined();
		expect(f.canonical.kind === "field" && f.canonical.field).toBe("user.col");
	});

	it("skips ModelAdmin without entity binding", async () => {
		await write(
			"x/admin.py",
			`from django.contrib import admin
class StandaloneAdmin(admin.ModelAdmin):
    @admin.display(description='Disconnected')
    def col(self, obj):
        return obj.x
`,
		);
		const r = await scanDjangoAdmin(tmp);
		// No `@admin.register(Model)` and no `admin.site.register(...)` —
		// walker can't bind labels to a canonical entity, drops them.
		expect(r.fragments).toEqual([]);
	});

	it("rejects f-string and non-static descriptions", async () => {
		await write(
			"x/admin.py",
			`from django.contrib import admin
@admin.register(User)
class UserAdmin(admin.ModelAdmin):
    @admin.display(description=f"Dynamic {x}")
    def col_a(self, obj):
        return obj.x

    @admin.display(description=_("Lazy"))
    def col_b(self, obj):
        return obj.x

    def col_c(self, obj):
        return obj.x
    col_c.short_description = f"Dynamic {y}"
`,
		);
		const r = await scanDjangoAdmin(tmp);
		expect(r.fragments).toEqual([]);
	});

	it("walks every nested app's admin.py", async () => {
		await write(
			"apps/users/admin.py",
			`from django.contrib import admin
@admin.register(User)
class UserAdmin(admin.ModelAdmin):
    @admin.display(description='Email')
    def email_display(self, obj):
        return obj.email
`,
		);
		await write(
			"apps/orders/admin.py",
			`from django.contrib import admin
@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    @admin.display(description='Total')
    def total_display(self, obj):
        return obj.total
`,
		);
		const r = await scanDjangoAdmin(tmp);
		const fields = r.fragments.map(
			(f) => f.canonical.kind === "field" && f.canonical.field,
		);
		expect(fields).toContain("user.email_display");
		expect(fields).toContain("order.total_display");
	});

	it("skips migrations/, venv/, __pycache__/", async () => {
		await write(
			"x/migrations/admin.py",
			`from django.contrib import admin
@admin.register(M)
class MAdmin(admin.ModelAdmin):
    @admin.display(description='X')
    def n(self, obj):
        return obj.n
`,
		);
		const r = await scanDjangoAdmin(tmp);
		expect(r.fragments).toEqual([]);
	});
});
