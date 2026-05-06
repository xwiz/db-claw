"""SemanticSQL rewriter — security-critical SQL validation + mandatory-filter injection.

Three modules, one purpose: ensure every query reaching the database is
SELECT-only and scoped to the calling tenant/owner/soft-delete predicate.

- :mod:`semsql_rewriter.sanitiser`: vocabulary input sanitisation (runs at
  extraction time so untrusted strings can never reach SQL).
- :mod:`semsql_rewriter.validator`: AST-level statement-type allowlist and
  schema cross-check via sqlglot.
- :mod:`semsql_rewriter.injector`: mandatory-filter injection across CTEs,
  subqueries, UNION branches, lateral joins, and recursive CTEs.

Reference: Apache Superset CVE-2025-48912 — the fix replaced an in-app
validator with sqlglot. We adopt sqlglot from day one and treat any output
the Rust second-pass parser disagrees with as a security failure.
"""

from .sanitiser import SanitiserError, sanitise_canonical_name, sanitise_label

__all__ = [
    "SanitiserError",
    "sanitise_canonical_name",
    "sanitise_label",
]

__version__ = "0.1.0.dev0"
