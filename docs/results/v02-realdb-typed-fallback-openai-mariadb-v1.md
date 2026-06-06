# LLM-Resolution Packet Resolve

This is the first retained live-provider typed fallback proof against a real
MariaDB app schema. OpenAI returned a typed proposal, not SQL. SemSQL rendered
and validated the proposal locally, then executed the selected SQL through the
MariaDB read-only adapter with row retention disabled.

- packet: `target\realdb_typed_fallback_mariadb_v1\fallback-active-mail-user-rate\rejected.packet.json`
- provider: `openai`
- provider calls: `1`
- direct LLM SQL used: `False`
- render valid: `True`
- selected source: `typed_provider`
- status: `selected`

## Selected SQL

```sql
SELECT CAST(SUM(CASE WHEN `mail_users`.`active` = 1 THEN 1 ELSE 0 END) AS DOUBLE) * 100.0 / NULLIF(COUNT(`mail_users`.`id`), 0) AS `percent_active_mail_users` FROM `mail_users`
```

## Result Shape

- kind: `scalar_metric`
- default view: `metric`
- reason: `single aggregate without GROUP BY`

## Execution

- requested: `True`
- engine: `mariadb`
- status: `ok`
- row preview count: `1`
- truncated: `False`
- rows retained: `False`
- policy: selected SQL only after local validation; execution adapter opens a read-only transaction or connection
- target: `mariadb://root:***@127.0.0.1:3306/maildb`
