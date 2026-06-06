# Platform Query Suite SemSQL Baseline v1

Date: 2026-06-02

Status: historical pre-state-machine baseline. Superseded by
`v02-platform-query-suite-semsql-state-machine-v1.md` and later by
`v02-pathway-benchmark-bound-plan-v30.md`.

## Signal

- route targets: `10`
- final SQL exec-correct: `0`
- final SQL exec-mismatch: `10`
- errors/timeouts: `0/0`
- final stage: Stage 3 for all route targets

Graph routing was useful but not promoted: two graph candidates were already
execution-equivalent (`pq003`, `pq006`). Stage 3 then generated plausible but
wrong SQL.

## Lessons Kept

- Fail closed before model fallback when a graph route is rejected or not
  promoted.
- Prefer display fields and support multi-projection requests.
- Normalize month/date phrases before value binding.
- Add typed grouped aggregate, comparison, event, intersection, and reject /
  clarify frames.
- LLM fallback must return typed proposals for local validation, not SQL.

## Retained Detail

Route report:
`target/platform_query_suite_v1/semsql-route-report-release.json`.
