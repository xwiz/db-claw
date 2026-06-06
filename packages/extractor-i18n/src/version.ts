import { createRequire } from "node:module";

const requirePackage = createRequire(import.meta.url);
const packageJson = requirePackage("../package.json") as { version?: unknown };

if (
	typeof packageJson.version !== "string" ||
	packageJson.version.length === 0
) {
	throw new Error("@semsql/extractor-i18n package.json is missing a version");
}

export const I18N_VERSION = packageJson.version;
