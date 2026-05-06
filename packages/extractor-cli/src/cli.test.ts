import { execFileSync } from "node:child_process";
import { mkdtemp, mkdir, writeFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

const CLI = path.resolve(__dirname, "..", "dist", "cli.js");

let tmp: string;

beforeEach(async () => {
    tmp = await mkdtemp(path.join(tmpdir(), "semsql-cli-"));
});

afterEach(async () => {
    await rm(tmp, { recursive: true, force: true });
});

async function setupLaravelProject(): Promise<void> {
    await writeFile(
        path.join(tmp, "composer.json"),
        JSON.stringify({ require: { "laravel/framework": "^11.0" } }),
        "utf8",
    );
    await mkdir(path.join(tmp, "app", "Filament", "Resources"), { recursive: true });
    await writeFile(
        path.join(tmp, "app", "Filament", "Resources", "StudentResource.php"),
        `<?php
        namespace App\\Filament\\Resources;
        class StudentResource extends Resource {
            protected static ?string $model = User::class;
            protected static ?string $modelLabel = 'Student';
            protected static ?string $pluralModelLabel = 'Students';
        }`,
        "utf8",
    );
}

function runCli(args: string[]): string {
    return execFileSync("node", [CLI, ...args], {
        encoding: "utf8",
    });
}

describe("semsql-extract CLI", () => {
    it("auto-detects Laravel and emits fragments to stdout", async () => {
        await setupLaravelProject();
        const out = runCli([tmp]);
        const lines = out.trim().split(/\r?\n/);
        expect(lines.length).toBeGreaterThanOrEqual(2);
        const parsed = lines.map((l) => JSON.parse(l));
        const terms = parsed.map((p) => p.term as string).sort();
        expect(terms).toEqual(["student", "students"]);
    });

    it("writes to --output when provided", async () => {
        await setupLaravelProject();
        const outFile = path.join(tmp, "frags.jsonl");
        const stdoutText = runCli([tmp, "--output", outFile]);
        expect(stdoutText).toBe("");
        const { readFile } = await import("node:fs/promises");
        const body = await readFile(outFile, "utf8");
        expect(body.split(/\r?\n/).filter((l) => l).length).toBeGreaterThan(0);
    });

    it("exits non-zero when no adapter matches", async () => {
        await mkdir(tmp, { recursive: true });
        let caught: { status?: number } | null = null;
        try {
            runCli([tmp]);
        } catch (e) {
            caught = e as { status?: number };
        }
        expect(caught).not.toBeNull();
        expect(caught?.status).toBe(2);
    });

    it("auto-detects Next.js and emits Drizzle fragments", async () => {
        await writeFile(
            path.join(tmp, "package.json"),
            JSON.stringify({ dependencies: { next: "^14.0.0" } }),
            "utf8",
        );
        await mkdir(path.join(tmp, "src", "db"), { recursive: true });
        await writeFile(
            path.join(tmp, "src", "db", "schema.ts"),
            `
            import { pgTable, text, integer, boolean } from "drizzle-orm/pg-core";
            export const users = pgTable("users", {
              id: integer("id").primaryKey(),
              email: text("email").notNull(),
              isActive: boolean("is_active").default(false),
            });
            `,
            "utf8",
        );
        const out = runCli([tmp]);
        const lines = out.trim().split(/\r?\n/);
        expect(lines.length).toBeGreaterThan(0);
        const parsed = lines.map((l) => JSON.parse(l));
        const isActive = parsed.find((p) => p.term === "is active");
        expect(isActive?.canonical?.field).toBe("users.is_active");
        expect(isActive?.locator?.layer).toBe(2);
    });

    it("auto-detect picks every adapter that matches (multi-stack repos)", async () => {
        // A repo that ships both a Laravel API and a Next.js dashboard.
        await writeFile(
            path.join(tmp, "composer.json"),
            JSON.stringify({ require: { "laravel/framework": "^11" } }),
            "utf8",
        );
        await writeFile(
            path.join(tmp, "package.json"),
            JSON.stringify({ dependencies: { next: "^14" } }),
            "utf8",
        );
        await mkdir(path.join(tmp, "app", "Filament", "Resources"), { recursive: true });
        await writeFile(
            path.join(tmp, "app", "Filament", "Resources", "StudentResource.php"),
            `<?php
            class StudentResource extends Resource {
                protected static ?string $model = User::class;
                protected static ?string $modelLabel = 'Student';
            }`,
            "utf8",
        );
        await mkdir(path.join(tmp, "src", "db"), { recursive: true });
        await writeFile(
            path.join(tmp, "src", "db", "schema.ts"),
            `
            import { pgTable, text } from "drizzle-orm/pg-core";
            export const users = pgTable("users", { email: text("email") });
            `,
            "utf8",
        );
        const out = runCli([tmp]);
        const parsed = out
            .trim()
            .split(/\r?\n/)
            .map((l) => JSON.parse(l));
        // Filament-derived fragment present.
        expect(parsed.find((p) => p.term === "student")).toBeDefined();
        // Drizzle-derived fragment present.
        expect(parsed.find((p) => p.term === "email")).toBeDefined();
    });
});
