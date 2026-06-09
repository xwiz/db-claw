# v0.2 Application-Context-First Direction
Date: 2026-06-09. Retained decision record; current work lives in [the plan](v02-semantic-atlas-completion-plan.md).

## Decision
Lead with application-aware extraction. ORM, framework, API, UI, and report code are the closest available semantic specification. Raw DB analysis remains a lower-confidence bootstrap:

`application contract + DB facts + approved memory -> typed plan -> guarded SQL`

## Evidence
- Laravel `mailer_web` grounded `3765/3765` source vocabulary rows over `214` entities; `fraudv` grounded `288/288`; Next.js/Drizzle grounded `174/174`.
- Generated MariaDB/BI probes prove safe execution after intended fields are expressible, not arbitrary end-user accuracy.
- BIRD stays weak without application semantics: first20 `7/20` after fail-closed cleanup.
- Graph v5 stores vocabulary, relationships, samples, conflicts, and metrics; durable correction memory was the missing layer.

## Contract
Keep generated graph evidence, source-controlled governed facts, and mutable resolution memory separate. Never promote a mapping merely because SQL executed. Promotion requires source proof, explicit approval, a trusted saved report/query, or repeated confirmed outcomes; drift makes learned facts stale.

## Direction
1. Make Laravel the reference integration: relationships/keys, scopes, casts, accessors, validation/resources, Filament filters, and report shapes.
2. Retrieve candidates per intent role, enumerate complete plans, and validate types, values, joins, scopes, safety, and result shape.
3. Use read-only probes or typed LLM help only for incomplete evidence; clarify meaningful ambiguity.
4. Evaluate held-out app-aware versus DB-only questions, correction reuse, and drift invalidation.

BIRD remains useful as a raw-DB stress test, not the primary product roadmap.
