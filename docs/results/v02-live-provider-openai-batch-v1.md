# v0.2 Live Provider OpenAI Batch v1

Date: 2026-06-05. Retained typed-fallback replay; current status lives in
[v02-current-status.md](v02-current-status.md).

## Result

Saved OpenAI typed proposals were replayed locally over six real-schema packets;
no new provider calls were made and no direct provider SQL was accepted.

| Signal | Value |
|---|---:|
| packets | `6` |
| initial selected | `5` typed fallback |
| initial unresolved | `1` schema-path clarification |
| post-choice selected | `6/6` typed fallback |
| errors | `0` |
| provider calls | `0` replay |
| post-choice fallback render valid | `6/6` |
| post-choice read-only execution | `6/6`, rows discarded |

Recovered lifecycle case: `paid invoices` maps to `paid_at IS NOT NULL` only
when one matching event timestamp exists and no backed `status = paid` value
does. Schema-path ambiguity now stays closed until a structured option id is
fed back; both single-choice proofs and the mixed batch executed read-only with
rows discarded.

## Generic Fixes

- Provider null syntax and SQL date/time field roles are normalized locally.
- Lifecycle-existence clarifications promote only with single-field evidence.
- Schema-path clarifications expose candidate fields, relationships, and
  structured options; selected options rewrite typed joins only.
- Batch fallback supports read-only execution checks with row discard and
  per-packet DB URL maps for mixed real-schema packet sets; execution failures
  are bucketed by generic DB/runtime cause.
- Provider SQL/raw SQL overrides remain disallowed; issues are ASCII-normalized.

## Replay

- batch options: `target/v02/live-provider-openai-batch-v1/replay-batch-after-schema-path-options`
- post-choice batch: `target/v02/live-provider-openai-batch-v1/replay-batch-after-choice-map`
- mixed MariaDB exec: `target/v02/live-provider-openai-batch-v1/replay-batch-after-choice-map-exec`
- single schema-path/lifecycle proofs live under
  `target/v02/live-provider-openai-batch-v1/replay-after-*`

Verification: `test_llm_resolution.py` `72/72`, CLI fallback subset `53/53`,
realdb shape subset `12/12`, mixed MariaDB replay execution `6/6`, Ruff
focused check, and `uv run mypy python`.
