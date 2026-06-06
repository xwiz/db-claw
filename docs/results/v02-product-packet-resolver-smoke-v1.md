# LLM-Resolution Packet Resolve

- packet: `target\llm_resolution_capture\schema_alias_pq004_v1\rejected.packet.json`
- provider: `none`
- provider calls: `0`
- direct LLM SQL used: `False`
- render valid: `True`
- selected source: `typed_proposal`
- status: `selected`

## Selected SQL

```sql
SELECT "staff_members"."staff_name", COUNT("support_cases"."id") AS "count" FROM "support_cases" JOIN "staff_members" ON "support_cases"."assigned_staff_id" = "staff_members"."id" WHERE "support_cases"."case_priority" = 'high' AND "support_cases"."case_status" = 'resolved' GROUP BY "staff_members"."staff_name" ORDER BY COUNT("support_cases"."id") DESC LIMIT 1
```

## Result Shape

- kind: `categorical_chart`
- default view: `chart`
- reason: `one grouped dimension with one or more measures`
- chartjs type: `bar`

## Execution

- requested: `True`
- engine: `sqlite`
- status: `ok`
- row preview count: `1`
- truncated: `False`
- policy: selected SQL only after local validation; SQLite is opened read-only
- target: `sqlite:///C:/dev/db-claw/target/pathway_decision_benchmark_schema_alias_v9/platform/database/growth_ops_semantic_alias/growth_ops_semantic_alias.sqlite`
- execution source: `db_url`

### Result Preview

| staff_name | count |
| --- | --- |
| Jon Bell | 2 |
