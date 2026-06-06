/**
 * Laravel + Filament extractor.
 *
 * Highest extraction surface of any framework we target:
 *
 *  - Filament `Forms\Components\TextInput::make('foo')->label('Bar')`
 *    (highest fidelity for enum labels — explicit UI value mappings).
 *  - Filament `Tables\Columns\TextColumn::make()->label()`.
 *  - Filament Resource `$navigationLabel`, `$modelLabel`, `$pluralModelLabel`.
 *  - Eloquent `$casts`, `$fillable`, relationship methods, global scopes.
 *  - `lang/*.php`, `lang/**\/*.json` via @semsql/extractor-i18n.
 *  - Blade `<th>label</th>` adjacent to `{{ $model->field }}`.
 *
 * Parser: tree-sitter-php (PHP, not TypeScript — corrected from the
 * earlier draft of the architecture plan).
 *
 * PHP AST walkers, framework conventions, and fixture coverage are kept in
 * this adapter so extraction behavior stays testable.
 */

import { promises as fs } from "node:fs";
import path from "node:path";

import type {
	ExtractCtx,
	Extractor,
	VocabFragment,
} from "@semsql/extractor-sdk";

import { scanLaravelConfig } from "./config.js";
import { scanEloquentModels } from "./eloquent.js";
import { scanFilamentResources } from "./filament.js";
import { scanLangDir } from "./lang.js";

export { scanLaravelConfig, type ConfigScanResult } from "./config.js";
export { scanLangDir, type LangScanResult } from "./lang.js";
export {
	scanFilamentResources,
	parseResourceProperties,
	modelClassToEntityCanonical,
	type FilamentScanResult,
	type ClassToEntityIndex,
} from "./filament.js";
export {
	scanEloquentModels,
	parseModelProperties,
	type EloquentScanResult,
} from "./eloquent.js";

export class LaravelExtractor implements Extractor {
	public readonly name = "extractor-laravel";

	async detect(root: string): Promise<boolean> {
		const composer = path.join(root, "composer.json");
		try {
			const text = await fs.readFile(composer, "utf8");
			const parsed = JSON.parse(text) as {
				require?: Record<string, string>;
				"require-dev"?: Record<string, string>;
			};
			const deps = {
				...(parsed.require ?? {}),
				...(parsed["require-dev"] ?? {}),
			};
			return "laravel/framework" in deps;
		} catch {
			return false;
		}
	}

	async *extract(ctx: ExtractCtx): AsyncIterable<VocabFragment> {
		// Eloquent walker runs first — it builds the class→table map that
		// the Filament walker then consults. Both walkers also emit
		// fragments at their own layers (ORM=2, Form/TableLabel=6).
		const eloquent = await scanEloquentModels(ctx.root);
		for (const f of eloquent.fragments) yield f;

		const config = await scanLaravelConfig(
			ctx.root,
			fieldCanonicals(eloquent.fragments),
		);
		for (const f of config.fragments) yield f;

		// Filament Resource walkers — highest source layer (form/table label).
		const filament = await scanFilamentResources(
			ctx.root,
			eloquent.classToEntity,
		);
		for (const f of filament.fragments) yield f;

		// i18n lang directories — layer 5.
		for (const sub of ["lang", path.join("resources", "lang")]) {
			const dir = path.join(ctx.root, sub);
			const result = await scanLangDir(dir);
			for (const f of result.fragments) yield f;
		}
	}
}

function fieldCanonicals(fragments: VocabFragment[]): string[] {
	const fields = new Set<string>();
	for (const fragment of fragments) {
		if (fragment.canonical.kind === "field") {
			fields.add(fragment.canonical.field);
		}
	}
	return Array.from(fields).sort();
}

export { LARAVEL_VERSION } from "./version.js";
