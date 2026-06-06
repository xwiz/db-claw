# Results Index

Only Start Here docs are live; other Markdown is retained one-run history.

## Start Here
| Doc | Role |
|---|---|
| [v02-current-status.md](v02-current-status.md) | decision card |
| [v02-evidence-ledger.md](v02-evidence-ledger.md) | regression anchors |
| [v02-quality-gate.md](v02-quality-gate.md) | release stop rules |
| [v02-semantic-atlas-completion-plan.md](v02-semantic-atlas-completion-plan.md) | active loop |

## Hygiene Contract
```bash
python scripts/check_docs_hygiene.py --fail-current-looking --fail-unregistered-current-looking --fail-large-retained --fail-missing-historical-banner --top 12
python scripts/check_git_artifacts.py --all
```

Run `python scripts/audit_v02_artifacts.py` before deleting under `target/v02`.
Living docs stay short; retained reports explain one run only. Promote durable
numbers to the ledger; register, rename, or banner anything that looks current.
