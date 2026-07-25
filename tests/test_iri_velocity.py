"""Unit tests for IRI consumer sell-through velocity + index (pure builder)."""
import pandas as pd
import pytest

import data_sources.iri_velocity as iri


def _raw() -> pd.DataFrame:
    # velocity = Σ units ÷ Σ (units ÷ units-per-store).  One row → velocity == upss.
    cols = [iri.COL_GEOGRAPHY, iri.COL_BRAND, iri.COL_SUBTYPE, iri.COL_PROCESS,
            iri.COL_SIZE, iri.COL_WEEK, iri.COL_U_SALES, iri.COL_UNITS_PER_STORE]
    rows = [
        # W1: GeoA 100u/10ps → 10 stores; GeoB 200u/40ps → 5 stores → 300u/15st = 20
        ["GeoA", "DARIGOLD", "REGULAR", "ESL", "20.1-47.9 OZ", "1/5/2025", 100, 10],
        ["GeoB", "DARIGOLD", "REGULAR", "ESL", "20.1-47.9 OZ", "1/5/2025", 200, 40],
        # W2: single row → velocity == upss = 30
        ["GeoA", "DARIGOLD", "REGULAR", "ESL", "20.1-47.9 OZ", "1/12/2025", 100, 30],
        # W3: velocity == 10
        ["GeoA", "DARIGOLD", "REGULAR", "ESL", "20.1-47.9 OZ", "1/19/2025", 100, 10],
        # A competitor row that must be filtered out when brand=DARIGOLD.
        ["GeoA", "FAIRLIFE", "REGULAR", "ESL", "20.1-47.9 OZ", "1/5/2025", 999, 999],
    ]
    return pd.DataFrame(rows, columns=cols)


def test_build_iri_weekly_aggregates_and_indexes():
    res = iri.build_iri_weekly(_raw(), brands=["DARIGOLD"])
    w = res.weekly.sort_values(iri.WEEK_START).reset_index(drop=True)
    assert list(w[iri.SELL_THROUGH_VELOCITY].round(4)) == [20.0, 30.0, 10.0]
    assert res.baseline == pytest.approx(20.0)                 # median of [20,30,10]
    assert list(w[iri.SELL_THROUGH_INDEX].round(1)) == [100.0, 150.0, 50.0]


def test_week_start_is_monday_of_ending_sunday():
    res = iri.build_iri_weekly(_raw(), brands=["DARIGOLD"])
    # Circana week ending Sun 1/5/2025 → Monday 2024-12-30.
    first = res.weekly.sort_values(iri.WEEK_START)[iri.WEEK_START].iloc[0]
    assert pd.Timestamp(first).weekday() == 0                  # Monday
    assert pd.Timestamp(first).date().isoformat() == "2024-12-30"


def test_empty_and_no_match():
    assert iri.build_iri_weekly(None).weekly.empty
    assert iri.build_iri_weekly(_raw(), brands=["NOPE"]).weekly.empty
