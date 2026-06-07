# Contributor Docs

This directory is for contributor-facing notes: how the system fits together,
how to package it, and how to read current evidence without committing raw
artifacts.

## Read These First

- [Architecture](ARCHITECTURE.md): runtime flow, component map, safety path.
- [Comparisons](COMPARISONS.md): Vanna history, Microsoft demo comparison,
  paid alternatives, and risk classes.
- [Contributing](CONTRIBUTING.md): local checks, docs rules, artifact rules.
- [Go-Live Packaging](GO_LIVE.md): npm launcher, release binaries, framework
  extractor packaging.
- [Results Index](results/README.md): current scorecards and retained Markdown
  summaries.

## Docs Policy

Keep public docs useful to a new contributor:

- lead with what can be run or changed;
- keep benchmark history summarized, not copied into every file;
- commit Markdown summaries, not raw JSON reports;
- keep local database names, generated paths, and private schema evidence out of
  public docs unless the file is explicitly a sanitized result summary.
- keep active status/plan docs compact and link retained evidence instead of
  repeating it.

Raw artifacts belong under ignored directories such as `target/`, `reports/`,
or `artifacts/`.

Run the strict docs gate before committing docs:

```bash
python scripts/check_docs_hygiene.py --fail-current-looking --fail-unregistered-current-looking --fail-large-retained --fail-missing-historical-banner --fail-missing-provenance-for-changed --top 12
```

Use `--warn-current-looking` only during exploratory cleanup.
