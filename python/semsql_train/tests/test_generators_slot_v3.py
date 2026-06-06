from __future__ import annotations

import json
from pathlib import Path

from semsql_train.generators_slot_v3 import DeriveSlotConfig, derive_slot_pairs


def test_derive_slot_pairs_canonicalizes_display_field_targets(tmp_path: Path) -> None:
    src = tmp_path / "teacher.jsonl"
    src.write_text(
        json.dumps(
            {
                "stage": 2,
                "nl": "charter schools in Alameda",
                "db_id": "demo",
                "natsql_skeleton": "SELECT @field1 FROM @entity1 WHERE @field2 = @val1",
                "ranked_schema": [
                    {"kind": "entity", "target": "FRPM", "score": 1.0},
                    {"kind": "field", "target": "frpm.Charter School (Y/N)", "score": 1.0},
                    {"kind": "field", "target": "frpm.County Name", "score": 1.0},
                ],
                "slot_map": {
                    "@entity1": "FRPM",
                    "@field1": "frpm.Charter School (Y/N)",
                    "@field2": "frpm.County Name",
                    "@val1": "'Alameda'",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    rows = list(derive_slot_pairs(src, DeriveSlotConfig(candidates_per_slot=4, seed=1)))
    field_rows = {row["slot_name"]: row for row in rows if row["slot_name"].startswith("@field")}

    assert field_rows["@field1"]["candidates"][
        field_rows["@field1"]["correct_index"]
    ] == "frpm.charter_school_y_n"
    assert field_rows["@field2"]["candidates"][
        field_rows["@field2"]["correct_index"]
    ] == "frpm.county_name"
    assert all(
        "Charter School" not in candidate
        for row in field_rows.values()
        for candidate in row["candidates"]
    )
