# Real App Framework Probe: mailer_web v2

Date: 2026-06-05

## Result

PASS. A real Laravel app plus local MariaDB schema produced DB-grounded source
vocabulary without sampling row values.

## Evidence

- app: `C:\Users\Son\cowork\mailer_web`
- framework: `laravel`
- database: `mailer_web`
- graph: `target\v02\real-app-framework-mailer-web-v2\app.framework.semsql`
- raw source fragments: `5011`
- source vocab grounded: `3765/3765`
- entities/fields/relationships: `214/3474/156`
- Eloquent model entity aliases: `164`
- source-entity query checks: `3/3`, required `3`
- sample-value rows: `0`
- artifacts: `target\v02\real-app-framework-mailer-web-v2\report.json`, `target\v02\real-app-framework-mailer-web-v2\report.md`

## Change Proven

Eloquent model classes now emit source entity aliases generically. This let
real-app terms such as `access permission`, `action rule`, and `admin campaign
job` route to count SQL over their DB-grounded tables.

## Limits

This is one large Laravel app. It proves source/schema grounding and safe
metadata extraction, not full BI query coverage or broad framework readiness.
