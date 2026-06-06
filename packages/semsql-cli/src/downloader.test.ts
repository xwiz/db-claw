import { createHash } from "node:crypto";
import { mkdtemp, readFile, writeFile } from "node:fs/promises";
import { createRequire } from "node:module";
import { tmpdir } from "node:os";
import path from "node:path";
import { pathToFileURL } from "node:url";

import { describe, expect, it } from "vitest";

import {
	defaultManifestUrl,
	ensureSemsqlBinary,
	resolveTarget,
} from "./downloader.js";
import { PACKAGE_VERSION } from "./version.js";

const requirePackage = createRequire(import.meta.url);
const packageJson = requirePackage("../package.json") as { version: string };

function digest(body: Buffer | string): string {
	return createHash("sha256").update(body).digest("hex");
}

describe("@semsql/cli binary downloader", () => {
	it("uses package.json as the launcher version source", () => {
		expect(PACKAGE_VERSION).toBe(packageJson.version);
	});

	it("resolves supported OS targets", () => {
		expect(resolveTarget("win32", "x64")).toEqual({
			key: "win32-x64",
			binaryName: "semsql.exe",
		});
		expect(resolveTarget("linux", "x64")).toEqual({
			key: "linux-x64",
			binaryName: "semsql",
		});
	});

	it("honors SEMSQL_BIN without downloading", async () => {
		await expect(
			ensureSemsqlBinary({ env: { SEMSQL_BIN: "C:\\tools\\semsql.exe" } }),
		).resolves.toBe("C:\\tools\\semsql.exe");
	});

	it("builds the default GitHub manifest URL from the version", () => {
		expect(defaultManifestUrl("0.1.0-alpha.1", {})).toBe(
			"https://github.com/xwiz/db-claw/releases/download/v0.1.0-alpha.1/semsql-downloads.json",
		);
	});

	it("downloads and verifies the target binary from a manifest", async () => {
		const root = await mkdtemp(
			path.join(tmpdir(), `semsql-cli-test-${process.pid}-`),
		);
		const cache = path.join(root, "cache");
		const asset = path.join(root, "semsql");
		const body = "#!/bin/sh\necho semsql\n";
		await writeFile(asset, body, "utf8");
		const manifest = path.join(root, "manifest.json");
		await writeFile(
			manifest,
			JSON.stringify({
				version: "0.1.0-test",
				assets: {
					"linux-x64": {
						url: pathToFileURL(asset).toString(),
						sha256: digest(body),
						size: Buffer.byteLength(body),
					},
				},
			}),
			"utf8",
		);

		const bin = await ensureSemsqlBinary({
			platform: "linux",
			arch: "x64",
			env: {
				SEMSQL_CLI_VERSION: "0.1.0-test",
				SEMSQL_CLI_CACHE_DIR: cache,
				SEMSQL_CLI_MANIFEST_URL: pathToFileURL(manifest).toString(),
			},
		});

		await expect(readFile(bin, "utf8")).resolves.toBe(body);
	});

	it("rejects checksum mismatches", async () => {
		const root = await mkdtemp(
			path.join(tmpdir(), `semsql-cli-test-bad-${process.pid}-`),
		);
		const asset = path.join(root, "semsql");
		await writeFile(asset, "not really semsql", "utf8");
		const manifest = path.join(root, "manifest.json");
		await writeFile(
			manifest,
			JSON.stringify({
				version: "0.1.0-test",
				assets: {
					"linux-x64": {
						url: pathToFileURL(asset).toString(),
						sha256: "0".repeat(64),
					},
				},
			}),
			"utf8",
		);

		await expect(
			ensureSemsqlBinary({
				platform: "linux",
				arch: "x64",
				env: {
					SEMSQL_CLI_VERSION: "0.1.0-test",
					SEMSQL_CLI_CACHE_DIR: path.join(root, "cache"),
					SEMSQL_CLI_MANIFEST_URL: pathToFileURL(manifest).toString(),
				},
			}),
		).rejects.toThrow("checksum mismatch");
	});

	it("rejects size mismatches before caching", async () => {
		const root = await mkdtemp(
			path.join(tmpdir(), `semsql-cli-test-size-${process.pid}-`),
		);
		const asset = path.join(root, "semsql");
		const body = "#!/bin/sh\necho semsql\n";
		await writeFile(asset, body, "utf8");
		const manifest = path.join(root, "manifest.json");
		await writeFile(
			manifest,
			JSON.stringify({
				version: "0.1.0-test",
				assets: {
					"linux-x64": {
						url: pathToFileURL(asset).toString(),
						sha256: digest(body),
						size: Buffer.byteLength(body) + 1,
					},
				},
			}),
			"utf8",
		);

		await expect(
			ensureSemsqlBinary({
				platform: "linux",
				arch: "x64",
				env: {
					SEMSQL_CLI_VERSION: "0.1.0-test",
					SEMSQL_CLI_CACHE_DIR: path.join(root, "cache"),
					SEMSQL_CLI_MANIFEST_URL: pathToFileURL(manifest).toString(),
				},
			}),
		).rejects.toThrow("size mismatch");
	});

	it("rejects malformed manifest checksums", async () => {
		const root = await mkdtemp(
			path.join(tmpdir(), `semsql-cli-test-sha-${process.pid}-`),
		);
		const asset = path.join(root, "semsql");
		await writeFile(asset, "echo semsql\n", "utf8");
		const manifest = path.join(root, "manifest.json");
		await writeFile(
			manifest,
			JSON.stringify({
				version: "0.1.0-test",
				assets: {
					"linux-x64": {
						url: pathToFileURL(asset).toString(),
						sha256: "not-a-sha",
					},
				},
			}),
			"utf8",
		);

		await expect(
			ensureSemsqlBinary({
				platform: "linux",
				arch: "x64",
				env: {
					SEMSQL_CLI_VERSION: "0.1.0-test",
					SEMSQL_CLI_CACHE_DIR: path.join(root, "cache"),
					SEMSQL_CLI_MANIFEST_URL: pathToFileURL(manifest).toString(),
				},
			}),
		).rejects.toThrow("invalid sha256");
	});
});
