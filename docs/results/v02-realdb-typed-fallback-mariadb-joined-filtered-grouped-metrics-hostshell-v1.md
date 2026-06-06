# Real DB MySQL/MariaDB Typed Fallback Probe

- status: `PASS`
- database: `hostshell`
- graph: `target\realdb_typed_fallback_mariadb_joined_filtered_grouped_metrics_hostshell_v1\graphs\hostshell.schemaonly.semsql`
- provider: `none`
- families: `joined_filtered_grouped_avg`
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
- expected kinds: `{'joined_filtered_grouped_avg': 3}`
- rows retained cases: `0`
- sample-value rows: `0`

## Records

| # | Question | Expected | Source | Provider Calls | Render | Exec | Rows Retained | Expected Match | OK | SQL |
|---:|---|---|---|---:|---|---|---|---|---|---|
| 1 | `which agents model has the highest average message count for agent conversations` | `agent_conversations.message_count by agents.model via agent_conversations.agent_id = agents.id` | `local` | `0` | `None` | `ok` | `False` | `True` | `True` | <code>SELECT `agents`.`model`, AVG(`agent_conversations`.`message_count`) AS `avg_message_count` FROM `agent_conversations` INNER JOIN `agents` ON `agent_conversations`.`agent_id` = `agents`.`id` WHERE `agent_conversations`.`message_count` IS NOT NULL GROUP BY `agents`.`model` ORDER BY `avg_message_count` DESC LIMIT 1</code> |
| 2 | `which tenants plan has the highest average generation time ms for logos` | `logos.generation_time_ms by tenants.plan via logos.tenant_id = tenants.id` | `local` | `0` | `None` | `ok` | `False` | `True` | `True` | <code>SELECT `tenants`.`plan`, AVG(`logos`.`generation_time_ms`) AS `avg_generation_time_ms` FROM `logos` INNER JOIN `tenants` ON `logos`.`tenant_id` = `tenants`.`id` WHERE `logos`.`generation_time_ms` IS NOT NULL GROUP BY `tenants`.`plan` ORDER BY `avg_generation_time_ms` DESC LIMIT 1</code> |
| 3 | `which agents provider has the highest average message count for agent conversations` | `agent_conversations.message_count by agents.provider via agent_conversations.agent_id = agents.id` | `local` | `0` | `None` | `ok` | `False` | `True` | `True` | <code>SELECT `agents`.`provider`, AVG(`agent_conversations`.`message_count`) AS `avg_message_count` FROM `agent_conversations` INNER JOIN `agents` ON `agent_conversations`.`agent_id` = `agents`.`id` WHERE `agent_conversations`.`message_count` IS NOT NULL GROUP BY `agents`.`provider` ORDER BY `avg_message_count` DESC LIMIT 1</code> |
