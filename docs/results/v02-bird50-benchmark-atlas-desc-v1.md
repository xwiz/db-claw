# v0.2 BIRD50 Atlas Description v1
Date: 2026-06-08. Retained checkpoint evidence; live status remains in
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
After the runtime date/scope/projection patch, the query:

`Please list the phone numbers of the direct charter-funded schools that are opened after 2000/1/1.`

binds:

- `schools.charter = 1`
- `schools.fundingtype = directly funded`
- `schools.opendate > 2000/1/1`
- projection `schools.phone`
- no implicit date projection or date ordering

CLI artifact:
`target/v02/current-bird50-atlas-desc-v1/probe-q4-frame-after-projection-prune.json`.

The local bound plan is now valid for the graph shape and emits:

`SELECT "schools"."Phone" FROM "schools" WHERE "schools"."Charter" = 1 AND "schools"."FundingType" = 'directly funded' AND "schools"."OpenDate" > '2000-01-01'`

The first rerun exposed a wrong accepted SQL because the local route selected
`schools.FundingType`/`schools.Charter` while BIRD gold uses the related
`frpm` fields. The runtime now fails closed when a related field has compatible
value evidence and materially stronger label evidence.

Slice artifact:
`target/v02/bird-projection-fix-slice/report-after-ambiguity-guard-noqf.json`.

Slice result for indexes `2,4,7,13`: `3/4` correct, `0` wrong accepted, `1`
bailed as `ambiguous_related_predicate_field`. The next root cause is table/join
composition over DB-only atlas evidence. A separate production gap is graph
load/startup latency: the same California graph can take more than a minute to
answer a single rank lookup from a cold CLI process.
