"""Unit tests for FY27 budget workbook parsing (Demand Plan Comparison)."""
from __future__ import annotations

import pandas as pd

from data_sources.demand_plan_comparison import (
    budget_by_row_id_from_workbook,
    parse_fy27_budget_workbook,
)


def _workbook_frame() -> pd.DataFrame:
    """Minimal frame mirroring the Fabric export layout (screenshot)."""
    return pd.DataFrame([
        ["Millions of lbs.", "Budget"],
        ["Total B2C (WITHOUT Butter)", 1119.1],
        ["ESL", 308.1],
        ["Large Carton", 263.1],
        ["Branded", 162.2],
        ["Private", 100.9],
        ["Aerosol Can", 0.4],
        ["Cultured", 49.2],
        ["Large Tub", 32.5],
        ["Butter", 7.4],
    ])


def test_parse_fy27_budget_workbook_builds_label_paths():
    raw = _workbook_frame().to_csv(index=False).encode("utf-8")
    # parse expects xlsx bytes; test path logic via direct frame injection
    paths = {}
    # Inline the parser loop via exported helpers — use read_excel substitute:
    import io
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        _workbook_frame().to_excel(writer, index=False, header=False)
    paths = parse_fy27_budget_workbook(buf.getvalue())
    assert paths[("ESL", "Large Carton", "Branded")] == 162.2
    assert paths[("Cultured", "Large Tub")] == 32.5
    assert paths[("Butter",)] == 7.4


def test_budget_by_row_id_maps_template_leaves():
    paths = {
        ("ESL", "Large Carton", "Branded"): 162.2,
        ("ESL", "Large Carton", "Private"): 100.9,
        ("Butter",): 7.4,
    }
    by_id = budget_by_row_id_from_workbook(paths)
    assert by_id["esl_lc_branded"] == 162.2
    assert by_id["esl_lc_private"] == 100.9
    assert by_id["butter"] == 7.4
    assert "fm_tanker" not in by_id
