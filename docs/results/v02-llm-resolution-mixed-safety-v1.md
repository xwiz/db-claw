# v0.2 LLM Resolution Mixed Safety v1

Date: 2026-06-06. Retained proof for mixed typed-provider safety outcomes.

## Result

PASS. The safety checker distinguishes expected routes from expected
clarify/block outcomes, so mixed provider batches no longer require every case
to emit SQL.

## Evidence

| Batch | Expected | Passed | Failed | Key Boundary |
|---|---:|---:|---:|---|
| live OpenAI fallback replay | `6` | `6` | `0` | `4` routes, `1` clarify, `1` block |
| provider shape contract | `4` | `4` | `0` | `3` routes, `1` shape block |

Artifacts:

- `target/v02/live-provider-openai-batch-v1/safety-gate.json`
- `target/v02/live-provider-openai-batch-v1/safety-gate.md`
- `target/v02/provider-result-shape-contract-v1/safety-gate.json`
- `target/v02/provider-result-shape-contract-v1/safety-gate.md`

## Interpretation

This proves the typed-provider boundary can be regression-tested as a mixed
safety contract: valid routes keep shape/source expectations, valid
clarifications do not produce SQL, invalid typed proposals stay blocked, and
direct provider SQL remains forbidden.

Commands:

```powershell
uv run python -m semsql_eval llm-resolution-safety-gate --summary-json target\v02\live-provider-openai-batch-v1\openai\fallback-batch.json --expectations-json target\v02\live-provider-openai-batch-v1\safety-expectations.json --out-json target\v02\live-provider-openai-batch-v1\safety-gate.json --out-md target\v02\live-provider-openai-batch-v1\safety-gate.md --strict
uv run python -m semsql_eval llm-resolution-safety-gate --summary-json target\v02\provider-result-shape-contract-v1\summary.json --expectations-json target\v02\provider-result-shape-contract-v1\safety-expectations.json --out-json target\v02\provider-result-shape-contract-v1\safety-gate.json --out-md target\v02\provider-result-shape-contract-v1\safety-gate.md --strict
```
