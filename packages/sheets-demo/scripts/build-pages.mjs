import { cp, mkdir, readFile, writeFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const scriptDir = dirname(fileURLToPath(import.meta.url));
const packageDir = resolve(scriptDir, "..");
const repoRoot = resolve(packageDir, "../..");
const outDir = resolve(repoRoot, "target/sheets-demo-pages");

await mkdir(resolve(outDir, "vendor"), { recursive: true });

const sourceHtml = await readFile(resolve(packageDir, "index.html"), "utf8");
const html = sourceHtml
	.replace("./src/styles.css", "./styles.css")
	.replace("../sheets/dist/index.js", "./vendor/sheets/index.js")
	.replace("./dist/app.js", "./app.js");

await writeFile(resolve(outDir, "index.html"), html);
await writeFile(resolve(outDir, ".nojekyll"), "");
await writeFile(resolve(outDir, "package.json"), '{"type":"module"}\n');
await cp(resolve(packageDir, "src/styles.css"), resolve(outDir, "styles.css"));
await cp(resolve(packageDir, "dist/app.js"), resolve(outDir, "app.js"));
await cp(resolve(packageDir, "dist/app.js.map"), resolve(outDir, "app.js.map"));
await cp(
	resolve(repoRoot, "packages/sheets/dist"),
	resolve(outDir, "vendor/sheets"),
	{
		force: true,
		recursive: true,
	},
);

console.log(`Built GitHub Pages demo at ${outDir}`);
