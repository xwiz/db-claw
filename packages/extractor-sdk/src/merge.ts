/**
 * Merge engine — applies the priority cascade across all extractor outputs.
 *
 *     Form/Table label > i18n > Filament admin > API resource >
 *         app constant > ORM > DB schema
 *
 * For each `(term, canonical_target)` pair, the highest-layer fragment wins;
 * lower layers attach as `aliases[]`. When two fragments at the *same* layer
 * disagree on canonical target, the conflict is recorded in `conflicts[]`
 * for `semsql doctor` to surface.
 */

import type { Canonical, Locator, VocabFragment } from "./types.js";

/** One merged vocabulary entry — wins over the alternatives in `superseded`. */
export interface MergedEntry {
	term: string;
	canonical: Canonical;
	confidence: number;
	locator: Locator;
	/** Lower-layer fragments that pointed at the same canonical target. */
	superseded: Locator[];
}

/** Two extractor sources disagreed on the canonical target for one term. */
export interface ConflictEntry {
	term: string;
	candidates: VocabFragment[];
	resolution: string;
}

/** Output of `mergeFragments`. */
export interface MergeResult {
	entries: MergedEntry[];
	conflicts: ConflictEntry[];
}

function canonicalKey(c: Canonical): string {
	switch (c.kind) {
		case "entity":
			return `entity:${c.entity}`;
		case "field":
			return `field:${c.field}`;
		case "enum_value":
			return `enum:${c.enumName}:${c.rawValue}`;
		case "scope_predicate":
			return `scope:${c.scope}:${c.field}:${c.operator}:${c.rawValue}`;
		case "relationship":
			return [
				"rel",
				`${c.from}.${c.fromField ?? "*"}`,
				`${c.to}.${c.toField ?? "*"}`,
				c.relationshipKind ?? "unknown",
				c.relationName ?? "",
			].join(":");
	}
}

/** Apply the cascade. Pure function — no I/O. */
export function mergeFragments(
	fragments: Iterable<VocabFragment>,
): MergeResult {
	// Bucket by (term, canonical_key) so superseding is per-target, not
	// per-term — the same term can map to multiple canonical refs (e.g.
	// "Organization" → tenants entity AND users.tenant_id field).
	const buckets = new Map<string, VocabFragment[]>();
	for (const f of fragments) {
		const key = `${f.term}\0${canonicalKey(f.canonical)}`;
		const arr = buckets.get(key);
		if (arr) arr.push(f);
		else buckets.set(key, [f]);
	}

	const entries: MergedEntry[] = [];
	const conflicts: ConflictEntry[] = [];

	// Cross-bucket conflict: same term → different canonical targets at the
	// same highest layer. Record but do not arbitrate — `semsql doctor`
	// surfaces these to the user with file:line provenance.
	const winnersByTerm = new Map<string, MergedEntry[]>();

	for (const [key, fs] of buckets) {
		// Sort priority:
		//   1. layer DESC — higher layer (FormOrTableLabel > i18n >
		//      ApiResource > AppConstant > Orm > DbSchema) wins.
		//   2. confidence DESC — at same layer, the more confident
		//      walker wins (e.g. Drizzle 0.7 over Pinia 0.5 at the
		//      Orm layer).
		//   3. file ASC, then line ASC — final deterministic tie-
		//      break so two walkers with identical confidence at the
		//      same layer always pick the same winner across runs.
		fs.sort((a, b) => {
			if (a.locator.layer !== b.locator.layer) {
				return b.locator.layer - a.locator.layer;
			}
			if (a.confidence !== b.confidence) {
				return b.confidence - a.confidence;
			}
			const fileCmp = a.locator.file.localeCompare(b.locator.file);
			if (fileCmp !== 0) return fileCmp;
			return a.locator.line - b.locator.line;
		});
		const top = fs[0];
		if (top === undefined) continue;
		const merged: MergedEntry = {
			term: top.term,
			canonical: top.canonical,
			confidence: top.confidence,
			locator: top.locator,
			superseded: fs.slice(1).map((f) => f.locator),
		};
		entries.push(merged);

		const [term] = key.split("\0");
		if (term === undefined) continue;
		const list = winnersByTerm.get(term);
		if (list) list.push(merged);
		else winnersByTerm.set(term, [merged]);
	}

	for (const [term, ms] of winnersByTerm) {
		if (ms.length <= 1) continue;
		// Multiple distinct canonical targets at the top layer — flag.
		const layers = ms.map((m) => m.locator.layer);
		const maxLayer = Math.max(...layers);
		const tied = ms.filter((m) => m.locator.layer === maxLayer);
		if (tied.length > 1) {
			conflicts.push({
				term,
				candidates: tied.map((m) => ({
					term: m.term,
					canonical: m.canonical,
					confidence: m.confidence,
					locator: m.locator,
				})),
				resolution:
					"ambiguous — multiple canonical targets at the same layer; user must disambiguate via semsql.overrides.yaml",
			});
		}
	}

	entries.sort((a, b) => a.term.localeCompare(b.term));
	return { entries, conflicts };
}
