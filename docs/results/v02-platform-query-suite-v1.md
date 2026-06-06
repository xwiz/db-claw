# Platform NL-to-SQL Comparison Suite

- suite: `platform-comparison-v1`
- db_id: `growth_ops`
- sqlite: `target\platform_query_suite_v1\database\growth_ops\growth_ops.sqlite`
- connection URI: `sqlite:///target/platform_query_suite_v1/database/growth_ops/growth_ops.sqlite`

## Purpose

Compare deterministic SemSQL QueryFrame routing with agentic NL-to-SQL systems on the same schema and questions.

## How To Use

1. Point the target NL-to-SQL system at the SQLite database.
2. Ask each question from `questions.jsonl`.
3. Compare SQL execution to `expected.sql` for `route` cases.
4. Count `clarify`, `reject`, and `known_gap` cases separately.

## Case Matrix

| ID | Disposition | Family | Difficulty | Question |
|---|---|---|---|---|
| `pq001` | `route` | `multi_join_filter_projection` | `medium` | List active enterprise accounts in EMEA with their owner |
| `pq002` | `route` | `date_range_count` | `medium` | How many active accounts signed up in February 2024? |
| `pq003` | `route` | `entity_value_join_aggregate_date` | `medium` | Total paid invoice amount for Acme Cloud in February 2024 |
| `pq004` | `route` | `topk_group_count` | `medium` | Which support agent resolved the most high priority tickets? |
| `pq005` | `route` | `multi_join_group_avg` | `hard` | Average resolution hours for enterprise accounts by support agent |
| `pq006` | `route` | `two_fact_intersection` | `hard` | Show accounts with overdue invoices and open tickets |
| `pq007` | `route` | `anti_join_temporal` | `hard` | Which active accounts have no login events after March 1 2024? |
| `pq008` | `known_gap` | `ratio_conditional_aggregate` | `hard` | What percentage of active accounts are enterprise accounts? |
| `pq009` | `route` | `grouped_metric_comparison` | `hard` | Compare February paid invoice totals for EMEA and APAC |
| `pq010` | `route` | `boolean_join_filter` | `medium` | List accounts owned by inactive agents |
| `pq011` | `clarify` | `ambiguous_projection` | `easy` | Show status |
| `pq012` | `clarify` | `ambiguous_entity` | `easy` | Show open things |
| `pq013` | `reject` | `causal_analysis` | `hard` | Why did revenue drop in March? |
| `pq014` | `reject` | `unsafe_action` | `easy` | Email all accounts with overdue invoices |
| `pq015` | `reject` | `row_dump` | `easy` | List every ticket with all columns |
| `pq016` | `clarify` | `undefined_business_metric` | `medium` | Which customer is healthiest? |
| `pq017` | `route` | `structured_identifier` | `easy` | Find account ACME-001 |
| `pq018` | `route` | `event_filter_join` | `medium` | Accounts with a cancellation event |

## Interpretation

- `route`: expected to produce executable SQL over the provided schema.
- `clarify`: expected to ask a smaller disambiguation question.
- `reject`: expected to refuse unsafe/non-SQL action.
- `known_gap`: useful stress case, but not a current SemSQL acceptance target.
