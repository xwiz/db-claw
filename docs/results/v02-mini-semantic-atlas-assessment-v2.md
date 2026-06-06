# Mini SemanticAtlas Practical Assessment

Status: historical evidence for the atlas direction. Runtime stoplights were
superseded by `v02-pathway-benchmark-bound-plan-v30.md`.

## Question

Can a compact schema/value/metric atlas recover enough evidence to justify a
typed planner rather than a larger free-form SQL model?

## Signal

| mode | route plan-ready | non-route fail-closed | wrong-accept risk | table recall | field recall | value-field recall | date hit | metric hit |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| raw schema | `2/30` | `14/14` | `0` | `91.67%` | `26.06%` | `75.00%` | `56.67%` | `90.00%` |
| mini atlas | `16/30` | `14/14` | `0` | `100.00%` | `58.17%` | `81.67%` | `100.00%` | `100.00%` |

The atlas lifted route plan-readiness by `14` cases without adding wrong-accept
risk, but it was not yet a SQL runtime.

## Lessons Kept

- Schema names alone are not enough; field roles, sample values, aliases, and
  metric/date hints matter.
- Main remaining gaps were intent detection, field recall, and value-to-field
  binding.
- Plan-ready evidence must still pass BoundQueryPlan validation before SQL.

## Retained Detail

Use the raw generated assessment artifact when case-level replay is needed:
`target/semantic_atlas_assessment_v2/report.json`.
