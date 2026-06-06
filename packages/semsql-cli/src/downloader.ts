import { createHash } from "node:crypto";
import { existsSync } from "node:fs";
import {
	chmod,
	mkdir,
	readFile,
	rename,
	rm,
	writeFile,
} from "node:fs/promises";
import { get as httpGet } from "node:http";
import { get as httpsGet } from "node:https";
import { homedir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { PACKAGE_VERSION } from "./version.js";

const DEFAULT_REPOSITORY = "xwiz/db-claw";
const MANIFEST_NAME = "semsql-downloads.json";

export interface BinaryAsset {
	url: string;
	sha256: string;
	size?: number;
}

export interface ReleaseManifest {
	version: string;
	assets: Record<string, BinaryAsset>;
}

export interface BinaryTarget {
	key: string;
	binaryName: string;
}

export interface EnsureBinaryOptions {
	env?: NodeJS.ProcessEnv;
	platform?: NodeJS.Platform;
	arch?: NodeJS.Architecture;
}

const TARGETS: Record<string, BinaryTarget> = {
	"darwin-arm64": { key: "darwin-arm64", binaryName: "semsql" },
	"darwin-x64": { key: "darwin-x64", binaryName: "semsql" },
	"linux-arm64": { key: "linux-arm64", binaryName: "semsql" },
	"linux-x64": { key: "linux-x64", binaryName: "semsql" },
	"win32-x64": { key: "win32-x64", binaryName: "semsql.exe" },
};

export function resolveTarget(
	platform: NodeJS.Platform = process.platform,
	arch: NodeJS.Architecture = process.arch,
): BinaryTarget {
	const key = `${platform}-${arch}`;
	const target = TARGETS[key];
	if (!target) {
		throw new Error(
			`unsupported semsql binary target '${key}'. Supported: ${Object.keys(TARGETS).join(", ")}`,
		);
	}
	return target;
}

export function defaultCacheDir(env: NodeJS.ProcessEnv = process.env): string {
	if (env.SEMSQL_CLI_CACHE_DIR) return env.SEMSQL_CLI_CACHE_DIR;
	if (process.platform === "win32" && env.LOCALAPPDATA) {
		return path.join(env.LOCALAPPDATA, "semsql", "bin");
	}
	const xdg = env.XDG_CACHE_HOME;
	return path.join(xdg || path.join(homedir(), ".cache"), "semsql", "bin");
}

export function defaultManifestUrl(
	version: string,
	env: NodeJS.ProcessEnv = process.env,
): string {
	if (env.SEMSQL_CLI_MANIFEST_URL) return env.SEMSQL_CLI_MANIFEST_URL;
	const tag = env.SEMSQL_CLI_RELEASE_TAG || `v${version}`;
	const base =
		env.SEMSQL_CLI_DOWNLOAD_BASE_URL ||
		`https://github.com/${DEFAULT_REPOSITORY}/releases/download/${tag}`;
	return `${base.replace(/\/$/, "")}/${MANIFEST_NAME}`;
}

export async function ensureSemsqlBinary(
	options: EnsureBinaryOptions = {},
): Promise<string> {
	const env = options.env || process.env;
	if (env.SEMSQL_BIN) return env.SEMSQL_BIN;

	const version = env.SEMSQL_CLI_VERSION || PACKAGE_VERSION;
	const target = resolveTarget(options.platform, options.arch);
	const cacheRoot = defaultCacheDir(env);
	const binaryPath = path.join(
		cacheRoot,
		version,
		target.key,
		target.binaryName,
	);

	if (existsSync(binaryPath)) return binaryPath;
	if (env.SEMSQL_CLI_SKIP_DOWNLOAD === "1") {
		throw new Error(
			`semsql binary is not cached at ${binaryPath}; unset SEMSQL_CLI_SKIP_DOWNLOAD or set SEMSQL_BIN`,
		);
	}
	if (version.endsWith("-dev") && !env.SEMSQL_CLI_MANIFEST_URL) {
		throw new Error(
			"@semsql/cli dev builds cannot infer a GitHub Release asset. Set SEMSQL_BIN or SEMSQL_CLI_MANIFEST_URL.",
		);
	}

	const manifest = await downloadJson<ReleaseManifest>(
		defaultManifestUrl(version, env),
	);
	const asset = manifest.assets[target.key];
	if (!asset) {
		throw new Error(
			`release manifest has no semsql binary for ${target.key}. Available: ${Object.keys(manifest.assets).join(", ")}`,
		);
	}
	validateAsset(target.key, asset);

	const body = await downloadBytes(asset.url);
	if (asset.size !== undefined && body.byteLength !== asset.size) {
		throw new Error(
			`size mismatch for ${asset.url}: expected ${asset.size} bytes, got ${body.byteLength}`,
		);
	}
	const digest = sha256(body);
	if (digest !== asset.sha256.toLowerCase()) {
		throw new Error(
			`checksum mismatch for ${asset.url}: expected ${asset.sha256}, got ${digest}`,
		);
	}

	await mkdir(path.dirname(binaryPath), { recursive: true });
	const tempPath = `${binaryPath}.${process.pid}.tmp`;
	await writeFile(tempPath, body, { mode: 0o755 });
	if (process.platform !== "win32") await chmod(tempPath, 0o755);
	try {
		await rename(tempPath, binaryPath);
	} catch (error) {
		await rm(tempPath, { force: true });
		throw error;
	}
	return binaryPath;
}

function validateAsset(targetKey: string, asset: BinaryAsset): void {
	if (!asset.url) {
		throw new Error(`release manifest asset for ${targetKey} is missing url`);
	}
	if (!/^[a-fA-F0-9]{64}$/.test(asset.sha256)) {
		throw new Error(
			`release manifest asset for ${targetKey} has invalid sha256`,
		);
	}
	if (
		asset.size !== undefined &&
		(!Number.isInteger(asset.size) || asset.size < 0)
	) {
		throw new Error(`release manifest asset for ${targetKey} has invalid size`);
	}
}

function sha256(body: Buffer): string {
	return createHash("sha256").update(body).digest("hex");
}

async function downloadJson<T>(url: string): Promise<T> {
	const body = await downloadBytes(url);
	return JSON.parse(body.toString("utf8")) as T;
}

async function downloadBytes(url: string, redirects = 0): Promise<Buffer> {
	if (redirects > 5)
		throw new Error(`too many redirects while downloading ${url}`);
	const parsed = new URL(url);
	if (parsed.protocol === "file:") return readFile(fileURLToPath(parsed));
	const get =
		parsed.protocol === "https:"
			? httpsGet
			: parsed.protocol === "http:"
				? httpGet
				: null;
	if (get === null)
		throw new Error(`unsupported download protocol '${parsed.protocol}'`);

	return new Promise((resolve, reject) => {
		const req = get(parsed, (res) => {
			const location = res.headers.location;
			if (
				location &&
				res.statusCode !== undefined &&
				res.statusCode >= 300 &&
				res.statusCode < 400
			) {
				res.resume();
				resolve(
					downloadBytes(new URL(location, parsed).toString(), redirects + 1),
				);
				return;
			}
			if (
				res.statusCode === undefined ||
				res.statusCode < 200 ||
				res.statusCode >= 300
			) {
				res.resume();
				reject(new Error(`download failed for ${url}: HTTP ${res.statusCode}`));
				return;
			}
			const chunks: Buffer[] = [];
			res.on("data", (chunk: Buffer) => chunks.push(chunk));
			res.on("end", () => resolve(Buffer.concat(chunks)));
		});
		req.on("error", reject);
		req.end();
	});
}
