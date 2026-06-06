# Real DB Postgres Typed Fallback Probe

Note: this report was captured from a disposable read-only Postgres database.
After capture, the temporary DB, role, SSH tunnel, and exact narrow HBA rule
were removed.

- status: `PASS`
- database: `semsql_probe_20260605_0952`
- graph: `target\realdb_typed_fallback_postgres_disposable_v1\graphs\semsql_probe_20260605_0952.schemaonly.semsql`
- provider: `none`
- families: `rate, grouped_avg, filtered_grouped_avg, joined_filtered_grouped_avg`
- high-risk schema: `False`
- safety mode: `schema-only extraction; no sample values; provider may propose typed plans only; SQL is locally rendered/validated and executed read-only with row values discarded`

## Summary

- questions: `4`
- selected SQL: `4/4`
- typed fallback selected: `0/4`
- local selected: `4`
- provider calls: `0`
- provider errors: `0`
- provider readiness: `4/4 configured, 0 unconfigured; providers={'none': 4}, missing_env={}`
- render errors: `0`
- execution ok: `4/4`
- expected table/field matches: `4/4`
- expected kinds: `{'conditional_rate': 2, 'grouped_avg': 1, 'filtered_grouped_avg': 1}`
- rows retained cases: `0`
- sample-value rows: `0`
- packet schema evidence ok: `True`
- full rejected packet schema evidence: `0 checked, 0 missing records, 0 missing facts`
- compact provider request schema evidence: `0 checked, 0 missing records, 0 missing facts`

## Records

| # | Question | Expected | Source | Provider Calls | Render | Exec | Rows Retained | Expected Match | OK | SQL |
|---:|---|---|---|---:|---|---|---|---|---|---|
| 1 | `what percentage of orders are paid` | `orders.is_paid` | `local` | `0` | `None` | `ok` | `False` | `True` | `True` | <code>SELECT SUM(CASE WHEN "orders"."is_paid" = true THEN 1 ELSE 0 END) * 100.0 / NULLIF(COUNT("orders"."id"), 0) AS is_paid_measure_1_rate FROM "orders"</code> |
| 2 | `what percentage of customers are active` | `customers.is_active` | `local` | `0` | `None` | `ok` | `False` | `True` | `True` | <code>SELECT SUM(CASE WHEN "customers"."is_active" = true THEN 1 ELSE 0 END) * 100.0 / NULLIF(COUNT("customers"."id"), 0) AS is_active_measure_1_rate FROM "customers"</code> |
| 3 | `which status has the highest average amount for orders` | `orders.amount by orders.status` | `local` | `0` | `None` | `ok` | `False` | `True` | `True` | <code>SELECT "orders"."status", AVG("orders"."amount") AS "avg_amount" FROM "orders" WHERE "orders"."amount" IS NOT NULL GROUP BY "orders"."status" ORDER BY "avg_amount" DESC LIMIT 1</code> |
| 4 | `which status has the highest average amount for orders that are paid` | `orders.amount by orders.status where orders.is_paid = 1` | `local` | `0` | `None` | `ok` | `False` | `True` | `True` | <code>SELECT "orders"."status", AVG("orders"."amount") AS "avg_amount" FROM "orders" WHERE "orders"."is_paid" = TRUE AND "orders"."amount" IS NOT NULL GROUP BY "orders"."status" ORDER BY "avg_amount" DESC LIMIT 1</code> |
