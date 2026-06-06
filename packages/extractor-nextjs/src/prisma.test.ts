import { describe, expect, it } from "vitest";

import {
	extractFields,
	extractModelBlocks,
	extractTableMap,
	parsePrismaSchema,
} from "./prisma.js";

const SCHEMA = `// schema.prisma
generator client {
  provider = "prisma-client-js"
}

datasource db {
  provider = "postgresql"
  url      = env("DATABASE_URL")
}

model User {
  id         Int      @id @default(autoincrement())
  email      String   @unique
  isActive   Boolean  @default(false) @map("is_active")
  tenantId   Int      @map("tenant_id")
  createdAt  DateTime @default(now()) @map("created_at")
  posts      Post[]

  @@map("users")
  @@index([tenantId])
}

model Post {
  id       Int    @id @default(autoincrement())
  title    String
  authorId Int    @map("author_id")
  author   User   @relation(fields: [authorId], references: [id])
}
`;

describe("extractModelBlocks", () => {
	it("captures every model block by name + body", () => {
		const blocks = extractModelBlocks(SCHEMA);
		const names = blocks.map((b) => b.name).sort();
		expect(names).toEqual(["Post", "User"]);
		const user = blocks.find((b) => b.name === "User")!;
		expect(user.body).toContain('@@map("users")');
	});

	it("does not pick up 'model X {' inside a line comment", () => {
		// A comment that happens to contain the word `model` followed
		// by an identifier and a brace must NOT register a phantom
		// block. Real schemas often contain comments referencing
		// model names ("// model User used to be named ...").
		const text = `
            // historical: model Legacy { id Int }
            model Real {
              id Int @id
            }
        `;
		const blocks = extractModelBlocks(text);
		expect(blocks.map((b) => b.name)).toEqual(["Real"]);
	});

	it("does not pick up 'model X {' inside a block comment", () => {
		const text = `
            /*
             * model Ghost { still inside the comment }
             */
            model Real {
              id Int @id
            }
        `;
		const blocks = extractModelBlocks(text);
		expect(blocks.map((b) => b.name)).toEqual(["Real"]);
	});

	it("ignores braces inside string literals", () => {
		const text = `model Foo {
  meta String @default("a{b}c")
  bar  Int
}`;
		const blocks = extractModelBlocks(text);
		expect(blocks.length).toBe(1);
		expect(blocks[0]!.body).toContain("bar  Int");
	});
});

describe("extractTableMap", () => {
	it("returns the @@map argument if present", () => {
		const block = extractModelBlocks(SCHEMA).find((b) => b.name === "User")!;
		expect(extractTableMap(block.body)).toBe("users");
	});

	it("returns null when @@map is absent", () => {
		const block = extractModelBlocks(SCHEMA).find((b) => b.name === "Post")!;
		expect(extractTableMap(block.body)).toBeNull();
	});
});

describe("extractFields", () => {
	it("flags @relation fields as relations and skipped downstream", () => {
		const block = extractModelBlocks(SCHEMA).find((b) => b.name === "Post")!;
		const fields = extractFields(block.body);
		const author = fields.find((f) => f.tsName === "author")!;
		const title = fields.find((f) => f.tsName === "title")!;
		expect(author.isRelation).toBe(true);
		expect(title.isRelation).toBe(false);
	});

	it("uses @map(...) for db name when present, identifier otherwise", () => {
		const block = extractModelBlocks(SCHEMA).find((b) => b.name === "User")!;
		const fields = extractFields(block.body);
		const map = new Map(fields.map((f) => [f.tsName, f.dbName]));
		expect(map.get("isActive")).toBe("is_active");
		expect(map.get("tenantId")).toBe("tenant_id");
		expect(map.get("email")).toBe("email"); // no @map → identifier
	});

	it("skips block-level annotations (@@map, @@index)", () => {
		const block = extractModelBlocks(SCHEMA).find((b) => b.name === "User")!;
		const fields = extractFields(block.body);
		const tsNames = fields.map((f) => f.tsName);
		expect(tsNames).not.toContain("@@map");
		expect(tsNames).not.toContain("@@index");
	});

	it("ignores comment lines", () => {
		const text = `
            // a comment
            id   Int      @id
            // another comment
            email String  @unique
        `;
		const fields = extractFields(text);
		expect(fields.map((f) => f.tsName).sort()).toEqual(["email", "id"]);
	});
});

describe("parsePrismaSchema", () => {
	it("emits ORM-layer fragments with @@map-resolved entity names", () => {
		const result = parsePrismaSchema("schema.prisma", SCHEMA);
		const userFields = result.fragments.filter(
			(f) =>
				f.canonical.kind === "field" && f.canonical.field.startsWith("users."),
		);
		const labels = userFields.map((f) => f.term).sort();
		// "is active", "tenant" (Id-stripped), "created at", "email", "id"
		expect(labels).toContain("email");
		expect(labels).toContain("is active");
		expect(labels).toContain("tenant");
		expect(labels).toContain("created at");
	});

	it("uses the model identifier when @@map is absent", () => {
		const result = parsePrismaSchema("schema.prisma", SCHEMA);
		const postFields = result.fragments.filter(
			(f) =>
				f.canonical.kind === "field" && f.canonical.field.startsWith("Post."),
		);
		expect(postFields.length).toBeGreaterThan(0);
	});

	it("skips relation fields", () => {
		const result = parsePrismaSchema("schema.prisma", SCHEMA);
		// Post.author is a User relation → must NOT appear.
		const authorFrag = result.fragments.find(
			(f) =>
				f.canonical.kind === "field" && f.canonical.field === "Post.author",
		);
		expect(authorFrag).toBeUndefined();
	});

	it("emits FormOrTableLabel? no — ORM layer always", () => {
		const result = parsePrismaSchema("schema.prisma", SCHEMA);
		for (const f of result.fragments) {
			expect(f.locator.layer).toBe(2);
			expect(f.locator.extractor).toBe("extractor-nextjs:prisma");
		}
	});
});
