import { mkdtemp, mkdir, writeFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { RailsExtractor } from "./index.js";

let tmp: string;

beforeEach(async () => {
    tmp = await mkdtemp(path.join(tmpdir(), "semsql-rails-detect-"));
});

afterEach(async () => {
    await rm(tmp, { recursive: true, force: true });
});

async function writeGemfile(content: string): Promise<void> {
    await mkdir(tmp, { recursive: true });
    await writeFile(path.join(tmp, "Gemfile"), content, "utf8");
}

describe("RailsExtractor.detect", () => {
    const ext = new RailsExtractor();

    it("returns true for `gem 'rails'`", async () => {
        await writeGemfile(`source "https://rubygems.org"\ngem "rails", "~> 7.1"\n`);
        expect(await ext.detect(tmp)).toBe(true);
    });

    it("returns true for `gem 'activerecord'`", async () => {
        await writeGemfile(`gem 'activerecord'\n`);
        expect(await ext.detect(tmp)).toBe(true);
    });

    it("does NOT match `gem 'rails-i18n'` alone", async () => {
        // Regression: the previous `\b...\b` regex matched
        // `rails-i18n` because `-` is a word boundary. A Gemfile
        // that pulls in rails-i18n but no Rails core (rare but
        // possible — some gem-only libs do this) must NOT be
        // flagged as a Rails project.
        await writeGemfile(`gem 'rails-i18n'\n`);
        expect(await ext.detect(tmp)).toBe(false);
    });

    it("returns true when both rails and rails-i18n are present", async () => {
        await writeGemfile(`gem 'rails'\ngem 'rails-i18n'\n`);
        expect(await ext.detect(tmp)).toBe(true);
    });

    it("returns false when Gemfile is missing", async () => {
        expect(await ext.detect(tmp)).toBe(false);
    });

    it("returns false on unrelated Gemfile", async () => {
        await writeGemfile(`gem 'sinatra'\ngem 'rack'\n`);
        expect(await ext.detect(tmp)).toBe(false);
    });
});
