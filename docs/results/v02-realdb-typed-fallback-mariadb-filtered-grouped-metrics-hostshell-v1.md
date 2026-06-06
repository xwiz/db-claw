# Real DB MySQL/MariaDB Typed Fallback Probe

- status: `PASS`
- database: `hostshell`
- graph: `target\realdb_typed_fallback_mariadb_filtered_grouped_metrics_hostshell_v1\graphs\hostshell.schemaonly.semsql`
- provider: `openai`
- families: `filtered_grouped_avg`
- high-risk schema: `False`
- safety mode: `schema-only extraction; no sample values; provider may propose typed plans only; SQL is locally rendered/validated and executed read-only with row values discarded`

## Summary

- questions: `2`
- selected SQL: `2/2`
- typed fallback selected: `0/2`
- local selected: `2`
- provider calls: `0`
- provider errors: `0`
- render errors: `0`
- execution ok: `2/2`
- expected table/field matches: `2/2`
- expected kinds: `{'filtered_grouped_avg': 2}`
- rows retained cases: `0`
- sample-value rows: `0`

## Records

| # | Question | Expected | Source | Provider Calls | Render | Exec | Rows Retained | Expected Match | OK | SQL |
|---:|---|---|---|---:|---|---|---|---|---|---|
| 1 | `which model has the highest average temperature for agents that have memory enabled` | `agents.temperature by agents.model where agents.memory_enabled = 1` | `local` | `0` | `None` | `ok` | `False` | `True` | `True` | <code>SELECT `agents`.`model`, AVG(`agents`.`temperature`) AS `avg_temperature` FROM `agents` INNER JOIN `tenants` ON `agents`.`tenant_id` = `tenants`.`id` WHERE `agents`.`memory_enabled` = 1 AND `tenants`.`is_active` = 1 AND `agents`.`temperature` IS NOT NULL GROUP BY `agents`.`model` ORDER BY `avg_temperature` DESC LIMIT 1</code> |
| 2 | `which status has the highest average temperature for agents that have memory enabled` | `agents.temperature by agents.status where agents.memory_enabled = 1` | `local` | `0` | `None` | `ok` | `False` | `True` | `True` | <code>SELECT `agents`.`status`, AVG(`agents`.`temperature`) AS `avg_temperature` FROM `agents` INNER JOIN `tenants` ON `agents`.`tenant_id` = `tenants`.`id` WHERE `agents`.`memory_enabled` = 1 AND `tenants`.`is_active` = 1 AND `agents`.`temperature` IS NOT NULL GROUP BY `agents`.`status` ORDER BY `avg_temperature` DESC LIMIT 1</code> |
