import { mkdtemp, mkdir, writeFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { scanDjangoSerializers } from "./serializers.js";

let tmp: string;

beforeEach(async () => {
    tmp = await mkdtemp(path.join(tmpdir(), "semsql-django-ser-"));
});

afterEach(async () => {
    await rm(tmp, { recursive: true, force: true });
});

async function write(rel: string, body: string): Promise<void> {
    const full = path.join(tmp, rel);
    await mkdir(path.dirname(full), { recursive: true });
    await writeFile(full, body, "utf8");
}

describe("scanDjangoSerializers", () => {
    it("emits ApiResource-layer label fragments via Meta.model + label kwarg", async () => {
        await write(
            "users/serializers.py",
            `from rest_framework import serializers
from .models import User

class UserSerializer(serializers.ModelSerializer):
    email_address = serializers.EmailField(source='email', label='Email Address')
    is_active = serializers.BooleanField(label='Account Status')

    class Meta:
        model = User
        fields = ['id', 'email_address', 'is_active']
`,
        );
        const r = await scanDjangoSerializers(tmp);
        const m = new Map(
            r.fragments.map((f) => [
                f.term,
                f.canonical.kind === "field" ? f.canonical.field : "",
            ]),
        );
        // `source='email'` rewrites the canonical field to user.email
        // even though the serializer field is `email_address`.
        expect(m.get("email address")).toBe("user.email");
        expect(m.get("account status")).toBe("user.is_active");
        for (const f of r.fragments) {
            expect(f.locator.layer).toBe(4); // ApiResource
            expect(f.locator.extractor).toBe("extractor-django:serializers");
        }
    });

    it("emits help_text alongside label", async () => {
        await write(
            "x/serializers.py",
            `from rest_framework import serializers
class ItemSerializer(serializers.ModelSerializer):
    sku = serializers.CharField(label='SKU', help_text='Stock keeping unit')
    class Meta:
        model = Item
        fields = ['sku']
`,
        );
        const r = await scanDjangoSerializers(tmp);
        const sku = r.fragments.filter(
            (f) => f.canonical.kind === "field" && f.canonical.field === "item.sku",
        );
        expect(sku.length).toBe(2);
        const terms = sku.map((f) => f.term).sort();
        expect(terms).toEqual(["sku", "stock keeping unit"]);
    });

    it("ignores serializers without Meta.model", async () => {
        await write(
            "x/serializers.py",
            `from rest_framework import serializers
class StandaloneSerializer(serializers.Serializer):
    name = serializers.CharField(label='Name')
`,
        );
        const r = await scanDjangoSerializers(tmp);
        // No Meta.model → no entity binding → no fragments. Future
        // work: emit unbound fragments under a synthetic namespace.
        expect(r.fragments).toEqual([]);
    });

    it("walks user-defined Serializer subclasses", async () => {
        await write(
            "x/serializers.py",
            `from rest_framework import serializers
class TimestampedSerializer(serializers.ModelSerializer):
    created_at = serializers.DateTimeField(label='Created')

class PostSerializer(TimestampedSerializer):
    title = serializers.CharField(label='Title')
    class Meta:
        model = Post
        fields = ['title', 'created_at']
`,
        );
        const r = await scanDjangoSerializers(tmp);
        const titles = r.fragments.filter((f) => f.term === "title");
        expect(titles.length).toBe(1);
        expect(
            titles[0]!.canonical.kind === "field" && titles[0]!.canonical.field,
        ).toBe("post.title");
    });

    it("handles HyperlinkedModelSerializer base", async () => {
        await write(
            "x/serializers.py",
            `from rest_framework import serializers
class CityHySerializer(serializers.HyperlinkedModelSerializer):
    name = serializers.CharField(label='City Name')
    class Meta:
        model = City
        fields = ['name']
`,
        );
        const r = await scanDjangoSerializers(tmp);
        const f = r.fragments.find((f) => f.term === "city name");
        expect(f).toBeDefined();
        expect(f?.canonical.kind === "field" && f.canonical.field).toBe("city.name");
    });

    it("falls back to lhs name when source= is absent", async () => {
        await write(
            "x/serializers.py",
            `from rest_framework import serializers
class BookSerializer(serializers.ModelSerializer):
    title = serializers.CharField(label='Title')
    class Meta:
        model = Book
        fields = ['title']
`,
        );
        const r = await scanDjangoSerializers(tmp);
        const f = r.fragments[0];
        expect(f?.canonical.kind === "field" && f.canonical.field).toBe("book.title");
    });

    it("ignores fields with no label and no help_text", async () => {
        await write(
            "x/serializers.py",
            `from rest_framework import serializers
class FooSerializer(serializers.ModelSerializer):
    name = serializers.CharField(max_length=10)
    class Meta:
        model = Foo
        fields = ['name']
`,
        );
        const r = await scanDjangoSerializers(tmp);
        expect(r.fragments).toEqual([]);
    });

    it("skips f-string and non-static labels", async () => {
        await write(
            "x/serializers.py",
            `from rest_framework import serializers
class BarSerializer(serializers.ModelSerializer):
    a = serializers.CharField(label=f"Dynamic {x}")
    b = serializers.CharField(label=_("Lazy"))
    class Meta:
        model = Bar
        fields = ['a', 'b']
`,
        );
        const r = await scanDjangoSerializers(tmp);
        expect(r.fragments).toEqual([]);
    });

    it("walks every nested app's serializers.py", async () => {
        await write(
            "apps/users/serializers.py",
            `from rest_framework import serializers
class US(serializers.ModelSerializer):
    email = serializers.EmailField(label='Email')
    class Meta:
        model = User
        fields = ['email']
`,
        );
        await write(
            "apps/orders/serializers.py",
            `from rest_framework import serializers
class OS(serializers.ModelSerializer):
    total = serializers.IntegerField(label='Total')
    class Meta:
        model = Order
        fields = ['total']
`,
        );
        const r = await scanDjangoSerializers(tmp);
        const fieldNames = new Set(
            r.fragments.map((f) =>
                f.canonical.kind === "field" ? f.canonical.field : "",
            ),
        );
        expect(fieldNames.has("user.email")).toBe(true);
        expect(fieldNames.has("order.total")).toBe(true);
    });
});
