# LLM-Resolution Fallback Query

This is the first retained real-MariaDB typed fallback proof. It uses a fresh
schema-only `maildb` graph with `0` sample-value rows. Local SemSQL routing
fails closed, then a reviewed typed conditional-rate proposal renders locally
and executes through the MariaDB read-only adapter. The execution artifact
retains no row values.

- question: `what percentage of mail users are active`
- graph: `target\realdb_typed_fallback_mariadb_v1\maildb.schemaonly.semsql`
- status: `selected`
- selected source: `typed_fallback`
- local routed: `False`
- local stage: `needs_model`
- provider: `none`
- provider calls: `0`
- direct LLM SQL used: `False`
- fallback render valid: `True`

## Artifacts

- query_frame: `target\realdb_typed_fallback_mariadb_v1\fallback-active-mail-user-rate\query-frame.json`
- packet: `target\realdb_typed_fallback_mariadb_v1\fallback-active-mail-user-rate\rejected.packet.json`
- openai_request: `target\realdb_typed_fallback_mariadb_v1\fallback-active-mail-user-rate\openai-request.json`
- provider_result: `-`
- render: `target\realdb_typed_fallback_mariadb_v1\fallback-active-mail-user-rate\render.json`
- execution: `target\realdb_typed_fallback_mariadb_v1\fallback-active-mail-user-rate\execution.json`
- summary_json: `target\realdb_typed_fallback_mariadb_v1\fallback-active-mail-user-rate\fallback-query.json`
- summary_markdown: `target\realdb_typed_fallback_mariadb_v1\fallback-active-mail-user-rate\fallback-query.md`

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

## Selected SQL

```sql
SELECT CAST(SUM(CASE WHEN `mail_users`.`active` = 1 THEN 1 ELSE 0 END) AS DOUBLE) * 100.0 / NULLIF(COUNT(`mail_users`.`id`), 0) AS `active_mail_user_rate` FROM `mail_users`
```
