# v0.2 Expectation Ladder

Date: 2026-06-02

Historical expectation memo. Current decision and regression numbers live in
[v02-current-status.md](v02-current-status.md) and
[v02-evidence-ledger.md](v02-evidence-ledger.md).

## Preserved Lessons

| Layer | Durable expectation |
|---|---|
| Runtime | tests/clippy must pass before promotion |
| Shortcuts | no DB-family literal maps or example routers |
| Canaries | grounded lookups route; ambiguous prompts fail closed |
| Real apps | schema/source vocabulary must ground practical questions |
| Benchmarks | BIRD is research until current gates improve |

## Suspicious Improvements

Treat a result as suspicious if it:

- improves a named benchmark DB slice by adding exact DB-family rules;
- improves only a small sampled smoke but not full-dev behavior;
- requires natural-language examples hardcoded in runtime code;
- routes complex BI questions without schema-derived metrics or typed plan
  validation.

## Next Good Lift

These lessons fed the current loop:

1. schema summary packets for rejected queries;
2. typed LLM plan proposal and validation;
3. reusable QueryFrame rules for BI shapes such as grouped aggregates, date
   windows, top-k, and intersections;
4. broader live-provider and real-app evidence with `0` wrong accepted SQL.
