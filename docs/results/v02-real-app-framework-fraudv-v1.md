# Real App Framework Probe: fraudv v1

Date: 2026-06-05

## Result

PASS. A second real Laravel app plus local MariaDB schema produced DB-grounded
source vocabulary without sampling row values.

## Evidence

- app: `C:\Users\Son\cowork\fraudv`
- framework: `laravel`
- database: `fraudv_go`
- graph: `target\v02\real-app-framework-fraudv-v1\app.framework.semsql`
- raw source fragments: `1262`
- source vocab grounded: `288/288`
- entities/fields/relationships: `29/421/38`
- Eloquent model entity aliases: `21`
- source-entity query checks: `5/5`, required `3`
- sample-value rows: `0`
- artifacts: `target\v02\real-app-framework-fraudv-v1\report.json`, `target\v02\real-app-framework-fraudv-v1\report.md`

## Query Checks

- `ai prompt history` -> `SELECT COUNT(*) FROM ai_prompt_histories`
- `bank` -> `SELECT COUNT(*) FROM banks`
- `bank account` -> `SELECT COUNT(*) FROM bank_accounts`
- `bank aml setting` -> `SELECT COUNT(*) FROM bank_aml_settings`
- `bank channel` -> `SELECT COUNT(*) FROM bank_channels`

## Limits

This is metadata/source-vocabulary evidence for a real fraud/operations app.
It does not prove full typed fallback or BI query coverage.
