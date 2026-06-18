"""
OneLake-backed store helpers for Bid Assistant RFP P&L Analysis.

Responsibilities
----------------
* Read and cache the three source CSVs used by the RFP P&L model:
  - Files/BOM/BOM_History_Tracker_tagged.csv
  - Files/BOM/Budget/Budget_Update.csv
  - Files/RO Tracking/Demand Plan/qry_pdh.csv
* Compute default cost outputs for each Target SKU item.
* List/read/write scenario CSV files under Files/Program_Bid_Management/New_Bids.

This module is intentionally UI-agnostic. Streamlit widgets live in the page
layer; this store provides deterministic data + calculation helpers only.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Iterable, Optional

import pandas as pd
import streamlit as st

from data_sources import fabric_lakehouse_io as _io


class RfpPnlStoreError(RuntimeError):
    """Raised on configuration/auth/I-O failures for RFP P&L storage."""


_SECRETS_SECTION = "fabric_htst"

# Source files
_BOM_PATH = "BOM/BOM_History_Tracker_tagged.csv"
_BUDGET_PATH = "BOM/Budget/Budget_Update.csv"
_PDH_PATH = "RO Tracking/Demand Plan/qry_pdh.csv"

# Scenario persistence location
SCENARIO_FOLDER = "Program_Bid_Management/New_Bids"

# Canonical item table schema (one row per target SKU item).
ITEM_COL = "Item"
METRIC_COLS: tuple[str, ...] = (
    "Month",
    "Plant",
    "Target SKU Name",
    "Target SKU lbs per Each",
    "Target SKU Volume (units)",
    "Target SKU Volume (pounds)",
    "Milk Reference SKU",
    "Ingredient Reference SKU",
    "Packaging Reference SKU",
    "Conversion Reference SKU",
    "Milk Reference SKU lbs per Each",
    "Ingredient Reference SKU lbs per Each",
    "Packaging Reference SKU lbs per Each",
    "Conversion Reference SKU lbs per Each",
    "Category",
    "PCM $/lbs",
    "FOB Price",
    # Cost rows: each component has an ``<Component> Override`` companion
    # column for the analyst's manual override (blank = use BOM/Budget
    # default; type a number to override). The displayed component cell
    # is *strictly* recomputed on every Refresh as
    # ``override if non-blank else BOM/Budget default``, so saved values
    # can never silently mask the calculated result. Override columns are
    # persisted in the scenario CSV (so re-opening a saved scenario
    # rehydrates them) but hidden from the on-screen scenario table to
    # keep it readable.
    "Milk Override",
    "Milk",
    "Ingredient Override",
    "Ingredient",
    "Packaging Override",
    "Packaging",
    "Conversion Cost Override",
    "Conversion Cost",
    "Cost of Quality Override",
    "Cost of Quality",
    "Internal Logistics (Shuttling & WHSE) Override",
    "Internal Logistics (Shuttling & WHSE)",
    "Other Cost",
    "Total Costs",
    "PCM",
    "PCM%",
    "GP",
    "GP $/lbs",
    "GP%",
    # Retail-side rows: Retail Price and Freight Cost are user inputs;
    # Delivered Price and Retailer's Margin% are strictly recomputed
    # (override-style overrides not supported here — no calculated
    # default exists for either input). Freight is allowed to be blank
    # (treated as $0/EA).
    "Retail Price",
    "Freight Cost",
    "Delivered Price",
    "Retailer's Margin%",
)

# Maps each user-overridable cost component (display column) to the
# scenario column that holds the analyst's manual override. Used by
# ``_calc_for_item`` to apply override precedence and by the page's
# input panel to render one widget per override.
COST_OVERRIDE_FOR: dict[str, str] = {
    "Milk": "Milk Override",
    "Ingredient": "Ingredient Override",
    "Packaging": "Packaging Override",
    "Conversion Cost": "Conversion Cost Override",
    "Cost of Quality": "Cost of Quality Override",
    "Internal Logistics (Shuttling & WHSE)":
        "Internal Logistics (Shuttling & WHSE) Override",
}

# Always recomputed on Refresh; users do not override these directly.
# All cost components are strict because they are pure functions of
# (BOM/Budget defaults, override columns, Reference SKU lbs/Each).
# Treating any of them as defaultable previously caused stale values
# from a saved scenario CSV to survive every Refresh, masking input
# changes from the analyst. To override a cost, populate its dedicated
# ``<Component> Override`` column instead.
STRICT_CALC_METRICS = {
    "Target SKU Volume (pounds)",
    "Milk",
    "Ingredient",
    "Packaging",
    "Conversion Cost",
    "Cost of Quality",
    "Internal Logistics (Shuttling & WHSE)",
    "FOB Price",
    "Total Costs",
    "PCM",
    "PCM%",
    "GP",
    "GP $/lbs",
    "GP%",
    "Delivered Price",
    "Retailer's Margin%",
}

# Auto-derived defaults that remain editable: value is filled only when blank.
# Reference SKU lbs per Each are intentionally NOT auto-defaulted from PDH —
# the user enters those by hand because the PDH net-weight column is too
# inconsistent to be a reliable pricing driver.
DEFAULTABLE_METRICS = {
    "Ingredient Reference SKU",
    "Packaging Reference SKU",
    "Conversion Reference SKU",
    "Category",
}

# Conversion cost intentionally drops cost categories that are accounted
# for elsewhere on the P&L (Milk Component / Ingredient / Milk /
# Packaging) and Depreciation, which is treated as a non-conversion
# overhead by the bid model. Comparisons are case-insensitive (values
# are normalized via ``_norm`` before the membership test).
_CONVERSION_TAG_EXCLUDE = {
    "milk component",
    "ingredient",
    "milk",
    "packaging",
    "depreciation",
}

# BOM cost aggregation uses ``Ext Cost.1`` (the per-resource line cost)
# exclusively. Two columns starting with "Ext Cost" exist in the tagged
# BOM extract:
#
# * ``Ext Cost.1`` — the line-level resource cost (one number per
#   ingredient / labour / utility row). This is what every analyst spot-
#   checks against.
# * ``Ext Cost`` — the rolled-up Output-Item cost. On real ingredient
#   rows it duplicates the recipe's total cost; on "Upper Level Costs"
#   placeholder rows it carries the **entire rolled-up recipe** value
#   (ingredient + packaging + conversion + everything).
#
# Including ``Ext Cost`` either double-counts (Packaging) or pulls in
# unrelated cost categories (Conversion). We therefore sum only
# ``Ext Cost.1``. ``Ext Cost`` is used as a column-level fallback only
# when ``Ext Cost.1`` is entirely absent from the file (legacy snapshot).
_BOM_RESOURCE_COST_COL = "Ext Cost.1"
_BOM_FALLBACK_COST_COL = "Ext Cost"


@dataclass(frozen=True)
class ScenarioFile:
    """Minimal metadata for a saved RFP P&L scenario CSV in OneLake."""

    name: str
    full_path: str
    etag: Optional[str]
    last_modified: Optional[str]
    size: int


@dataclass(frozen=True)
class RfpPnlSources:
    """Normalized, calculation-ready source datasets and lookup maps.

    ``cost_col`` is the BOM column used for cost aggregation
    (``Ext Cost.1`` in current files; falls back to ``Ext Cost`` only if
    the resource-level column is missing entirely).
    """

    bom_df: pd.DataFrame
    budget_df: pd.DataFrame
    pdh_df: pd.DataFrame
    cost_col: str
    month_options: tuple[str, ...]
    plant_options: tuple[str, ...]
    # Normalized set of every PDH ``Item Description``. The Reference SKU
    # dropdowns are sourced from the BOM (Level-1 ``Rule Item Desc``) but
    # restricted to values that also appear here, since each Reference SKU
    # must resolve to a PDH Category. See :func:`reference_sku_options`.
    pdh_item_desc_set: frozenset[str]
    category_by_desc: dict[str, str]
    budget_sum_by_cat_tag: dict[tuple[str, str], float]


def _norm(value: object) -> str:
    """Case-insensitive, whitespace-trimmed string key with NaN safety.

    Returning ``""`` for NaN / None is critical: every downstream filter
    (Tag exclusion, Plant / Month / Rule Item Desc match, the empty-Tag
    rollup guard) compares against normalized strings, and ``str(nan)``
    silently produces ``"nan"`` which would slip past every test.
    """
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    return str(value).strip().casefold()


def _is_blank(value: object) -> bool:
    if value is None:
        return True
    if pd.isna(value):
        return True
    return str(value).strip() == ""


def _apply_override(default: Optional[float], override_raw: object) -> Optional[float]:
    """Override-precedence rule for cost components.

    Returns the analyst's override when it parses as a number; otherwise
    returns the BOM/Budget default. ``None`` propagates so empty defaults
    (e.g. missing Reference SKU lbs/Each) still render as a blank cell
    in the scenario table when no override is supplied.
    """
    override = _to_float(override_raw)
    if override is not None:
        return override
    return default


def _to_float(value: object) -> Optional[float]:
    """Best-effort numeric coercion.

    Returns ``None`` for blanks (``None``, empty string, NaN) and for
    strings that cannot be parsed as numbers. The NaN guard is critical:
    pandas' CSV reader represents blank cells as ``float('nan')`` and
    those values flow into override columns when a saved scenario is
    reloaded. Without this guard, ``_apply_override`` would treat NaN as
    a "valid" override (since ``NaN is not None``), the NaN propagates
    through every cost sum, and the UI renders it as ``$nan`` / ``nan%``.
    """
    if value is None:
        return None
    if isinstance(value, str) and value.strip() == "":
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    try:
        result = float(value)
    except (TypeError, ValueError):
        try:
            s = (
                str(value)
                .strip()
                .replace("$", "")
                .replace(",", "")
                .replace("%", "")
                .replace("(", "-")
                .replace(")", "")
            )
            result = float(s)
        except (TypeError, ValueError):
            return None
    return None if pd.isna(result) else result


def _safe_div(num: Optional[float], den: Optional[float]) -> Optional[float]:
    if num is None or den is None or den == 0:
        return None
    return num / den


def _fmt_level(level: object) -> str:
    return str(level).strip()


def _level_equals(level: object, target: int) -> bool:
    s = _fmt_level(level)
    if not s:
        return False
    try:
        return float(s) == float(target)
    except ValueError:
        return s == str(target)


def _level_contains(level: object, needle: str) -> bool:
    return needle in _fmt_level(level)


def _sum_resource_cost(df: pd.DataFrame, cost_col: str) -> float:
    """Sum ``cost_col`` (Ext Cost.1) across the matched rows.

    "Upper Level Costs" placeholder rows have ``Ext Cost.1 == 0`` and
    naturally contribute zero — that is the desired behaviour because
    their ``Ext Cost`` value represents the full rolled-up recipe and
    would otherwise pollute Conversion / Packaging totals.
    """
    if df.empty or cost_col not in df.columns:
        return 0.0
    return float(pd.to_numeric(df[cost_col], errors="coerce").fillna(0.0).sum())


def _read_csv_or_raise(blob_path: str) -> pd.DataFrame:
    try:
        df, _etag = _io.read_csv(_SECRETS_SECTION, blob_path)
    except _io.LakehouseIOError as exc:
        raise RfpPnlStoreError(str(exc)) from exc
    if df is None:
        raise RfpPnlStoreError(f"File not found in OneLake: Files/{blob_path}")
    out = df.copy()
    out.columns = [str(c).strip() for c in out.columns]
    return out


def _month_sort_key(label: str) -> tuple[int, datetime]:
    # Stable sorting for mixed date formats; unparseable values sink to end.
    dt = pd.to_datetime(str(label).strip(), errors="coerce")
    if pd.isna(dt):
        return (1, datetime.max)
    return (0, dt.to_pydatetime())


@st.cache_data(ttl=300, show_spinner=False)
def load_sources() -> RfpPnlSources:
    """Load/normalize BOM + Budget + PDH source files with cache."""
    bom = _read_csv_or_raise(_BOM_PATH)
    budget = _read_csv_or_raise(_BUDGET_PATH)
    pdh = _read_csv_or_raise(_PDH_PATH)

    for col in ("Per Beg", "Plant", "Rule Item Desc", "Tag", "Level",
                "Ing-Rsrc Desc", "Qty.1", "Top Recipe"):
        if col not in bom.columns:
            bom[col] = ""
    bom["_norm_month"] = bom["Per Beg"].map(_norm)
    bom["_norm_plant"] = bom["Plant"].map(_norm)
    bom["_norm_rule_item_desc"] = bom["Rule Item Desc"].map(_norm)
    bom["_norm_tag"] = bom["Tag"].map(_norm)
    bom["_norm_top_recipe"] = bom["Top Recipe"].map(_norm)
    bom["_level_text"] = bom["Level"].map(_fmt_level)
    # Use the resource-level Ext Cost.1 column for all cost aggregation.
    # Fall back to "Ext Cost" only when the file is missing the .1 variant
    # entirely (legacy snapshot before the tagged-BOM workflow added it).
    if _BOM_RESOURCE_COST_COL in bom.columns:
        cost_col = _BOM_RESOURCE_COST_COL
    elif _BOM_FALLBACK_COST_COL in bom.columns:
        cost_col = _BOM_FALLBACK_COST_COL
    else:
        cost_col = _BOM_RESOURCE_COST_COL
        bom[cost_col] = 0.0

    month_values = sorted(
        [str(v).strip() for v in bom["Per Beg"].dropna().astype(str).tolist() if str(v).strip()],
        key=_month_sort_key,
    )
    month_options = tuple(dict.fromkeys(month_values))

    plant_values = sorted(
        [str(v).strip() for v in bom["Plant"].dropna().astype(str).tolist() if str(v).strip()],
        key=lambda x: x.casefold(),
    )
    plant_options = tuple(dict.fromkeys(plant_values))

    for col in ("Item Description", "Portfolio Major"):
        if col not in pdh.columns:
            pdh[col] = ""
    pdh["_norm_item_desc"] = pdh["Item Description"].map(_norm)
    pdh["_category"] = pdh["Portfolio Major"].fillna("").astype(str).str.strip()

    # Reference-SKU weight (lbs/Each) is intentionally NOT loaded from PDH —
    # users supply this manually because the PDH `Item Net Weight Lbs`
    # column is too inconsistent to be a trustworthy pricing input.
    category_by_desc: dict[str, str] = {}
    for norm_desc, grp in pdh.groupby("_norm_item_desc", dropna=False):
        if not norm_desc:
            continue
        cats = grp["_category"][grp["_category"].astype(str).str.strip() != ""]
        if not cats.empty:
            category_by_desc[norm_desc] = str(cats.iloc[0]).strip()

    # Reference SKU dropdowns now derive from the BOM (Level-1 per plant +
    # month) rather than PDH; we only need PDH as a membership filter, so a
    # normalized set is sufficient. See :func:`reference_sku_options`.
    pdh_item_desc_set = frozenset(
        norm
        for norm in (_norm(v) for v in pdh["Item Description"].dropna().astype(str).tolist())
        if norm
    )

    for col in ("Category", "Tag", "Budget Value"):
        if col not in budget.columns:
            budget[col] = ""
    budget["_norm_category"] = budget["Category"].map(_norm)
    budget["_norm_tag"] = budget["Tag"].map(_norm)
    budget["_budget_value"] = pd.to_numeric(budget["Budget Value"], errors="coerce").fillna(0.0)
    budget_sum_by_cat_tag: dict[tuple[str, str], float] = (
        budget.groupby(["_norm_category", "_norm_tag"], dropna=False)["_budget_value"].sum().to_dict()
    )

    return RfpPnlSources(
        bom_df=bom,
        budget_df=budget,
        pdh_df=pdh,
        cost_col=cost_col,
        month_options=month_options,
        plant_options=plant_options,
        pdh_item_desc_set=pdh_item_desc_set,
        category_by_desc=category_by_desc,
        budget_sum_by_cat_tag=budget_sum_by_cat_tag,
    )


@st.cache_data(ttl=120, show_spinner=False)
def list_scenarios() -> list[ScenarioFile]:
    """Return scenario CSV files in Files/Program_Bid_Management/New_Bids."""
    try:
        files = _io.list_files(_SECRETS_SECTION, SCENARIO_FOLDER, suffix=".csv")
    except _io.LakehouseIOError as exc:
        raise RfpPnlStoreError(str(exc)) from exc
    out = [
        ScenarioFile(
            name=f.name,
            full_path=f.full_path,
            etag=f.etag,
            last_modified=f.last_modified,
            size=f.size,
        )
        for f in files
    ]
    return sorted(out, key=lambda f: f.last_modified or "", reverse=True)


def _scenario_path_from_name(scenario_name: str) -> str:
    name = str(scenario_name).strip().replace("\\", "_").replace("/", "_")
    if not name:
        raise RfpPnlStoreError("Scenario name cannot be empty.")
    if not name.lower().endswith(".csv"):
        name = f"{name}.csv"
    return f"{SCENARIO_FOLDER}/{name}"


def scenario_path_from_name(scenario_name: str) -> str:
    """Public wrapper for UI-level scenario name normalization."""
    return _scenario_path_from_name(scenario_name)


def build_empty_scenario(*, item_count: int = 1) -> pd.DataFrame:
    """Return a canonical empty scenario table with N item rows."""
    count = max(1, int(item_count))
    rows = []
    for i in range(count):
        row = {ITEM_COL: f"Item {i + 1}"}
        for metric in METRIC_COLS:
            row[metric] = ""
        row["Other Cost"] = 0.0
        rows.append(row)
    return pd.DataFrame(rows, columns=(ITEM_COL, *METRIC_COLS))


def _coerce_scenario_df(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out.columns = [str(c).strip() for c in out.columns]
    if ITEM_COL not in out.columns:
        out.insert(0, ITEM_COL, [f"Item {i + 1}" for i in range(len(out))])
    for col in METRIC_COLS:
        if col not in out.columns:
            out[col] = ""
    out = out[[ITEM_COL, *METRIC_COLS]].copy()
    out[ITEM_COL] = out[ITEM_COL].fillna("").astype(str).str.strip()
    for idx in out.index:
        if not out.at[idx, ITEM_COL]:
            out.at[idx, ITEM_COL] = f"Item {idx + 1}"
    return out.reset_index(drop=True)


def read_scenario(file_path: str) -> tuple[pd.DataFrame, Optional[str]]:
    """Read one saved scenario CSV and coerce it to canonical item schema."""
    try:
        df, etag = _io.read_csv(_SECRETS_SECTION, file_path)
    except _io.LakehouseIOError as exc:
        raise RfpPnlStoreError(str(exc)) from exc
    if df is None:
        raise RfpPnlStoreError(f"Scenario file not found: Files/{file_path}")
    return _coerce_scenario_df(df), etag


def save_scenario(
    scenario_name: str,
    items_df: pd.DataFrame,
    *,
    etag: Optional[str] = None,
) -> tuple[str, str]:
    """Write the scenario CSV; returns ``(file_path, new_etag)``."""
    file_path = _scenario_path_from_name(scenario_name)
    payload = _coerce_scenario_df(items_df)
    try:
        new_etag = _io.write_csv(_SECRETS_SECTION, file_path, payload, etag=etag)
    except _io.LakehouseIOError as exc:
        raise RfpPnlStoreError(str(exc)) from exc
    list_scenarios.clear()
    return file_path, new_etag


def scenario_exists(file_path: str) -> bool:
    """Return True when a scenario file already exists in New_Bids."""
    return any(s.full_path == file_path for s in list_scenarios())


def _lookup_category(sources: RfpPnlSources, item_desc: object) -> str:
    norm_desc = _norm(item_desc)
    if not norm_desc:
        return ""
    return sources.category_by_desc.get(norm_desc, "")


def _budget_value(sources: RfpPnlSources, category: object, tag: str) -> float:
    return float(sources.budget_sum_by_cat_tag.get((_norm(category), _norm(tag)), 0.0))


def _two_step_cost(
    sources: RfpPnlSources,
    *,
    month: object,
    plant: object,
    step1_rule_item_desc: object,
    step2_tag: str,
    ref_lbs_each: Optional[float],
    target_lbs_each: Optional[float],
) -> Optional[float]:
    """2-step BOM cost lookup used by the Milk and Ingredient rows.

    Step 1: rows with ``Rule Item Desc`` equal to the supplied Reference SKU,
            ``Level == 1``, ``Tag == "Milk Component"``. We capture the
            chain descriptor (``Ing-Rsrc Desc``), ``Qty.1`` and ``Top
            Recipe`` for each.
    Step 2: rows with ``Rule Item Desc`` equal to the step-1 chain descriptor,
            ``Level`` containing ``"2"``, ``Tag`` equal to the supplied
            ``step2_tag`` (``"Milk"`` or ``"Ingredient"``), and
            ``Top Recipe`` matching the step-1 anchor (so we don't pull in
            another finished good that shares the same sub-recipe).

    Per-anchor cost = Σ Ext Cost.1 × Qty.1 ÷ Reference SKU lbs/Each
                      × Target SKU lbs/Each.

    Dimensional intuition:

        Σ Ext Cost.1   = $ / lb of sub-recipe (e.g. bulk cream)
        Qty.1          = lb of sub-recipe / EA of Reference SKU
        ──────────────────────────────────────────────────────────
        product        = $ / EA of Reference SKU
        ÷ ref lbs/EA   = $ / lb of Reference SKU
        × target lbs/EA = $ / EA of Target SKU
    """
    if target_lbs_each is None or ref_lbs_each is None or ref_lbs_each == 0:
        return None

    bom = sources.bom_df
    m_norm = _norm(month)
    p_norm = _norm(plant)
    step1_rule = _norm(step1_rule_item_desc)
    if not (m_norm and p_norm and step1_rule):
        return None

    step1 = bom[
        (bom["_norm_month"] == m_norm)
        & (bom["_norm_plant"] == p_norm)
        & (bom["_norm_rule_item_desc"] == step1_rule)
        & (bom["_norm_tag"] == "milk component")
        & (bom["Level"].map(lambda v: _level_equals(v, 1)))
    ]
    if step1.empty:
        return None

    total = 0.0
    matched = False
    for _, row in step1.iterrows():
        qty_1 = _to_float(row.get("Qty.1"))
        chain_desc = _norm(row.get("Ing-Rsrc Desc"))
        anchor_top_recipe = _norm(row.get("Top Recipe"))
        if qty_1 is None or qty_1 == 0 or not chain_desc:
            continue

        # Scope step 2 to the same ``Top Recipe`` as the step-1 anchor.
        # The chain key (sub-recipe Rule Item Desc) is shared by every
        # parent recipe that consumes that sub-recipe, so without this
        # scope we would also pull in step-2 rows belonging to
        # *other* finished goods that happen to share a sub-recipe
        # (e.g. ``DG Hvy Whip Hg UP`` and ``DG Cl 40pc Whip Hg UP Disp
        # Box`` both consume ``Crm Whp Bulk 40.5pc Cln UP``), which
        # would silently double-count Milk and Ingredient costs.
        step2 = bom[
            (bom["_norm_month"] == m_norm)
            & (bom["_norm_plant"] == p_norm)
            & (bom["_norm_rule_item_desc"] == chain_desc)
            & (bom["_norm_tag"] == _norm(step2_tag))
            & (bom["Level"].map(lambda v: _level_contains(v, "2")))
        ]
        if anchor_top_recipe and "_norm_top_recipe" in bom.columns:
            step2 = step2[step2["_norm_top_recipe"] == anchor_top_recipe]
        ext_sum = _sum_resource_cost(step2, sources.cost_col)
        total += ext_sum * qty_1 * target_lbs_each / ref_lbs_each
        matched = True

    return total if matched else None


_LEVEL_EQUALS_1: Callable[[object], bool] = lambda v: _level_equals(v, 1)
_LEVEL_CONTAINS_2: Callable[[object], bool] = lambda v: _level_contains(v, "2")


def _one_step_cost(
    sources: RfpPnlSources,
    *,
    month: object,
    plant: object,
    rule_item_desc: object,
    level_match: Callable[[object], bool] = _LEVEL_EQUALS_1,
    tag_include: Optional[set[str]] = None,
    tag_exclude: Optional[set[str]] = None,
    require_non_empty_tag: bool = False,
    ref_lbs_each: Optional[float],
    target_lbs_each: Optional[float],
) -> Optional[float]:
    """1-step BOM cost lookup used by the Packaging and Conversion rows.

    Filter rows to ``Per Beg``=month, ``Plant``=plant, ``Rule Item Desc`` =
    Reference SKU, the supplied ``level_match`` predicate, plus the optional
    tag include / exclude rules. ``require_non_empty_tag`` additionally
    discards placeholder/rollup rows (e.g. ``Ing-Rsrc Desc = "Upper Level
    Costs"``) that carry an empty ``Tag``.
    Result = Σ Ext Cost.1 × Target SKU lbs/Each ÷ Reference SKU lbs/Each.
    """
    if target_lbs_each is None or ref_lbs_each is None or ref_lbs_each == 0:
        return None

    m_norm = _norm(month)
    p_norm = _norm(plant)
    rule_norm = _norm(rule_item_desc)
    if not (m_norm and p_norm and rule_norm):
        return None

    bom = sources.bom_df
    base = bom[
        (bom["_norm_month"] == m_norm)
        & (bom["_norm_plant"] == p_norm)
        & (bom["_norm_rule_item_desc"] == rule_norm)
        & (bom["Level"].map(level_match))
    ]
    if tag_include:
        wanted = {_norm(t) for t in tag_include}
        base = base[base["_norm_tag"].isin(wanted)]
    if tag_exclude:
        blocked = {_norm(t) for t in tag_exclude}
        base = base[~base["_norm_tag"].isin(blocked)]
    if require_non_empty_tag:
        base = base[base["_norm_tag"].astype(str).str.len() > 0]

    ext_sum = _sum_resource_cost(base, sources.cost_col)
    return ext_sum * target_lbs_each / ref_lbs_each


def _calc_for_item(row: pd.Series, sources: RfpPnlSources) -> dict[str, Optional[float] | str]:
    """Calculate all model-derived metrics for one item row.

    Reference SKU lbs per Each are read from the row (manual input only).
    Cost of Quality and Internal Logistics use the per-Category sum of
    ``Budget Value`` from ``Budget_Update.csv`` multiplied by the Target
    SKU lbs per Each, mirroring the snapshot's $/EA convention.
    """
    month = row.get("Month", "")
    plant = row.get("Plant", "")
    target_lbs = _to_float(row.get("Target SKU lbs per Each"))
    target_units = _to_float(row.get("Target SKU Volume (units)"))
    volume_pounds = None if target_lbs is None or target_units is None else target_lbs * target_units

    milk_ref = row.get("Milk Reference SKU", "")
    ingredient_ref = row.get("Ingredient Reference SKU", "") or milk_ref
    packaging_ref = row.get("Packaging Reference SKU", "") or milk_ref
    conversion_ref = row.get("Conversion Reference SKU", "") or milk_ref

    # Reference SKU lbs per Each is supplied manually by the analyst.
    # Ingredient / Packaging / Conversion all default to the Milk Reference
    # SKU lbs per Each when the analyst leaves them blank, mirroring the
    # default behaviour of the Reference SKU IDs themselves above.
    milk_ref_lbs = _to_float(row.get("Milk Reference SKU lbs per Each"))
    ingredient_ref_lbs = _to_float(row.get("Ingredient Reference SKU lbs per Each"))
    if ingredient_ref_lbs is None:
        ingredient_ref_lbs = milk_ref_lbs
    packaging_ref_lbs = _to_float(row.get("Packaging Reference SKU lbs per Each"))
    if packaging_ref_lbs is None:
        packaging_ref_lbs = milk_ref_lbs
    conversion_ref_lbs = _to_float(row.get("Conversion Reference SKU lbs per Each"))
    if conversion_ref_lbs is None:
        conversion_ref_lbs = milk_ref_lbs

    category = _lookup_category(sources, milk_ref)

    # Milk and Ingredient rule (BOM_History_Tracker_tagged.csv):
    # Per-anchor cost = Σ Ext Cost.1 (step 2) × Qty.1 (step 1)
    #                   ÷ Reference SKU lbs/Each
    #                   × Target SKU lbs/Each
    # See _two_step_cost docstring for the dimensional intuition.
    milk_default = _two_step_cost(
        sources,
        month=month,
        plant=plant,
        step1_rule_item_desc=milk_ref,
        step2_tag="Milk",
        ref_lbs_each=milk_ref_lbs,
        target_lbs_each=target_lbs,
    )
    ingredient_default = _two_step_cost(
        sources,
        month=month,
        plant=plant,
        step1_rule_item_desc=ingredient_ref,
        step2_tag="Ingredient",
        ref_lbs_each=ingredient_ref_lbs,
        target_lbs_each=target_lbs,
    )
    packaging_default = _one_step_cost(
        sources,
        month=month,
        plant=plant,
        rule_item_desc=packaging_ref,
        tag_include={"Packaging"},
        ref_lbs_each=packaging_ref_lbs,
        target_lbs_each=target_lbs,
    )
    # Conversion rule (BOM_History_Tracker_tagged.csv):
    #   Per Beg == Month
    #   Plant   == Plant
    #   Rule Item Desc == Conversion Reference SKU
    #   Level == 1
    #   Tag NOT IN {"Milk Component", "Ingredient", "Milk",
    #               "Packaging", "Depreciation"}
    #   Tag is NOT blank
    # Σ Ext Cost.1 ÷ Conversion Reference SKU lbs/Each × Target SKU
    # lbs/Each.
    #
    # Notes:
    #   * Depreciation is excluded because the bid model treats it as a
    #     non-conversion overhead.
    #   * The non-blank-Tag guard drops placeholder/rollup rows such as
    #     ``Ing-Rsrc Desc = "Upper Level Costs"`` whose Tag is empty and
    #     whose ``Ext Cost.1`` carries an unrelated recipe rollup value
    #     -- empty strings are not in the named exclude set, so without
    #     this guard those rollup rows would silently pollute the sum.
    conversion_default = _one_step_cost(
        sources,
        month=month,
        plant=plant,
        rule_item_desc=conversion_ref,
        tag_exclude=_CONVERSION_TAG_EXCLUDE,
        require_non_empty_tag=True,
        ref_lbs_each=conversion_ref_lbs,
        target_lbs_each=target_lbs,
    )

    # Cost of Quality and Internal Logistics are stored in Budget_Update as
    # $/lb. Multiply by Target SKU lbs/Each to convert to the $/EA scale
    # used by every other row in the Target SKU P&L block.
    coq_per_lb = _budget_value(sources, category, "Cost of Quality")
    internal_per_lb = _budget_value(sources, category, "Internal Logistics (Shuttling & WHSE)")
    coq_default = None if target_lbs is None else coq_per_lb * target_lbs
    internal_default = (
        None if target_lbs is None else internal_per_lb * target_lbs
    )

    # Apply override precedence: blank override → BOM/Budget default,
    # non-blank parseable override → wins. Logic is centralized in
    # ``_apply_override`` so the rule is provably consistent across all
    # six components.
    milk = _apply_override(milk_default, row.get("Milk Override"))
    ingredient = _apply_override(
        ingredient_default, row.get("Ingredient Override")
    )
    packaging = _apply_override(
        packaging_default, row.get("Packaging Override")
    )
    conversion = _apply_override(
        conversion_default, row.get("Conversion Cost Override")
    )
    coq = _apply_override(coq_default, row.get("Cost of Quality Override"))
    internal = _apply_override(
        internal_default,
        row.get("Internal Logistics (Shuttling & WHSE) Override"),
    )

    other = _to_float(row.get("Other Cost")) or 0.0
    pcm_lbs = _to_float(row.get("PCM $/lbs"))
    pcm = None if pcm_lbs is None or target_lbs is None else pcm_lbs * target_lbs

    fob = None
    if all(v is not None for v in (milk, ingredient, packaging, pcm)):
        fob = float(milk) + float(ingredient) + float(packaging) + float(pcm)

    total_costs = None
    if all(v is not None for v in (milk, ingredient, packaging, conversion, coq, internal)):
        total_costs = (
            float(milk) + float(ingredient) + float(packaging) + float(conversion)
            + float(coq) + float(internal) + other
        )

    gp = None if fob is None or total_costs is None else fob - total_costs
    pcm_pct = _safe_div(pcm, fob)
    gp_lbs = _safe_div(gp, target_lbs)
    gp_pct = _safe_div(gp, fob)

    # Retail-side metrics. Freight defaults to $0/EA when blank (per
    # spec); Delivered Price is therefore well-defined whenever FOB is.
    # Retailer's Margin% requires a non-zero Retail Price; otherwise the
    # division would be undefined and we surface a blank cell.
    retail = _to_float(row.get("Retail Price"))
    freight = _to_float(row.get("Freight Cost"))
    freight_for_calc = 0.0 if freight is None else freight
    delivered = None if fob is None else fob + freight_for_calc
    retailer_margin = (
        None
        if delivered is None or retail is None or retail == 0
        else (retail - delivered) / retail
    )

    return {
        "Target SKU Volume (pounds)": volume_pounds,
        "Ingredient Reference SKU": ingredient_ref,
        "Packaging Reference SKU": packaging_ref,
        "Conversion Reference SKU": conversion_ref,
        "Category": category,
        "Milk": milk,
        "Ingredient": ingredient,
        "Packaging": packaging,
        "Conversion Cost": conversion,
        "Cost of Quality": coq,
        "Internal Logistics (Shuttling & WHSE)": internal,
        "FOB Price": fob,
        "Total Costs": total_costs,
        "PCM": pcm,
        "PCM%": pcm_pct,
        "GP": gp,
        "GP $/lbs": gp_lbs,
        "GP%": gp_pct,
        "Delivered Price": delivered,
        "Retailer's Margin%": retailer_margin,
    }


# Inputs that the user *must* supply — without them the calc engine
# cannot produce FOB / PCM% / GP / Delivered Price / Retailer's
# Margin%. The page surfaces missing values as a warning above the
# scenario table so the analyst is prompted before they spot blank
# cells. Ingredient / Packaging / Conversion lbs/Each are NOT required
# because they inherit Milk Reference SKU lbs/Each when blank.
REQUIRED_INPUT_FIELDS: tuple[str, ...] = (
    "Milk Reference SKU lbs per Each",
    "PCM $/lbs",
)


def find_missing_required_inputs(
    items_df: pd.DataFrame,
) -> list[tuple[str, list[str]]]:
    """Return a list of ``(item_label, [missing_field, ...])`` pairs.

    ``item_label`` falls back to ``"Item N"`` when the row's
    ``Target SKU Name`` is blank, mirroring the labelling convention
    used by the per-item input panel. Items with no missing required
    inputs are omitted, so the caller can simply check truthiness of
    the return value to decide whether to render the prompt.
    """
    coerced = _coerce_scenario_df(items_df)
    issues: list[tuple[str, list[str]]] = []
    for idx, row in coerced.iterrows():
        missing = [
            field for field in REQUIRED_INPUT_FIELDS
            if _to_float(row.get(field)) is None
        ]
        if missing:
            label = str(row.get("Target SKU Name", "") or "").strip() \
                or f"Item {idx + 1}"
            issues.append((label, missing))
    return issues


def recompute_items(items_df: pd.DataFrame, sources: RfpPnlSources) -> pd.DataFrame:
    """Recompute scenario defaults and strict formulas for all item rows.

    Rules:
    * STRICT_CALC_METRICS are always overwritten.
    * DEFAULTABLE_METRICS are only filled when the current cell is blank.
    * Non-derived fields (manual inputs and override columns) are
      preserved as-entered.
    """
    out = _coerce_scenario_df(items_df)
    for idx, row in out.iterrows():
        calc = _calc_for_item(row, sources)
        for metric, value in calc.items():
            if metric in STRICT_CALC_METRICS:
                out.at[idx, metric] = value if value is not None else ""
            elif metric in DEFAULTABLE_METRICS and _is_blank(out.at[idx, metric]):
                out.at[idx, metric] = value if value is not None else ""
    return out


# ─── Multi-Scenario Summary builder ──────────────────────────────────────────
#
# Drives the "Multi-Scenario Summary" UI. Pure data; the page layer does
# the multiselects and rendering. The function is intentionally tolerant
# of stale / partially-populated scenario CSVs: it always recomputes each
# scenario through ``recompute_items`` first so the summary reflects the
# current calc engine, never values frozen into a saved CSV before a
# formula change.

#: Per-item summary rows shown for every (scenario, item) pair.
#: ``FOB Revenue`` makes it explicit that the figure is FOB-based and
#: doesn't include freight / retailer markup. ``Volume (pounds)``
#: spells out the unit so it's not confused with units / cases.
SUMMARY_PER_ITEM_METRICS: tuple[str, ...] = (
    "Volume (pounds)", "FOB Price", "FOB Revenue", "PCM%", "GP%",
)

#: Total roll-up rows. Volume (pounds) / FOB Revenue / GP are SUMS
#: across items in *dollars* (Total GP = Σ GP $/EA × units, NOT a sum
#: of per-EA values). PCM% and GP% are volume-weighted (lbs) so they
#: reflect the realized portfolio profitability, not a misleading
#: equal-weighted average.
SUMMARY_TOTAL_METRICS: tuple[str, ...] = (
    "Volume (pounds)", "FOB Revenue", "GP", "PCM%", "GP%",
)

SUMMARY_TOTAL_LABEL = "Total"


def _summary_per_item_values(item_row: pd.Series) -> dict[str, Optional[float]]:
    """Derive the five per-item summary metrics from a recomputed item row.

    All inputs are tolerated as raw cell values (strings or numbers) and
    coerced via ``_to_float``. Returns ``None`` for any metric whose
    inputs are missing so the summary table can render blank cells
    cleanly rather than $0 placeholders.
    """
    units = _to_float(item_row.get("Target SKU Volume (units)"))
    lbs_each = _to_float(item_row.get("Target SKU lbs per Each"))
    fob = _to_float(item_row.get("FOB Price"))
    pcm_pct = _to_float(item_row.get("PCM%"))
    gp_pct = _to_float(item_row.get("GP%"))

    volume_lbs = None if units is None or lbs_each is None else units * lbs_each
    revenue = None if fob is None or units is None else fob * units
    # ``_units`` is preserved on the per-item record (not displayed) so
    # the Total roll-up can compute Total GP = Σ (GP $/EA × units), i.e.
    # actual GP dollars, rather than mistakenly summing per-EA values.
    return {
        "Volume (pounds)": volume_lbs,
        "FOB Price": fob,
        "FOB Revenue": revenue,
        "PCM%": pcm_pct,
        "GP%": gp_pct,
        "_units": units,
    }


def _weighted_average(values: list[float], weights: list[float]) -> Optional[float]:
    """Volume-weighted average. Returns ``None`` when no weight is positive."""
    paired = [
        (v, w) for v, w in zip(values, weights)
        if v is not None and w is not None and w > 0
    ]
    if not paired:
        return None
    weight_sum = sum(w for _, w in paired)
    if weight_sum == 0:
        return None
    return sum(v * w for v, w in paired) / weight_sum


def _summary_total_values(per_item_records: list[dict]) -> dict[str, Optional[float]]:
    """Aggregate the per-item records into the Total row metrics.

    Volume (pounds) / FOB Revenue are simple sums. **Total GP** is the
    sum of *dollar* GP across items: ``Σ (GP $/EA × units)``. Summing
    the per-EA GP values directly would mix items of different sizes
    and hide the portfolio's actual profit. PCM% and GP% are weighted
    by Volume (lbs) so the portfolio rate reflects revenue/profit-mix
    rather than item count.
    """
    volumes = [r.get("Volume (pounds)") for r in per_item_records]
    revenues = [r.get("FOB Revenue") for r in per_item_records]
    gp_per_each = [r.get("GP") for r in per_item_records]
    units = [r.get("_units") for r in per_item_records]
    pcm_pcts = [r.get("PCM%") for r in per_item_records]
    gp_pcts = [r.get("GP%") for r in per_item_records]

    def _sum(xs: list) -> Optional[float]:
        clean = [x for x in xs if x is not None]
        return sum(clean) if clean else None

    gp_dollars = [
        gp * u
        for gp, u in zip(gp_per_each, units)
        if gp is not None and u is not None
    ]
    total_gp_dollars = sum(gp_dollars) if gp_dollars else None

    weights = [v if v is not None else 0.0 for v in volumes]
    return {
        "Volume (pounds)": _sum(volumes),
        "FOB Revenue": _sum(revenues),
        "GP": total_gp_dollars,
        "PCM%": _weighted_average(pcm_pcts, weights),
        "GP%": _weighted_average(gp_pcts, weights),
    }


def _enrich_with_gp_dollars(item_row: pd.Series) -> Optional[float]:
    """Per-item GP $ used by the Total roll-up (not displayed per item)."""
    return _to_float(item_row.get("GP"))


# ─── BOM Search (powers the "BOM Search" UI) ─────────────────────────────────
#
# A standalone browser over ``BOM_History_Tracker_tagged.csv``. Given a
# Month + Plant + Level-1 Item Description the analyst gets two extracts:
#
#   * Level 1 — the matching Level-1 rows for that Item Description.
#   * Level 2 — the chained sub-recipe rows, reached by following each
#     Level-1 row's ``Ing-Rsrc Desc`` into ``Rule Item Desc`` at a level
#     containing "2", scoped to the same ``Top Recipe`` as the anchor so a
#     sibling finished good that shares the sub-recipe isn't pulled in.
#
# Unlike the cost engine, the search applies NO Tag filter — it is a raw
# data-browsing aid, not a cost lookup.

#: BOM columns surfaced by the search, in the order an analyst expects.
#: Normalized helper columns (``_norm_*`` / ``_level_text``) added by
#: :func:`load_sources` are intentionally excluded.
_BOM_OUTPUT_COLS_ORDER: tuple[str, ...] = (
    "Cldr", "Period", "Per Beg",
    "Plant", "Top Recipe",
    "Level", "Recipe", "Rule Item Desc", "Item Desc",
    "Ing-Rsrc Desc",
    "Qty", "UM.1", "Qty.1", "UM.2",
    "Unit Cost", "Ext Cost",
    "Scrap Factor",
    "Unit Cost.1", "Ext Cost.1",
    "Tag",
)


def _project_bom_cols(df: pd.DataFrame) -> pd.DataFrame:
    """Return only the analyst-relevant BOM columns, in canonical order."""
    cols = [c for c in _BOM_OUTPUT_COLS_ORDER if c in df.columns]
    return df.loc[:, cols].copy()


def _level1_item_descs(
    sources: RfpPnlSources,
    *,
    month: object,
    plant: object,
    require_in_pdh: bool,
) -> tuple[str, ...]:
    """Unique Level-1 ``Rule Item Desc`` values for a Month + Plant.

    Powers two cascading dropdowns:

    * **Reference SKU** dropdowns (``require_in_pdh=True``) — restricted to
      values that also exist in the PDH ``Item Description`` column, since
      every Reference SKU must resolve to a PDH Category.
    * **BOM Search** Item Description dropdown (``require_in_pdh=False``) —
      the full Level-1 list, as the search is a raw BOM browser.

    Returns an empty tuple when either Month or Plant is blank so the
    dependent dropdown stays empty until both parents are chosen.
    """
    m_norm = _norm(month)
    p_norm = _norm(plant)
    if not (m_norm and p_norm):
        return ()

    bom = sources.bom_df
    mask = (
        (bom["_norm_month"] == m_norm)
        & (bom["_norm_plant"] == p_norm)
        & (bom["Level"].map(_LEVEL_EQUALS_1))
    )
    descs = [
        str(v).strip()
        for v in bom.loc[mask, "Rule Item Desc"].tolist()
        if str(v).strip()
    ]
    # ``dict.fromkeys`` dedupes while preserving first-seen order; we then
    # sort case-insensitively for a stable, analyst-friendly dropdown.
    unique = list(dict.fromkeys(descs))
    if require_in_pdh:
        unique = [d for d in unique if _norm(d) in sources.pdh_item_desc_set]
    return tuple(sorted(unique, key=lambda x: x.casefold()))


def reference_sku_options(
    sources: RfpPnlSources, *, month: object, plant: object
) -> tuple[str, ...]:
    """Reference SKU dropdown values: Level-1 BOM items present in PDH."""
    return _level1_item_descs(
        sources, month=month, plant=plant, require_in_pdh=True
    )


def bom_search_item_options(
    sources: RfpPnlSources, *, month: object, plant: object
) -> tuple[str, ...]:
    """BOM Search Item Description dropdown values: all Level-1 BOM items."""
    return _level1_item_descs(
        sources, month=month, plant=plant, require_in_pdh=False
    )


def bom_search(
    sources: RfpPnlSources,
    *,
    month: object,
    plant: object,
    item_desc: object,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return ``(level1_rows, level2_rows)`` for the BOM Search filters.

    Level 1 = ``Per Beg`` = month, ``Plant`` = plant, ``Rule Item Desc`` =
    ``item_desc``, ``Level`` == 1. Level 2 follows each Level-1 row's
    ``Ing-Rsrc Desc`` chain key into ``Rule Item Desc`` at a level
    containing "2", scoped to the anchor's ``Top Recipe``; rows are deduped
    by their BOM index. Any blank filter yields header-only frames so a
    download still carries column headers.
    """
    bom = sources.bom_df
    empty = _project_bom_cols(bom.iloc[0:0])

    m_norm = _norm(month)
    p_norm = _norm(plant)
    rule_norm = _norm(item_desc)
    if not (m_norm and p_norm and rule_norm):
        return empty, empty

    level1 = bom[
        (bom["_norm_month"] == m_norm)
        & (bom["_norm_plant"] == p_norm)
        & (bom["_norm_rule_item_desc"] == rule_norm)
        & (bom["Level"].map(_LEVEL_EQUALS_1))
    ]
    if level1.empty:
        return empty, empty

    # Walk each Level-1 anchor into its Level-2 sub-recipe rows. Collect
    # indices in a set so anchors that share a sub-recipe don't duplicate.
    level2_idx: set = set()
    for _, row in level1.iterrows():
        chain_desc = _norm(row.get("Ing-Rsrc Desc"))
        anchor_top_recipe = _norm(row.get("Top Recipe"))
        if not chain_desc:
            continue
        step2 = bom[
            (bom["_norm_month"] == m_norm)
            & (bom["_norm_plant"] == p_norm)
            & (bom["_norm_rule_item_desc"] == chain_desc)
            & (bom["Level"].map(_LEVEL_CONTAINS_2))
        ]
        if anchor_top_recipe and "_norm_top_recipe" in bom.columns:
            step2 = step2[step2["_norm_top_recipe"] == anchor_top_recipe]
        level2_idx.update(step2.index.tolist())

    level2 = bom.loc[sorted(level2_idx)] if level2_idx else bom.iloc[0:0]
    return _project_bom_cols(level1), _project_bom_cols(level2)


def summarize_scenarios(
    scenarios: dict[str, pd.DataFrame],
    sources: RfpPnlSources,
    *,
    items_filter: Optional[Iterable[str]] = None,
    categories_filter: Optional[Iterable[str]] = None,
) -> pd.DataFrame:
    """Build the Multi-Scenario Summary table.

    Parameters
    ----------
    scenarios:
        ``{scenario_display_name: items_df}``. Each DataFrame is the raw
        items table as persisted in ``Files/Program_Bid_Management/...``.
        We recompute each one through ``recompute_items`` so the summary
        reflects the live calc engine, never stale persisted values.
    sources:
        Source frames + lookup maps from :func:`load_sources`.
    items_filter / categories_filter:
        Optional. ``None`` or empty iterable means "include all". We
        match on ``Target SKU Name`` and ``Category`` exactly (raw
        values, no casefolding) to keep filter labels round-trippable.

    Returns
    -------
    pd.DataFrame
        Long-format frame with columns
        ``["Item", "Category", "Metric", *scenario_names]``. Per-item
        rows come first (one row per ``(item, metric)`` pair across
        :data:`SUMMARY_PER_ITEM_METRICS`), followed by Total rows
        (one row per metric in :data:`SUMMARY_TOTAL_METRICS`).
    """
    if not scenarios:
        return pd.DataFrame(columns=["Item", "Category", "Metric"])

    items_set = set(items_filter) if items_filter else None
    categories_set = set(categories_filter) if categories_filter else None

    # ── Step 1. Recompute each scenario, then derive summary metrics
    #            per item. ``per_scenario`` maps scenario → list of dicts.
    per_scenario: dict[str, list[dict]] = {}
    # Track the canonical (Item, Category) pairs in stable order — items
    # may appear in multiple scenarios and we want each (Item, Category)
    # to render exactly once in the summary table, in the order of
    # *first* appearance across scenarios.
    canonical_keys: list[tuple[str, str]] = []
    seen_keys: set[tuple[str, str]] = set()

    for name, items_df in scenarios.items():
        recomputed = recompute_items(items_df, sources)
        records: list[dict] = []
        for _, row in recomputed.iterrows():
            item_name = str(row.get("Target SKU Name", "") or "").strip()
            category = str(row.get("Category", "") or "").strip()
            if not item_name:
                continue
            if items_set is not None and item_name not in items_set:
                continue
            if categories_set is not None and category not in categories_set:
                continue
            metrics = _summary_per_item_values(row)
            metrics["GP"] = _enrich_with_gp_dollars(row)
            metrics["Item"] = item_name
            metrics["Category"] = category
            records.append(metrics)
            key = (item_name, category)
            if key not in seen_keys:
                seen_keys.add(key)
                canonical_keys.append(key)
        per_scenario[name] = records

    # ── Step 2. Per-item rows: one row per (Item, Category, metric).
    rows: list[dict] = []
    for item_name, category in canonical_keys:
        for metric in SUMMARY_PER_ITEM_METRICS:
            row: dict[str, object] = {
                "Item": item_name, "Category": category, "Metric": metric,
            }
            for scenario_name in scenarios:
                match = next(
                    (
                        r for r in per_scenario[scenario_name]
                        if r["Item"] == item_name and r["Category"] == category
                    ),
                    None,
                )
                row[scenario_name] = None if match is None else match.get(metric)
            rows.append(row)

    # ── Step 3. Total rows: one row per total metric, aggregated over
    #            the (already filtered) per-scenario records.
    for metric in SUMMARY_TOTAL_METRICS:
        row = {
            "Item": SUMMARY_TOTAL_LABEL, "Category": "", "Metric": metric,
        }
        for scenario_name in scenarios:
            totals = _summary_total_values(per_scenario[scenario_name])
            row[scenario_name] = totals.get(metric)
        rows.append(row)

    return pd.DataFrame(
        rows,
        columns=["Item", "Category", "Metric", *scenarios.keys()],
    )

