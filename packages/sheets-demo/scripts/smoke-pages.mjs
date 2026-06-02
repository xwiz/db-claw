import { access, readFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const scriptDir = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(scriptDir, "../../..");
const outDir = resolve(repoRoot, "target/sheets-demo-pages");

const requiredFiles = [
	"index.html",
	"app.js",
	"styles.css",
	"vendor/sheets/index.js",
];

for (const file of requiredFiles) {
	await access(resolve(outDir, file));
}

const html = await readFile(resolve(outDir, "index.html"), "utf8");
for (const expected of [
	"./styles.css",
	"./app.js",
	"./vendor/sheets/index.js",
]) {
	if (!html.includes(expected)) {
		throw new Error(`Pages HTML does not reference ${expected}`);
	}
}

const runtime = await import(
	pathToFileURL(resolve(outDir, "vendor/sheets/index.js")).href
);

const dataset = runtime.buildSheetDataset(runtime.parseCsv(runtime.SAMPLE_CSV));
const result = runtime.querySheet(dataset, "total revenue by region");

if (!result.ok) {
	throw new Error(`Expected smoke query to pass: ${result.rejectionReason}`);
}
if (
	result.rows[0]?.Region !== "LATAM" ||
	result.rows[0]?.["SUM Revenue"] !== 10500
) {
	throw new Error("Pages runtime returned the wrong smoke result");
}
if (result.confidence.level !== "high") {
	throw new Error(`Expected high confidence, got ${result.confidence.level}`);
}

if (runtime.SHEET_USE_CASES.length < 7) {
	throw new Error("Expected the Pages demo to include varied practical CSVs");
}

for (const useCase of runtime.SHEET_USE_CASES) {
	const useCaseDataset = runtime.buildSheetDataset(
		runtime.parseCsv(useCase.csv),
	);
	for (const question of useCase.questions) {
		const useCaseResult = runtime.querySheet(useCaseDataset, question);
		if (!useCaseResult.ok) {
			throw new Error(
				`Use case question failed (${useCase.id}): ${question} - ${useCaseResult.rejectionReason}`,
			);
		}
	}
}

console.log("Pages artifact smoke passed");
