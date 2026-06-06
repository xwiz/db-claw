# Real DB MySQL/MariaDB Typed Fallback Probe

- status: `PASS`
- database: `hostshell`
- graph: `target\realdb_typed_fallback_mariadb_multi_joined_grouped_metrics_hostshell_v2\graphs\hostshell.schemaonly.semsql`
- provider: `none`
- families: `multi_joined_filtered_grouped_avg`
- high-risk schema: `False`
- safety mode: `schema-only extraction; no sample values; provider may propose typed plans only; SQL is locally rendered/validated and executed read-only with row values discarded`

## Summary

- questions: `3`
- selected SQL: `3/3`
- typed fallback selected: `0/3`
- local selected: `3`
- provider calls: `0`
- provider errors: `0`
- render errors: `0`
- execution ok: `3/3`
- expected table/field matches: `3/3`
- expected kinds: `{'multi_joined_filtered_grouped_avg': 3}`
- rows retained cases: `0`
- sample-value rows: `0`

## Records

| # | Question | Expected | Source | Provider Calls | Render | Exec | Rows Retained | Expected Match | OK | SQL |
|---:|---|---|---|---:|---|---|---|---|---|---|
| 1 | `which tenants primary color has the highest average priority for dns records` | `dns_records.priority by tenants.primary_color via dns_records.domain_id=domains.id -> domains.tenant_id=tenants.id` | `local` | `0` | `None` | `ok` | `False` | `True` | `True` | <code>SELECT `tenants`.`primary_color`, AVG(`dns_records`.`priority`) AS `avg_priority` FROM `dns_records` INNER JOIN `domains` ON `dns_records`.`domain_id` = `domains`.`id` INNER JOIN `tenants` ON `domains`.`tenant_id` = `tenants`.`id` WHERE `dns_records`.`priority` IS NOT NULL GROUP BY `tenants`.`primary_color` ORDER BY `avg_priority` DESC LIMIT 1</code> |
| 2 | `which tenants plan has the highest average priority for dns records` | `dns_records.priority by tenants.plan via dns_records.domain_id=domains.id -> domains.tenant_id=tenants.id` | `local` | `0` | `None` | `ok` | `False` | `True` | `True` | <code>SELECT `tenants`.`plan`, AVG(`dns_records`.`priority`) AS `avg_priority` FROM `dns_records` INNER JOIN `domains` ON `dns_records`.`domain_id` = `domains`.`id` INNER JOIN `tenants` ON `domains`.`tenant_id` = `tenants`.`id` WHERE `dns_records`.`priority` IS NOT NULL GROUP BY `tenants`.`plan` ORDER BY `avg_priority` DESC LIMIT 1</code> |
| 3 | `which tenants name has the highest average priority for dns records` | `dns_records.priority by tenants.name via dns_records.domain_id=domains.id -> domains.tenant_id=tenants.id` | `local` | `0` | `None` | `ok` | `False` | `True` | `True` | <code>SELECT `tenants`.`name`, AVG(`dns_records`.`priority`) AS `avg_priority` FROM `dns_records` INNER JOIN `domains` ON `dns_records`.`domain_id` = `domains`.`id` INNER JOIN `tenants` ON `domains`.`tenant_id` = `tenants`.`id` WHERE `dns_records`.`priority` IS NOT NULL GROUP BY `tenants`.`name` ORDER BY `avg_priority` DESC LIMIT 1</code> |
