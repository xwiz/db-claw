# v0.2 BIRD50 Atlas Description v1
Date: 2026-06-07. Retained checkpoint evidence; live status remains in
[v02-current-status.md](v02-current-status.md).

## Scope
Fresh first-50 BIRD dev run using adjacent `database_description/` CSVs as
DB-only SemanticAtlas evidence. The eval runner now writes description-aware
SQLite caches as `*.desc.semsql` so old schema-only caches are not reused.

Artifacts:

- report: `target/v02/current-bird50-atlas-desc-v1/report.json`
- diagnosis: `target/v02/current-bird50-atlas-desc-v1/diagnosis.md`
- graph: `target/v02/current-bird50-atlas-desc-v1/graphs/california_schools.desc.semsql`

## Result
- completed checkpoint examples: `50/50`
- clean final write: `false` (outer command timed out after the checkpoint)
- correct: `3`
- exec_acc: `6.00%`
- final SQL emitted: `3`
- final SQL wrong: `0`
- route-used wrong SQL: `0`

The description-enriched graph contains `3` entities, `89` fields, `2`
relationships, `85` sample-value fields, `266` vocabulary rows, and `118`
field-scoped scope predicates. Accuracy did not improve from v6, but the
run confirms the BIRD blocker is composition, not missing description ingest.

## Targeted Probe
After the runtime date/scope patch, the query:

`Please list the phone numbers of the direct charter-funded schools that are opened after 2000/1/1.`

binds:

- `schools.charter = 1`
- `schools.fundingtype = directly funded`
- `schools.opendate > 2000/1/1`

It still fails closed because the route has not composed the required `frpm`
join/projection contract. Next work is join/table selection and projection
pruning over DB-only atlas evidence, not more value alias patches.
