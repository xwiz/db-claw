# Real App Framework Metric Probe: elbiblio_api v2

Date: 2026-06-06. Retained proof for a second real Laravel app plus local
MariaDB schema.

## Result

PASS. Source vocabulary grounded fully, five source-entity queries routed, and
three authored metrics were accepted as either deterministic local SQL or
bounded metric packets.

## Evidence

- app: `C:\Users\Son\cowork\elbiblio_api`
- framework/database: `laravel` / `el_biblio`
- graph: `target\v02\real-app-framework-elbiblio-api-metrics-v2\app.framework.semsql`
- raw source fragments: `1498`
- source vocab grounded: `909/909`
- entities/fields/relationships: `94/1060/113`
- metric definitions: `3`
- metric checks: `3/3`
- source-entity query checks: `5/5`, required `3`
- sample-value rows: `0`
- artifacts: `target\v02\real-app-framework-elbiblio-api-metrics-v2\report.json`,
  `target\v02\real-app-framework-elbiblio-api-metrics-v2\report.md`

## Change Proven

- `average_game_score` routed locally to `AVG(game_scores.score)`.
- `daily_anchor_completion_rate` produced a bounded metric packet.
- `unique_game_players` produced a bounded distinct-count metric packet over
  `game_scores.user_id`.

## Limits

This is a metadata and metric-contract probe. It does not sample table rows or
prove arbitrary app semantics.
