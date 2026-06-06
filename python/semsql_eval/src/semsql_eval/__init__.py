"""SemanticSQL evaluation harness.

Suites:

- ``spider``: Spider 1.0 dev/test (exec-acc, exact match).
- ``spider2``: Spider 2.0-lite (out-of-scope for tiny cascade — reported but
  not gated in CI).
- ``bird``: BIRD dev (exec-acc).
- ``per_stage``: per-stage accuracy (Stage 0a, 0b, 1, 2, 3) — the cascade is
  debuggable per stage so we measure each independently.
- ``adversarial``: SQL injection in NL, vocabulary collisions, hostile lang
  fragments, prompt-injection escape attempts.
- ``bypass``: mandatory-filter bypass test cases (CTE re-binding, UNION
  branches, lateral joins, recursive CTEs, comment-based bypass,
  multi-statement smuggling, writable-CTE smuggling).

The bypass suite is a hard safety gate: any failure ships nothing.
"""

__version__ = "0.1.0.dev0"
