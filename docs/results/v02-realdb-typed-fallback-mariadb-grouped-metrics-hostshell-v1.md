# Real DB MySQL/MariaDB Typed Fallback Probe

- status: `PASS`
- database: `hostshell`
- graph: `target\realdb_typed_fallback_mariadb_grouped_metrics_hostshell_v1\graphs\hostshell.schemaonly.semsql`
- provider: `openai`
- families: `grouped_avg`
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
- expected kinds: `{'grouped_avg': 3}`
- rows retained cases: `0`
- sample-value rows: `0`

## Records

| # | Question | Expected | Source | Provider Calls | Render | Exec | Rows Retained | Expected Match | OK | SQL |
|---:|---|---|---|---:|---|---|---|---|---|---|
| 1 | `which type has the highest average ttl for dns records` | `dns_records.ttl by dns_records.type` | `local` | `0` | `None` | `ok` | `False` | `True` | `True` | <code>SELECT `dns_records`.`type`, AVG(`dns_records`.`ttl`) AS `avg_ttl` FROM `dns_records` WHERE `dns_records`.`ttl` IS NOT NULL GROUP BY `dns_records`.`type` ORDER BY `avg_ttl` DESC LIMIT 1</code> |
| 2 | `which published status has the highest average page order for website pages` | `website_pages.page_order by website_pages.is_published` | `local` | `0` | `None` | `ok` | `False` | `True` | `True` | <code>SELECT `website_pages`.`is_published`, AVG(`website_pages`.`page_order`) AS `avg_page_order` FROM `website_pages` WHERE `website_pages`.`page_order` IS NOT NULL GROUP BY `website_pages`.`is_published` ORDER BY `avg_page_order` DESC LIMIT 1</code> |
| 3 | `which status has the highest average temperature for agents` | `agents.temperature by agents.status` | `local` | `0` | `None` | `ok` | `False` | `True` | `True` | <code>SELECT `agents`.`status`, AVG(`agents`.`temperature`) AS `avg_temperature` FROM `agents` WHERE `agents`.`temperature` IS NOT NULL GROUP BY `agents`.`status` ORDER BY `avg_temperature` DESC LIMIT 1</code> |
