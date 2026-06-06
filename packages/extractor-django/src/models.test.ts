import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { scanDjangoModels, toSnakeCase } from "./models.js";

let tmp: string;

beforeEach(async () => {
	tmp = await mkdtemp(path.join(tmpdir(), "semsql-django-"));
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

describe("scanDjangoModels", () => {
	it("emits Orm-layer entity + field labels for verbose_name", async () => {
		await write(
			"users/models.py",
			`from django.db import models

class User(models.Model):
    email = models.EmailField(verbose_name="Email Address", max_length=255)
    is_active = models.BooleanField(verbose_name="Account Status")

    class Meta:
        verbose_name = "Person"
        verbose_name_plural = "People"
`,
		);
		const r = await scanDjangoModels(tmp);
		const fieldFrags = r.fragments.filter((f) => f.canonical.kind === "field");
		const entityFrags = r.fragments.filter(
			(f) => f.canonical.kind === "entity",
		);

		const fieldMap = new Map(
			fieldFrags.map((f) => [
				f.term,
				f.canonical.kind === "field" ? f.canonical.field : "",
			]),
		);
		expect(fieldMap.get("email address")).toBe("user.email");
		expect(fieldMap.get("account status")).toBe("user.is_active");
		for (const f of fieldFrags) {
			expect(f.locator.layer).toBe(2); // Orm
			expect(f.locator.extractor).toBe("extractor-django:models");
		}

		const entityTerms = entityFrags.map((f) => f.term);
		expect(entityTerms).toContain("person");
		expect(entityTerms).toContain("people");
	});

	it("treats first positional string as verbose_name", async () => {
		await write(
			"x/models.py",
			`from django.db import models
class Order(models.Model):
    total = models.DecimalField("Order Total", max_digits=10, decimal_places=2)
`,
		);
		const r = await scanDjangoModels(tmp);
		const total = r.fragments.find(
			(f) =>
				f.canonical.kind === "field" && f.canonical.field === "order.total",
		);
		expect(total).toBeDefined();
		expect(total?.term).toBe("order total");
	});

	it("emits help_text at ApiResource layer", async () => {
		await write(
			"x/models.py",
			`from django.db import models
class Item(models.Model):
    sku = models.CharField(verbose_name="SKU", help_text="Stock keeping unit", max_length=32)
`,
		);
		const r = await scanDjangoModels(tmp);
		const sku = r.fragments.filter(
			(f) => f.canonical.kind === "field" && f.canonical.field === "item.sku",
		);
		expect(sku.length).toBe(2);
		const layers = sku.map((f) => f.locator.layer).sort();
		// Orm (=2) + ApiResource (=4)
		expect(layers).toEqual([2, 4]);
	});

	it("uses Meta.db_table when present", async () => {
		await write(
			"x/models.py",
			`from django.db import models
class LegacyUser(models.Model):
    email = models.EmailField(verbose_name="Email")
    class Meta:
        db_table = "auth_users"
        verbose_name = "Account"
`,
		);
		const r = await scanDjangoModels(tmp);
		const f = r.fragments.find(
			(f) =>
				f.canonical.kind === "field" &&
				f.canonical.field === "auth_users.email",
		);
		expect(f).toBeDefined();
		const e = r.fragments.find(
			(f) => f.canonical.kind === "entity" && f.term === "account",
		);
		expect(e).toBeDefined();
	});

	it("falls back to humanised class name when no Meta.verbose_name", async () => {
		await write(
			"x/models.py",
			`from django.db import models
class CustomerInvoice(models.Model):
    pass
`,
		);
		const r = await scanDjangoModels(tmp);
		const e = r.fragments.find((f) => f.canonical.kind === "entity");
		expect(e?.term).toBe("customer invoice");
	});

	it("ignores classes that don't subclass models.Model / Model", async () => {
		await write(
			"x/models.py",
			`class Helper:
    name = "x"

class Mixin(object):
    pass
`,
		);
		const r = await scanDjangoModels(tmp);
		expect(r.fragments).toEqual([]);
	});

	it("rejects f-string and interpolated verbose_name", async () => {
		await write(
			"x/models.py",
			`from django.db import models
class Foo(models.Model):
    bar = models.CharField(verbose_name=f"Display {x}", max_length=10)
    baz = models.CharField(verbose_name=_("Lazy"), max_length=10)
`,
		);
		const r = await scanDjangoModels(tmp);
		// Both labels are non-static; field-level fragments must NOT
		// be emitted from these forms. Entity-level fallback (`Foo`)
		// can still appear.
		const fieldFrags = r.fragments.filter((f) => f.canonical.kind === "field");
		expect(fieldFrags).toEqual([]);
	});

	it("walks every nested app's models.py", async () => {
		await write(
			"apps/billing/models.py",
			`from django.db import models
class Invoice(models.Model):
    total = models.IntegerField(verbose_name="Total")
`,
		);
		await write(
			"apps/users/models.py",
			`from django.db import models
class User(models.Model):
    email = models.EmailField(verbose_name="Email")
`,
		);
		const r = await scanDjangoModels(tmp);
		const fieldNames = new Set(
			r.fragments
				.filter((f) => f.canonical.kind === "field")
				.map((f) => (f.canonical.kind === "field" ? f.canonical.field : "")),
		);
		expect(fieldNames.has("invoice.total")).toBe(true);
		expect(fieldNames.has("user.email")).toBe(true);
	});

	it("skips migrations/, venv/, __pycache__/", async () => {
		await write(
			"x/migrations/models.py",
			`from django.db import models
class M(models.Model):
    n = models.CharField(verbose_name="N")
`,
		);
		await write(
			".venv/lib/models.py",
			`from django.db import models
class V(models.Model):
    n = models.CharField(verbose_name="N")
`,
		);
		const r = await scanDjangoModels(tmp);
		expect(r.fragments).toEqual([]);
	});

	it("captures the assignment line in locator.line", async () => {
		const src =
			`from django.db import models\n` +
			`\n` +
			`class User(models.Model):\n` +
			`    pass\n` +
			`\n` +
			`class Order(models.Model):\n` + // line 6
			`    # leading comment\n` +
			`    total = models.IntegerField(verbose_name="Total")\n`; // line 8
		await write("x/models.py", src);
		const r = await scanDjangoModels(tmp);
		const total = r.fragments.find(
			(f) =>
				f.canonical.kind === "field" && f.canonical.field === "order.total",
		);
		expect(total).toBeDefined();
		expect(total!.locator.line).toBe(8);
	});

	it("emits enum_value fragments for choices=[(...)] literal lists", async () => {
		await write(
			"x/models.py",
			`from django.db import models
class User(models.Model):
    status = models.CharField(
        max_length=10,
        choices=[("active", "Active"), ("inactive", "Inactive")],
        verbose_name="Status",
    )
`,
		);
		const r = await scanDjangoModels(tmp);
		const enumFrags = r.fragments.filter(
			(f) => f.canonical.kind === "enum_value",
		);
		expect(enumFrags.length).toBe(2);
		const m = new Map(
			enumFrags.map((f) => [
				f.term,
				f.canonical.kind === "enum_value"
					? { name: f.canonical.enumName, raw: f.canonical.rawValue }
					: null,
			]),
		);
		expect(m.get("active")).toEqual({ name: "user.status", raw: "active" });
		expect(m.get("inactive")).toEqual({ name: "user.status", raw: "inactive" });
		for (const f of enumFrags) {
			expect(f.locator.layer).toBe(2); // Orm
		}
	});

	it("supports tuple-of-tuples and integer rawValues", async () => {
		await write(
			"x/models.py",
			`from django.db import models
class Item(models.Model):
    rating = models.IntegerField(choices=((1, "Low"), (2, "Medium"), (3, "High")))
`,
		);
		const r = await scanDjangoModels(tmp);
		const ratings = r.fragments.filter(
			(f) => f.canonical.kind === "enum_value",
		);
		expect(ratings.length).toBe(3);
		const raws = ratings.map(
			(f) => f.canonical.kind === "enum_value" && f.canonical.rawValue,
		);
		expect(raws).toEqual(["1", "2", "3"]);
	});

	it("ignores choices=<bare reference> and i18n-wrapped labels", async () => {
		await write(
			"x/models.py",
			`from django.db import models
class Foo(models.Model):
    a = models.CharField(max_length=5, choices=ROLES)
    b = models.CharField(max_length=5, choices=Status.choices)
    c = models.CharField(max_length=5, choices=[("x", _("Lazy"))])
`,
		);
		const r = await scanDjangoModels(tmp);
		const enumFrags = r.fragments.filter(
			(f) => f.canonical.kind === "enum_value",
		);
		expect(enumFrags).toEqual([]);
	});

	it("strips digit separators on integer rawValue", async () => {
		await write(
			"x/models.py",
			`from django.db import models
class Tier(models.Model):
    points = models.IntegerField(choices=[(1_000, "Bronze"), (10_000, "Gold")])
`,
		);
		const r = await scanDjangoModels(tmp);
		const tiers = r.fragments.filter((f) => f.canonical.kind === "enum_value");
		const raws = tiers.map(
			(f) => f.canonical.kind === "enum_value" && f.canonical.rawValue,
		);
		expect(raws).toEqual(["1000", "10000"]);
	});

	it("emits choices alongside verbose_name without conflict", async () => {
		await write(
			"x/models.py",
			`from django.db import models
class User(models.Model):
    role = models.CharField(
        max_length=10,
        verbose_name="Role",
        choices=[("admin", "Administrator")],
    )
`,
		);
		const r = await scanDjangoModels(tmp);
		const fieldFrag = r.fragments.find(
			(f) => f.canonical.kind === "field" && f.canonical.field === "user.role",
		);
		const enumFrag = r.fragments.find((f) => f.canonical.kind === "enum_value");
		expect(fieldFrag?.term).toBe("role");
		expect(enumFrag?.term).toBe("administrator");
	});

	it("resolves choices=Status.choices via TextChoices class members", async () => {
		await write(
			"x/models.py",
			`from django.db import models
class Status(models.TextChoices):
    ACTIVE = 'active', 'Active'
    INACTIVE = 'inactive', 'Inactive'
    PENDING = 'pending', 'Pending Review'

class User(models.Model):
    status = models.CharField(max_length=10, choices=Status.choices)
`,
		);
		const r = await scanDjangoModels(tmp);
		const enumFrags = r.fragments.filter(
			(f) => f.canonical.kind === "enum_value",
		);
		expect(enumFrags.length).toBe(3);
		const m = new Map(
			enumFrags.map((f) => [
				f.term,
				f.canonical.kind === "enum_value"
					? { name: f.canonical.enumName, raw: f.canonical.rawValue }
					: null,
			]),
		);
		expect(m.get("active")).toEqual({ name: "user.status", raw: "active" });
		expect(m.get("inactive")).toEqual({ name: "user.status", raw: "inactive" });
		expect(m.get("pending review")).toEqual({
			name: "user.status",
			raw: "pending",
		});
	});

	it("resolves choices=Rating.choices via IntegerChoices class", async () => {
		await write(
			"x/models.py",
			`from django.db import models
class Rating(models.IntegerChoices):
    LOW = 1, 'Low'
    HIGH = 10, 'High'

class Item(models.Model):
    rating = models.IntegerField(choices=Rating.choices)
`,
		);
		const r = await scanDjangoModels(tmp);
		const ratings = r.fragments.filter(
			(f) => f.canonical.kind === "enum_value",
		);
		const raws = ratings.map(
			(f) => f.canonical.kind === "enum_value" && f.canonical.rawValue,
		);
		expect(raws).toEqual(["1", "10"]);
	});

	it("accepts parens-wrapped TextChoices member tuples", async () => {
		await write(
			"x/models.py",
			`from django.db import models
class Status(models.TextChoices):
    ACTIVE = ('active', 'Active')
    INACTIVE = ('inactive', 'Inactive')

class User(models.Model):
    status = models.CharField(max_length=10, choices=Status.choices)
`,
		);
		const r = await scanDjangoModels(tmp);
		const enumFrags = r.fragments.filter(
			(f) => f.canonical.kind === "enum_value",
		);
		expect(enumFrags.length).toBe(2);
	});

	it("does not emit fragments for unknown class refs", async () => {
		// `choices=ImportedFromElsewhere.choices` — class isn't
		// declared in this file, so the local map can't resolve it.
		// Cross-file resolution belongs in a dedicated resolver.
		await write(
			"x/models.py",
			`from django.db import models
class User(models.Model):
    status = models.CharField(max_length=10, choices=ImportedFromElsewhere.choices)
`,
		);
		const r = await scanDjangoModels(tmp);
		const enumFrags = r.fragments.filter(
			(f) => f.canonical.kind === "enum_value",
		);
		expect(enumFrags).toEqual([]);
	});

	it("does not treat a TextChoices class as a model entity", async () => {
		await write(
			"x/models.py",
			`from django.db import models
class Status(models.TextChoices):
    ACTIVE = 'active', 'Active'
`,
		);
		const r = await scanDjangoModels(tmp);
		const entityFrags = r.fragments.filter(
			(f) => f.canonical.kind === "entity",
		);
		expect(entityFrags).toEqual([]);
	});

	it("skips TextChoices members without a static label", async () => {
		// `ACTIVE = 'active'` (no second element) and
		// `OTHER = 'other', _("Lazy")` (i18n) both fail static
		// extraction. Don't emit anything for either.
		await write(
			"x/models.py",
			`from django.db import models
class Status(models.TextChoices):
    ACTIVE = 'active'
    OTHER = 'other', _("Lazy")
    OK = 'ok', 'Ok'

class User(models.Model):
    status = models.CharField(max_length=10, choices=Status.choices)
`,
		);
		const r = await scanDjangoModels(tmp);
		const enumFrags = r.fragments.filter(
			(f) => f.canonical.kind === "enum_value",
		);
		expect(enumFrags.length).toBe(1);
		expect(enumFrags[0]!.term).toBe("ok");
	});

	it("inherits fields from an in-file abstract base class", async () => {
		await write(
			"x/models.py",
			`from django.db import models

class Timestamped(models.Model):
    created_at = models.DateTimeField(verbose_name="Created")
    updated_at = models.DateTimeField(verbose_name="Updated")
    class Meta:
        abstract = True

class User(Timestamped):
    email = models.EmailField(verbose_name="Email")
`,
		);
		const r = await scanDjangoModels(tmp);
		const fields = new Set(
			r.fragments
				.filter((f) => f.canonical.kind === "field")
				.map((f) => (f.canonical.kind === "field" ? f.canonical.field : "")),
		);
		// All three fields land on user.* — abstract base contributes
		// created_at/updated_at, User contributes email.
		expect(fields).toContain("user.email");
		expect(fields).toContain("user.created_at");
		expect(fields).toContain("user.updated_at");
		// Abstract base must NOT emit its own entity fragment.
		const entities = r.fragments
			.filter((f) => f.canonical.kind === "entity")
			.map((f) => (f.canonical.kind === "entity" ? f.canonical.entity : ""));
		expect(entities).not.toContain("timestamped");
		expect(entities).toContain("user");
	});

	it("walks multi-level inheritance chains in one file", async () => {
		await write(
			"x/models.py",
			`from django.db import models

class Base(models.Model):
    id_v2 = models.IntegerField(verbose_name="ID v2")
    class Meta:
        abstract = True

class Timestamped(Base):
    created_at = models.DateTimeField(verbose_name="Created")
    class Meta:
        abstract = True

class Order(Timestamped):
    total = models.IntegerField(verbose_name="Total")
`,
		);
		const r = await scanDjangoModels(tmp);
		const fields = new Set(
			r.fragments
				.filter((f) => f.canonical.kind === "field")
				.map((f) => (f.canonical.kind === "field" ? f.canonical.field : "")),
		);
		expect(fields).toContain("order.id_v2");
		expect(fields).toContain("order.created_at");
		expect(fields).toContain("order.total");
	});

	it("does not emit entity fragments for abstract bases", async () => {
		await write(
			"x/models.py",
			`from django.db import models
class Mixin(models.Model):
    name = models.CharField(verbose_name="Name", max_length=10)
    class Meta:
        abstract = True
`,
		);
		const r = await scanDjangoModels(tmp);
		// Abstract bases are skipped — no entity fragment, no field
		// fragment (no concrete entity to bind to).
		expect(r.fragments).toEqual([]);
	});

	it("inherits via a non-Model mixin chained through a Model base", async () => {
		// `class Loggable: pass` is a plain Python class. Even when
		// it appears in the MRO between User and the Django Model
		// base, the walker should still recognise User as a Django
		// model because the chain includes models.Model further up.
		await write(
			"x/models.py",
			`from django.db import models

class Loggable(models.Model):
    log_id = models.IntegerField(verbose_name="Log ID")
    class Meta:
        abstract = True

class Mixin:
    pass

class User(Mixin, Loggable):
    email = models.EmailField(verbose_name="Email")
`,
		);
		const r = await scanDjangoModels(tmp);
		const fields = new Set(
			r.fragments
				.filter((f) => f.canonical.kind === "field")
				.map((f) => (f.canonical.kind === "field" ? f.canonical.field : "")),
		);
		expect(fields).toContain("user.email");
		expect(fields).toContain("user.log_id");
	});

	it("survives an inheritance cycle without hanging", async () => {
		// Pathological — Python wouldn't actually run this — but
		// the walker must finish in bounded time.
		await write(
			"x/models.py",
			`from django.db import models
class A(B):
    a = models.IntegerField(verbose_name="A")
class B(A):
    b = models.IntegerField(verbose_name="B")
class Real(models.Model):
    name = models.CharField(verbose_name="Name", max_length=5)
`,
		);
		const r = await scanDjangoModels(tmp);
		const fields = r.fragments
			.filter((f) => f.canonical.kind === "field")
			.map((f) => (f.canonical.kind === "field" ? f.canonical.field : ""));
		// The cycle in A↔B means neither reaches models.Model
		// through the chain, so they're skipped. Real still emits.
		expect(fields).toContain("real.name");
	});

	it("resolves cross-file TextChoices via the global registry", async () => {
		// Choices class declared in apps/users/enums/models.py and
		// referenced from apps/users/profiles/models.py — the
		// cross-file walker must thread the registry across both
		// files. (We use models.py for both because the walker is
		// currently filename-scoped to models.py; cross-file
		// import-resolution belongs in a dedicated resolver.)
		await write(
			"apps/users/enums/models.py",
			`from django.db import models
class Status(models.TextChoices):
    ACTIVE = 'active', 'Active'
    INACTIVE = 'inactive', 'Inactive'
`,
		);
		await write(
			"apps/users/profiles/models.py",
			`from django.db import models
class User(models.Model):
    status = models.CharField(max_length=10, choices=Status.choices)
`,
		);
		const r = await scanDjangoModels(tmp);
		const enumFrags = r.fragments.filter(
			(f) => f.canonical.kind === "enum_value",
		);
		expect(enumFrags.length).toBe(2);
		const raws = enumFrags.map(
			(f) => f.canonical.kind === "enum_value" && f.canonical.rawValue,
		);
		expect(new Set(raws)).toEqual(new Set(["active", "inactive"]));
	});

	it("resolves cross-file abstract-base inheritance", async () => {
		await write(
			"apps/common/models.py",
			`from django.db import models
class Timestamped(models.Model):
    created_at = models.DateTimeField(verbose_name="Created")
    updated_at = models.DateTimeField(verbose_name="Updated")
    class Meta:
        abstract = True
`,
		);
		await write(
			"apps/users/models.py",
			`from django.db import models
class User(Timestamped):
    email = models.EmailField(verbose_name="Email")
`,
		);
		const r = await scanDjangoModels(tmp);
		const fields = new Set(
			r.fragments
				.filter((f) => f.canonical.kind === "field")
				.map((f) => (f.canonical.kind === "field" ? f.canonical.field : "")),
		);
		expect(fields).toContain("user.email");
		expect(fields).toContain("user.created_at");
		expect(fields).toContain("user.updated_at");
		// Abstract base in the other file must NOT emit its own
		// entity — the dispatcher checks every class's abstract flag
		// against the global registry, not just the local one.
		const entities = r.fragments
			.filter((f) => f.canonical.kind === "entity")
			.map((f) => (f.canonical.kind === "entity" ? f.canonical.entity : ""));
		expect(entities).not.toContain("timestamped");
	});

	it("file-scoped resolution avoids cross-app first-wins collisions", async () => {
		// Two apps each declare a Status TextChoices with different
		// members. With file-scoped resolution, each model's
		// `choices=Status.choices` resolves against its own file's
		// Status — no silent over-merge across apps.
		await write(
			"apps/a/models.py",
			`from django.db import models
class Status(models.TextChoices):
    ACTIVE = 'active', 'Active'

class Foo(models.Model):
    status = models.CharField(max_length=10, choices=Status.choices)
`,
		);
		await write(
			"apps/b/models.py",
			`from django.db import models
class Status(models.TextChoices):
    ARCHIVED = 'archived', 'Archived'

class Bar(models.Model):
    status = models.CharField(max_length=10, choices=Status.choices)
`,
		);
		const r = await scanDjangoModels(tmp);
		const enumFrags = r.fragments.filter(
			(f) => f.canonical.kind === "enum_value",
		);
		expect(enumFrags.length).toBe(2);
		const byEnum = new Map(
			enumFrags.map((f) => [
				f.canonical.kind === "enum_value" ? f.canonical.enumName : "",
				f.canonical.kind === "enum_value" ? f.canonical.rawValue : "",
			]),
		);
		// Each app's model resolves to its own file's Status class.
		expect(byEnum.get("foo.status")).toBe("active");
		expect(byEnum.get("bar.status")).toBe("archived");
	});

	it("resolves choices via explicit `from .x import Status` import", async () => {
		// Sibling-file import. The resolver must walk
		// `<importer_dir>/<modulePath>/models.py`.
		await write(
			"apps/users/choices/models.py",
			`from django.db import models
class Status(models.TextChoices):
    ACTIVE = 'active', 'Active'
    INACTIVE = 'inactive', 'Inactive'
`,
		);
		await write(
			"apps/users/models.py",
			`from django.db import models
from .choices import Status

class User(models.Model):
    status = models.CharField(max_length=10, choices=Status.choices)
`,
		);
		const r = await scanDjangoModels(tmp);
		const enumFrags = r.fragments.filter(
			(f) => f.canonical.kind === "enum_value",
		);
		expect(enumFrags.length).toBe(2);
		const byTerm = new Map(enumFrags.map((f) => [f.term, f]));
		expect(byTerm.has("active")).toBe(true);
		expect(byTerm.has("inactive")).toBe(true);
	});

	it("aliased imports resolve under the alias", async () => {
		await write(
			"apps/users/enums/models.py",
			`from django.db import models
class Status(models.TextChoices):
    ACTIVE = 'active', 'Active'
`,
		);
		await write(
			"apps/users/models.py",
			`from django.db import models
from .enums import Status as UserStatus

class User(models.Model):
    status = models.CharField(max_length=10, choices=UserStatus.choices)
`,
		);
		const r = await scanDjangoModels(tmp);
		const enumFrags = r.fragments.filter(
			(f) => f.canonical.kind === "enum_value",
		);
		expect(enumFrags.length).toBe(1);
		expect(
			enumFrags[0]!.canonical.kind === "enum_value" &&
				enumFrags[0]!.canonical.rawValue,
		).toBe("active");
	});

	it("imported abstract base resolves through file-scoped registry", async () => {
		// Cross-file inheritance via explicit relative import. The
		// class resolver chases the import to the sibling file's
		// Timestamped, picks up its fields onto User.
		await write(
			"apps/common/models.py",
			`from django.db import models
class Timestamped(models.Model):
    created_at = models.DateTimeField(verbose_name="Created")
    class Meta:
        abstract = True
`,
		);
		await write(
			"apps/users/models.py",
			`from django.db import models
from ..common import Timestamped

class User(Timestamped):
    email = models.EmailField(verbose_name="Email")
`,
		);
		const r = await scanDjangoModels(tmp);
		const fields = new Set(
			r.fragments
				.filter((f) => f.canonical.kind === "field")
				.map((f) => (f.canonical.kind === "field" ? f.canonical.field : "")),
		);
		expect(fields).toContain("user.email");
		expect(fields).toContain("user.created_at");
	});

	it("returns empty for non-Django Python projects", async () => {
		await write("util.py", `def hello(): pass\n`);
		const r = await scanDjangoModels(tmp);
		expect(r.fragments).toEqual([]);
	});
});

describe("toSnakeCase", () => {
	it.each([
		["User", "user"],
		["UserProfile", "user_profile"],
		["HTTPResponse", "http_response"],
		["Item", "item"],
		["Already_snake", "already_snake"],
	])("%s → %s", (input, expected) => {
		expect(toSnakeCase(input)).toBe(expected);
	});
});
