import { createRequire } from "node:module";

const requirePackage = createRequire(import.meta.url);
const packageJson = requirePackage("../package.json") as { version?: unknown };

if (
	typeof packageJson.version !== "string" ||
	packageJson.version.length === 0
) {
	throw new Error("@semsql/extractor-rails package.json is missing a version");
}

export const RAILS_VERSION = packageJson.version;
