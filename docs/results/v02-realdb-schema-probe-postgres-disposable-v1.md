# Real DB Postgres Schema-Only Probe

Note: this report was captured from a disposable read-only Postgres database.
After capture, the temporary DB, role, SSH tunnel, and exact narrow HBA rule
were removed.

- status: `PASS`
- database: `semsql_probe_20260605_0952`
- graph: `target\realdb_schema_probe_postgres_disposable_v1\graphs\semsql_probe_20260605_0952.schemaonly.semsql`
- high-risk schema: `False`
- safety mode: `schema-only extraction; required count-only execution; optional governed analytics execution; no result values retained`

## Summary

- questions: `6`
- required contract: `3/3`
- routed: `6`
- count-only routes: `3`
- executed count-only queries: `3`
- analytics probes: `3`
- analytics ok: `3/3`
- analytics gaps: `0`
- executed governed analytics queries: `3`
- execution errors: `0`
- safe not-executed routes/rejects: `0`
- semantic ok or safe not-executed: `6/6`
- needs review: `0`
- sample-value rows: `0`
- stages: `{'stage_0a': 6}`

## Records

| # | Question | Stage | Kind | Expected | Actual | Shape | Required | Executed | Exec | Review | SQL |
|---:|---|---|---|---|---|---|---:|---:|---|---|---|
| 1 | `how many customers` | `stage_0a` | `table_count` | `customers` | `customers` | `table_count` | `True` | `True` | `ok` | `ok` | <code>SELECT COUNT(*) FROM "customers"</code> |
| 2 | `how many orders` | `stage_0a` | `table_count` | `orders` | `orders` | `table_count` | `True` | `True` | `ok` | `ok` | <code>SELECT COUNT(*) FROM "orders"</code> |
| 3 | `how many support tickets` | `stage_0a` | `table_count` | `support_tickets` | `support_tickets` | `table_count` | `True` | `True` | `ok` | `ok` | <code>SELECT COUNT(*) FROM "support_tickets"</code> |
| 4 | `how many orders created yesterday` | `stage_0a` | `date_count` | `orders.created_at` | `orders.created_at` | `date_count` | `False` | `True` | `ok` | `ok` | <code>SELECT COUNT(*) FROM "orders" WHERE "orders"."created_at" = '2026-06-04'</code> |
| 5 | `count customers by status` | `stage_0a` | `group_count` | `customers.status` | `customers.status` | `group_count` | `False` | `True` | `ok` | `ok` | <code>SELECT "customers"."status", COUNT("customers"."id") AS "customer_count" FROM "customers" GROUP BY "customers"."status" ORDER BY "customer_count" DESC</code> |
| 6 | `average resolution hours for support tickets` | `stage_0a` | `avg` | `support_tickets.resolution_hours` | `support_tickets.resolution_hours` | `avg` | `False` | `True` | `ok` | `ok` | <code>SELECT AVG("support_tickets"."resolution_hours") AS "avg_resolution_hours" FROM "support_tickets" WHERE "support_tickets"."resolution_hours" IS NOT NULL</code> |
