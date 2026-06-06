/**
 * SemanticSQL extractor SDK.
 *
 * Defines the protocol every framework adapter (Laravel, Next.js, Django,
 * Rails, Vue) implements, plus the merge engine that applies the priority
 * cascade:
 *
 *     Form/Table label > i18n > Filament admin > API resource >
 *         app constant > ORM > DB schema
 *
 * Highest layer wins; lower layers attach as `aliases[]`. Conflicts are
 * deterministic and recorded in `conflict_log`.
 *
 * Vocabulary is **untrusted input** — every fragment is sanitised before it
 * enters the merge engine (see `sanitise.ts`).
 */

export {
	type Canonical,
	type Confidence,
	type Extractor,
	type ExtractCtx,
	type LangIndex,
	type LangIndexEntry,
	type Locator,
	type MetricDefinitionFragment,
	type MetricKind,
	type SemanticFragment,
	SourceLayer,
	type VocabFragment,
} from "./types.js";

export { mergeFragments, type MergeResult } from "./merge.js";
export {
	sanitiseCanonical,
	sanitiseLabel,
	SanitiserError,
} from "./sanitise.js";

export { SDK_VERSION } from "./version.js";
