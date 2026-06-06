# MySQL/MariaDB Sharding Audit

- status: `REVIEW`
- database: `mailer_web`
- safety: `information_schema/source-only audit; no table data sampled`

## Summary

- tables: `214`
- shard families: `12`
- shard tables: `30`
- active shard tables: `6`
- active ambiguous families: `2`
- malformed shard tables: `1`
- nested shard tables: `1`
- source expected families: `9`
- needs review: `14`

## Source Hints

- source inspected: `True`
- configured shard models: `AppFile, AppForm, Contact, Employee, Mail, MailAlias, MailAttachment, Task, TicketThread`
- expected base tables: `app_files, app_forms, contacts, employees, mail_aliases, mail_attachments, mails, tasks, ticket_threads`

## Families

| Base | Anchor | Shards | Rows | Column drift | Active physical | Missing cols | Extra cols | Review |
|---|---|---:|---:|---:|---|---|---|---|
| `app_file_versions` | `organizations` | `1` | `73` | `6` | `app_file_versions_organizations_1` | `` | `` | `type_drift` |
| `app_files` | `organizations` | `3` | `110` | `33` | `app_files_organizations_1` | `` | `` | `type_drift` |
| `app_forms` | `organizations` | `3` | `0` | `42` | `-` | `` | `` | `type_drift` |
| `contacts` | `organizations` | `3` | `0` | `45` | `-` | `` | `` | `type_drift` |
| `employees` | `organizations` | `3` | `10` | `66` | `employees_organizations_1, employees_organizations_3` | `` | `` | `type_drift, active_table_ambiguity` |
| `employees_organizations_1` | `organizations` | `1` | `0` | `0` | `employees_organizations_1` | `agent_metadata, kind` | `` | `missing_columns` |
| `mail_aliases` | `organizations` | `3` | `0` | `15` | `-` | `` | `` | `type_drift` |
| `mail_attachments` | `organizations` | `3` | `0` | `27` | `-` | `` | `` | `type_drift` |
| `mails` | `organizations` | `3` | `183` | `110` | `mails_organizations_1, mails_organizations_3` | `vvs_agent_id, vvs_key_version, vvs_trust_level` | `` | `missing_columns, type_drift, active_table_ambiguity` |
| `tasks` | `organizations` | `3` | `0` | `24` | `-` | `` | `` | `type_drift` |
| `ticket_threads` | `organizations` | `3` | `0` | `36` | `-` | `` | `` | `type_drift` |
| `user_corrections` | `organizations` | `1` | `0` | `5` | `-` | `` | `` | `type_drift` |

## Malformed Shard Tables

- `mails_organizations_`: base `mails`, anchor `organizations`, reason `missing_shard_id`

## Nested Shard Tables

- `employees_organizations_1_organizations_1`

## DB Families Not Listed In Source Config

- `app_file_versions`
- `employees_organizations_1`
- `user_corrections`
