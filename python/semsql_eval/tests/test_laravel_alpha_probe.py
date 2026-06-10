from __future__ import annotations

from semsql_eval.laravel_alpha_probe import render_laravel_alpha_probe_markdown


def test_render_laravel_alpha_probe_markdown() -> None:
    report = {
        "status": "pass",
        "question": "show clients in segment enterprise",
        "approved_target": "packages.plan_level=enterprise",
        "checks": {
            "extract": True,
            "source_relationship_grounded": True,
            "first_query_asks_user": True,
            "approval_saved": True,
            "approved_rerun_executes": True,
        },
        "first_query": {
            "decision": "ask_user",
            "candidates": [
                "packages.plan_level=enterprise",
                "packages.service_level=enterprise",
            ],
        },
        "rerun": {
            "decision": "execute",
            "sql": 'SELECT "clients"."client_name" FROM "clients"',
        },
    }

    rendered = render_laravel_alpha_probe_markdown(report)

    assert "status: `PASS`" in rendered
    assert "`source_relationship_grounded` | `True`" in rendered
    assert "first decision: `ask_user`" in rendered
    assert "rerun decision: `execute`" in rendered
