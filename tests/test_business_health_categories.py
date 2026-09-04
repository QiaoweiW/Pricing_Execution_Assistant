"""Unit tests for the Business Health per-category deep-dive.

Two lenses, both driven entirely by IBP **Order** lbs: **A** order YoY across
the trailing windows, and **Mix Shifts** — the top Customer×SKU movers computed
for EVERY trailing window (L12M / L6M / L3M).
"""

from datetime import date

import pandas as pd
import pytest

from data_sources.demand_plan_comparison import (
    BH_CATEGORY_ROWS,
    BH_CATEGORY_LABELS,
    BH_PERIOD_ORDER,
    BH_TAG_EXIT,
    BH_TAG_SOFTENING,
    BH_TAG_SUBSTITUTION,
    BH_TAG_GROWTH,
    build_business_health_categories,
)

PRIOR = date(2026, 6, 1)   # single-month fixtures land in every window ending here

_COLS = ["pmaj", "sfmt", "brand", "pminor", "customer_name", "item_desc",
         "month", "pounds"]


def _frame(rows: list[tuple]) -> pd.DataFrame:
    """Build an enriched-orders frame from ``(customer, sku, month, lbs)`` rows."""
    df = pd.DataFrame(
        [("Butter", "Sticks", "Branded", "Packaged Butter", c, s, m, p)
         for c, s, m, p in rows],
        columns=_COLS,
    )
    df["month"] = pd.to_datetime(df["month"]).dt.date
    df["item_key"] = df["item_desc"]
    return df


# ── Fixtures: a Butter deep-dive with clean, single-month numbers ────────────

def _orders() -> pd.DataFrame:
    """Butter orders — several Customer×SKU lines, incl. an order-only account
    (MT Food Bank) that has NO shipment.  Orders are the demand signal precisely
    so lines like that are not missed."""
    return _frame([
        # Growers.
        ("Costco", "KS Btr Qtr", "2026-06-01", 2_000_000),
        ("Costco", "KS Btr Qtr", "2025-06-01", 1_000_000),   # +1.0
        ("Kroger", "KRO Salted", "2026-06-01",   800_000),
        ("Kroger", "KRO Salted", "2025-06-01",   300_000),   # +0.5
        # Decliners.
        ("MT Food Bank", "DG Btr", "2025-06-01", 1_400_000),  # -1.4 (order-only)
        ("Baugh", "WhF Btr", "2026-06-01",   100_000),
        ("Baugh", "WhF Btr", "2025-06-01",   900_000),        # -0.8
        ("Golden State (McD)", "DG Btr", "2026-06-01", 200_000),
        ("Golden State (McD)", "DG Btr", "2025-06-01", 800_000),   # -0.6
    ])


def _butter(frame: pd.DataFrame | None = None, **kw):
    cats = {c.row_id: c for c in build_business_health_categories(
        _orders() if frame is None else frame, PRIOR, **kw)}
    return cats["butter"]


def _l3m(cat):
    """The L3M Mix Shifts block — every single-month fixture lands in it."""
    return cat.concentrations["L3M"]


# ── Shape ───────────────────────────────────────────────────────────────────

def test_categories_shape_and_order():
    cats = build_business_health_categories(_orders(), PRIOR)
    assert [c.row_id for c in cats] == list(BH_CATEGORY_ROWS)
    assert [c.label for c in cats] == [BH_CATEGORY_LABELS[r] for r in BH_CATEGORY_ROWS]


def test_lens_a_order_series_covers_every_window():
    """Lens A reads order pounds + YoY for each trailing window."""
    series = _butter().order_series
    assert set(series) >= set(BH_PERIOD_ORDER)
    # Butter L3M: 3.1M current (2.0 + 0.8 + 0.1 + 0.2) vs 4.4M year-ago.
    assert series["L3M"]["vol"] == pytest.approx(3.1, rel=1e-3)
    assert series["L3M"]["yoy"] == pytest.approx(3.1 / 4.4 - 1.0, rel=1e-3)


# ── Mix Shifts — computed per window ────────────────────────────────────────

def test_mix_shifts_computed_for_every_trailing_window():
    """Every category carries a Mix Shifts block for L12M, L6M AND L3M."""
    for cat in build_business_health_categories(_orders(), PRIOR):
        assert set(cat.concentrations) == set(BH_PERIOD_ORDER)


def test_mix_shifts_window_separates_structural_from_recent():
    """The whole point of running all three windows.

    A mover whose activity sits outside the recent windows shows up ONLY at
    L12M — so a planner can tell a structural shift from a recent one instead
    of reading a single L3M snapshot.
    """
    frame = _frame([
        # Recent mover: Jun 2026 vs Jun 2025 — inside L3M, L6M and L12M.
        ("Costco", "Recent SKU", "2026-06-01", 1_000_000),
        ("Costco", "Recent SKU", "2025-06-01",   200_000),   # +0.8
        # Structural-only mover: Oct 2025 vs Oct 2024.  Oct 2025 is inside the
        # L12M window (Jul 25 - Jun 26) but outside L6M (Jan - Jun 26) and L3M.
        ("Sysco", "Old SKU", "2025-10-01", 2_000_000),
        ("Sysco", "Old SKU", "2024-10-01",   500_000),       # +1.5, L12M only
    ])
    conc = _butter(frame).concentrations
    l12 = {s.label for s in conc["L12M"].all_movers}
    l6 = {s.label for s in conc["L6M"].all_movers}
    l3 = {s.label for s in conc["L3M"].all_movers}

    assert "Sysco × Old SKU" in l12          # structural — visible at the year
    assert "Sysco × Old SKU" not in l6       # …and only there
    assert "Sysco × Old SKU" not in l3
    assert "Costco × Recent SKU" in l3 & l6 & l12   # recent — visible everywhere


def test_mix_shifts_windows_are_independently_scaled():
    """Each window's ``share`` is a % of THAT window's gross move, not L3M's."""
    frame = _frame([
        ("Costco", "Recent SKU", "2026-06-01", 1_000_000),
        ("Costco", "Recent SKU", "2025-06-01",   200_000),   # +0.8 in every window
        ("Sysco", "Old SKU", "2025-10-01", 2_000_000),
        ("Sysco", "Old SKU", "2024-10-01",   500_000),       # +1.5, L12M only
    ])
    conc = _butter(frame).concentrations
    assert conc["L3M"].total_abs_m == pytest.approx(0.8, rel=1e-3)
    assert conc["L12M"].total_abs_m == pytest.approx(2.3, rel=1e-3)   # 0.8 + 1.5
    # Costco is the whole L3M move but only a third of the L12M move.
    l3_costco = next(s for s in conc["L3M"].all_movers if s.customer == "Costco")
    l12_costco = next(s for s in conc["L12M"].all_movers if s.customer == "Costco")
    assert l3_costco.share == pytest.approx(1.0, rel=1e-3)
    assert l12_costco.share == pytest.approx(0.8 / 2.3, rel=1e-3)


def test_mix_shifts_order_based_captures_mt_food_bank():
    """Ranked by ORDER-lbs Δ, so MT Food Bank (order-only, no shipment) is
    captured as the #1 decliner."""
    conc = _l3m(_butter())
    # Order deltas: Costco +1.0, Kroger +0.5, MT Food Bank −1.4, Baugh −0.8,
    # Golden State −0.6 → Σ|Δ| = 4.3M.
    assert conc.total_abs_m == pytest.approx(4.3, rel=1e-3)
    assert [g.label for g in conc.growers] == ["Costco × KS Btr Qtr", "Kroger × KRO Salted"]
    assert conc.growers[0].delta_m == pytest.approx(1.0, rel=1e-3)
    assert conc.growers[0].share == pytest.approx(1.0 / 4.3, rel=1e-3)
    top_decliner = conc.decliners[0]
    assert top_decliner.label == "MT Food Bank × DG Btr"      # captured, ranked #1
    assert top_decliner.kind == "decliner"
    assert top_decliner.delta_m == pytest.approx(-1.4, rel=1e-3)
    assert top_decliner.yoy_pct == pytest.approx(-1.0, rel=1e-3)   # 0 vs 1.4M → −100%
    assert [d.label for d in conc.decliners] == [
        "MT Food Bank × DG Btr", "Baugh × WhF Btr", "Golden State (McD) × DG Btr"]
    # Ranking is by absolute pounds — never YoY% (MT Food Bank's −100% doesn't
    # jump it above a bigger-pound line; it wins here on 1.4M lbs, not the %).
    assert all(g.kind == "grower" for g in conc.growers)


def test_mix_shifts_tags_and_decline_concentration():
    """Each mover is tagged exit / softening / substitution / growth / new, and
    the decline concentration answers 'few accounts or broad?'."""
    conc = _l3m(_butter())
    tags = {s.label: s.tag for s in (*conc.decliners, *conc.growers)}
    assert tags["MT Food Bank × DG Btr"] == BH_TAG_EXIT             # 1.4M → 0 (walked away)
    assert tags["Golden State (McD) × DG Btr"] == BH_TAG_SOFTENING  # 0.8M → 0.2M partial
    assert tags["Costco × KS Btr Qtr"] == BH_TAG_GROWTH             # 1.0M → 2.0M
    # Net decline: MT 1.4 + Baugh 0.8 + Golden 0.6 = 2.8M across 3 accounts.
    assert conc.decline_total_m == pytest.approx(2.8, rel=1e-3)
    assert conc.decline_accounts == 3
    assert conc.decline_k == 3
    # all_movers = every significant mover, ranked by |Δ| desc, each tagged.
    am = conc.all_movers
    assert len(am) == 5                                        # 2 growers + 3 decliners
    assert am[0].label == "MT Food Bank × DG Btr"              # biggest |Δ| (1.4M)
    assert abs(am[0].delta_m) >= abs(am[-1].delta_m)           # sorted by |Δ|
    assert all(s.tag and s.customer and s.sku for s in am)     # tagged + customer/sku set
    # by_customer: one block per customer, ordered by |net|, biggest on top.
    bc = conc.by_customer
    assert {g.customer for g in bc} == {
        "Costco", "Kroger", "MT Food Bank", "Baugh", "Golden State (McD)"}
    assert bc[0].customer == "MT Food Bank"                    # |net 1.4| is largest
    assert bc[0].net_delta_m == pytest.approx(-1.4, rel=1e-3)
    assert bc[0].movers[0].sku == "DG Btr"                     # SKU ins/outs within
    assert all(abs(g.net_delta_m) >= abs(nxt.net_delta_m)      # net-descending order
               for g, nxt in zip(bc, bc[1:]))


def test_mix_shifts_substitution_tag():
    """A customer that drops one SKU and picks up another (net ~flat) is tagged
    SUBSTITUTION on both lines — the lbs moved, not lost/gained."""
    frame = _frame([   # Costco: SKU A 1.0M → 0 ; SKU B 0 → 1.0M (net flat)
        ("Costco", "SKU A", "2025-06-01", 1_000_000),
        ("Costco", "SKU B", "2026-06-01", 1_000_000),
    ])
    conc = _l3m(_butter(frame))
    tags = {s.label: s.tag for s in (*conc.decliners, *conc.growers)}
    assert tags["Costco × SKU A"] == BH_TAG_SUBSTITUTION   # dropped but reappears
    assert tags["Costco × SKU B"] == BH_TAG_SUBSTITUTION   # as SKU B, same customer
    # Grouped by customer: Costco is one block, net ~0 (substitution), two SKUs.
    assert len(conc.by_customer) == 1
    g = conc.by_customer[0]
    assert g.customer == "Costco" and g.net_delta_m == pytest.approx(0.0, abs=1e-6)
    assert g.gross_m == pytest.approx(2.0, rel=1e-3) and len(g.movers) == 2


def test_mix_shifts_excludes_near_zero_movers():
    """Lines below ~0.5% of the gross move are dropped from the mover table."""
    frame = _frame([   # Big mover 1.0M + a trivial 1k-lb line (~0.1%) → dropped.
        ("Costco", "Big", "2026-06-01", 1_000_000),
        ("Tiny Co", "Small", "2026-06-01", 1_000),
    ])
    conc = _l3m(_butter(frame))
    labels = {s.label for s in conc.all_movers}
    assert "Costco × Big" in labels
    assert not any("Tiny Co" in lbl for lbl in labels)          # near-0% excluded
    assert {g.customer for g in conc.by_customer} == {"Costco"}  # Tiny Co dropped


# ── Degradation ─────────────────────────────────────────────────────────────

def test_empty_inputs_degrade():
    cats = build_business_health_categories(None, PRIOR)
    assert [c.row_id for c in cats] == list(BH_CATEGORY_ROWS)
    for c in cats:
        assert set(c.concentrations) == set(BH_PERIOD_ORDER)
        for conc in c.concentrations.values():
            assert conc.growers == () and conc.decliners == ()


def test_packaged_butter_label_is_consistent():
    assert BH_CATEGORY_LABELS["butter"] == "Packaged Butter"
