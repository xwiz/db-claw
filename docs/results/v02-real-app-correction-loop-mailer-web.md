# Real-App Correction Loop: mailer_web (Laravel)

- **status**: PASS
- **question**: `plans with enterprise`
- **approved target**: `plans.name=Enterprise`
- **date**: 2026-06-12

## Correction Loop

| Step | Result |
|------|--------|
| Extract | PASS (214 entities, 3474 fields, 307 relationships) |
| First query → ask_user | PASS (`ambiguous_unscoped_value_field`, candidates: `plans.name=Enterprise`) |
| Resolve with approval | PASS (mapping: `enterprise → enum_value:plans.name:Enterprise`, confidence 0.9) |
| Rerun → execute | PASS (SQL: ``SELECT `plans`.`name` FROM `plans` WHERE `plans`.`name` = 'Enterprise'``) |

## Detail

The extract used the running MariaDB for mailer_web at `mysql://root:password@127.0.0.1:3306/mailer_web`.

The query `plans with enterprise` was ambiguous because "enterprise" matches `plans.name` (sample value "Enterprise")
and `plans.code` (sample value "enterprise"). The Stage 0a pre-resolver chose `ask_user` with
`ambiguous_unscoped_value_field` and candidate `plans.name=Enterprise`.

After approval:
- `semsql resolve` saved the mapping to resolution memory
- Rerunning with `--resolution-memory` immediately produced `execute` with the correct SQL

## Artifacts

- Graph: `target/v02/mailer-web-probe/app.semsql`
- Rejection packet: `C:\Users\Son\AppData\Local\Temp\opencode\reject-packet.json`
- Resolution memory: `C:\Users\Son\AppData\Local\Temp\opencode\resolution.yaml`
- Probe report: this file

## GO_LIVE.md

All 16 items now checked. The correction-loop gate is cleared.
