"""
RO Comparison data plumbing for Demand Planner Analytics.

This module owns every Microsoft Fabric Lakehouse I/O and pure-pandas
transformation that the *RO Comparison* section on the Demand Planner
Analytics page needs.  Splitting the logic out of the page renderer
keeps the UI code thin, makes the comparison algorithm trivially
unit-testable, and concentrates Fabric-specific knowledge in one place.

Data surfaces (all under one OneLake lakehouse — see ``_WORKSPACE_GUID``
/ ``_LAKEHOUSE_GUID``)
-----------------------------------------------------------------------
READ
* ``Files/RO Tracking/RO_History_Tracker.csv`` — the master per-month
  per-RO tracker (1 row per ship-period per RO Key).
* ``dbo.dp_dimitems`` Delta table — item dimension that supplies
  Portfolio Major / Portfolio Minor / Supply Format for the comparison
  enrichment via the ``Item Code`` key.

WRITE
* ``Files/RO Tracking/Append_New_History/<uploaded_filename>`` — drop
  zone for the local *Customer Input* CSV the demand planner uploads
  each month.
* ``Files/RO Tracking/RO_Reporting/RO_Comparison_Output.csv`` — the
  current published comparison; rewritten on every Save click.

Module layout
-------------
* Constants + typed error
* ``ComparisonWarnings`` value object (everything-that-needs-attention)
* Pure helpers (no I/O, no Streamlit) — date / brand / item-id /
  numeric coercion + safe division
* Per-month aggregation (``_aggregate_one_month``)
* Public pure transform (``build_ro_comparison``,
  ``_compute_driver``, ``_enrich_portfolio_supply``,
  ``_recompute_derived_columns``)
* Streamlit-cached Fabric I/O wrappers
  (``fetch_ro_history_df``, ``fetch_dimitems_df``,
  ``fetch_ro_item_master_df``, ``fetch_ro_item_master_raw_bytes``,
  ``upload_customer_input``,
  ``save_ro_comparison_output``, ``list_months``)
* ``__all__`` contract

Cloud notes
-----------
* All readers route through ``data_sources.fabric_lakehouse_io`` /
  ``data_sources.fabric_auth`` so the section reuses the warm DuckDB
  connection + bearer token shared with HTST / IBP / Milk Mover.  This
  means a planner who is already signed in for any other Fabric-backed
  page pays zero additional auth latency here.
* TTL caches: 15 min for the RO_History CSV (matches IBP cadence) and
  60 min for the dp_dimitems Delta scan (dim tables change rarely).
* The pure-transform path is fully synchronous and CPU-bound.  Hot
  re-runs (filter clicks, cell edits) bypass Fabric entirely because
  the summary frame lives in ``st.session_state`` keyed by the
  selected (Prior, LE) month pair.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Optional

import pandas as pd
import streamlit as st

from data_sources.fabric_auth import (
    FabricAuthError,
    acquire_storage_token,
    bind_storage_token,
    duckdb_lock,
    get_duckdb_connection,
    read_section,
)
from data_sources.fabric_lakehouse_io import (
    LakehouseFile,
    LakehouseIOError,
    archive_bytes,
    get_file_properties,
    list_files,
    read_bytes,
    read_csv,
    write_bytes,
    write_csv,
)
from data_sources.fabric_tls import (
    apply_ca_cert_env,
    resolve_ca_cert_file,
    ssl_verify_enabled,
)


logger = logging.getLogger(__name__)


# ── Configuration ────────────────────────────────────────────────────────────
#
# We piggyback on the ``[fabric_htst]`` secrets block — same pattern as every
# other lakehouse-backed connector in this codebase.  See
# ``fabric_lakehouse_io._read_lakehouse_config`` for the inheritance rules.
_SECRETS_SECTION: str = "fabric_htst"

# Source / sink POSIX paths under ``Files/``.
_RO_HISTORY_BLOB_PATH: str = "RO Tracking/RO_History_Tracker.csv"
_APPEND_NEW_HISTORY_FOLDER: str = "RO Tracking/Append_New_History"
_RO_REPORTING_BLOB_PATH: str = "RO Tracking/RO_Reporting/RO_Comparison_Output.csv"
_RO_ITEM_MASTER_BLOB_PATH: str = "RO Tracking/RO_Item_Master.csv"
# Append-only archive of planner "RO Pipeline Review" snapshots — one
# timestamped CSV per Refresh click (see :func:`save_pipeline_review_snapshot`).
_RO_PIPELINE_REVIEW_ARCHIVE_DIR: str = "RO Tracking/RO Pipeline Review Archive"

# Sidecar storing the SHA-256 fingerprint of the RO_History snapshot
# that the current ``RO_Comparison_Output.csv`` was generated from.
# Hidden-style filename (leading dot) signals "metadata, not for the
# planner to open by hand" — same convention as ``.gitignore`` etc.
# Used by :func:`auto_regenerate_if_history_changed` to detect when
# RO_History has been refreshed since the last comparison Save and
# trigger an automatic overwrite (per the planner's published spec).
_HISTORY_FINGERPRINT_BLOB_PATH: str = (
    "RO Tracking/RO_Reporting/.history_fingerprint.txt"
)

# Workspace + Lakehouse GUIDs for the ``dbo.dp_dimitems`` Delta table.
# Same Fabric lakehouse as the IBP connector — see the rationale in
# ``data_sources/ibp_official.py`` for preferring GUIDs to display names.
_WORKSPACE_GUID: str = "bb11c51d-03c8-4f1b-938c-e20657a8f31d"
_LAKEHOUSE_GUID: str = "a01f513d-eee7-41eb-8c15-670bc40e7fc8"
_DIMITEMS_SCHEMA: str = "dbo"
_DIMITEMS_TABLE: str = "dp_dimitems"

# Cache TTLs — see module docstring for sizing rationale.
# RO_Item_Master shares the dp_dimitems TTL because both are dim-style
# tables that change infrequently and are exercised by the same
# enrichment pass.
_HISTORY_CACHE_TTL_SECONDS: int = 15 * 60
_DIMITEMS_CACHE_TTL_SECONDS: int = 60 * 60
_ITEM_MASTER_CACHE_TTL_SECONDS: int = 60 * 60

# Brand spellings we normalise to "Private".  Comparison is
# case-insensitive (we lower() before lookup).
_PRIVATE_BRAND_TOKENS: frozenset[str] = frozenset({
    "pl", "private label", "private-label", "privatelabel",
})

# Excel's date epoch — 1899-12-30 corrects for the Lotus-1-2-3 leap-year
# bug that Excel inherited.  Pandas Timestamp.fromordinal does not
# account for that bug, so we add the days as a Timedelta from this
# anchor instead.
_EXCEL_EPOCH: pd.Timestamp = pd.Timestamp("1899-12-30")

# Excel serial range we accept.  Below 367 we'd misinterpret very small
# integers as 1900-ish dates; above ~80000 we'd manufacture dates beyond
# the year 2118 from genuine non-serial numbers.  Real-world ship dates
# fall comfortably inside this window.
_EXCEL_SERIAL_MIN: float = 367.0       # 1901-01-01
_EXCEL_SERIAL_MAX: float = 80000.0     # 2118-12-15

# Numeric columns we sum when collapsing multiple period-rows per RO Key
# inside one snapshot.
_NUMERIC_SUM_COLUMNS: tuple[str, ...] = (
    "Lbs./yr",
    "Lbs./yr Exp",
    "FY Lbs. Exp",
)

# Output column order — 28 columns, second "Brand" column intentionally
# dropped per spec.  The legacy "Lbs./yr" / "Lbs./yr Exp" / "FY Lbs. Exp"
# names from the original RO_Comparison_Output template are renamed to
# planner-friendly equivalents per spec:
#
#   Lbs./yr      -> Annual Opportunity (lbs)
#   Lbs./yr Exp  -> Year1 Probabilized Lbs
#   FY Lbs. Exp  -> Current Fiscal Probabilized Lbs   (drops the now-redundant "FY" qualifier)
#
# These names appear verbatim in the data editor, subtotal, per-Format
# summary, saved CSV, and filter widgets — keep them in sync if you ever
# change one place.
ANNUAL_OPP_PRIOR    = "Prior Annual Opportunity (lbs)"
ANNUAL_OPP_LE       = "LE Annual Opportunity (lbs)"
ANNUAL_OPP_CHANGE   = "Change Annual Opportunity (lbs)"
YEAR1_PROB_PRIOR    = "Prior Year1 Probabilized Lbs"
YEAR1_PROB_LE       = "LE Year1 Probabilized Lbs"
YEAR1_PROB_CHANGE   = "Change Year1 Probabilized Lbs"
CUR_FISCAL_PROB_PRIOR  = "Prior Current Fiscal Probabilized Lbs"
CUR_FISCAL_PROB_LE     = "LE Current Fiscal Probabilized Lbs"
CUR_FISCAL_PROB_CHANGE = "Change Current Fiscal Probabilized Lbs"

OUTPUT_COLUMNS: tuple[str, ...] = (
    "Format", "Customer", "Taxonomy", "Brand", "Item #", "Description",
    "Prior RO Key", "LE RO Key", "Driver",
    ANNUAL_OPP_PRIOR, ANNUAL_OPP_LE, ANNUAL_OPP_CHANGE,
    YEAR1_PROB_PRIOR, YEAR1_PROB_LE, YEAR1_PROB_CHANGE,
    CUR_FISCAL_PROB_PRIOR, CUR_FISCAL_PROB_LE, CUR_FISCAL_PROB_CHANGE,
    "Prior Probability", "LE Probability", "Change Probability",
    "Prior First Ship Date", "LE First Ship Date", "Change (Days)",
    "Existing SKUs", "Portfolio Major", "Portfolio Minor", "Supply Format",
)

# Columns that get summed for the subtotal row beneath the editable table.
SUBTOTAL_COLUMNS: tuple[str, ...] = (
    ANNUAL_OPP_PRIOR, ANNUAL_OPP_LE, ANNUAL_OPP_CHANGE,
    YEAR1_PROB_PRIOR, YEAR1_PROB_LE, YEAR1_PROB_CHANGE,
    CUR_FISCAL_PROB_PRIOR, CUR_FISCAL_PROB_LE, CUR_FISCAL_PROB_CHANGE,
)


# ── Errors ───────────────────────────────────────────────────────────────────

class RoComparisonError(RuntimeError):
    """Raised on any RO-Comparison-specific failure.

    Wraps the lower-level :class:`LakehouseIOError` / :class:`FabricAuthError`
    so the page renders a single, scope-aware banner without leaking
    auth chain diagnostics into the section body.
    """


# ── Warnings value object ────────────────────────────────────────────────────

@dataclass
class ComparisonWarnings:
    """Structured warnings produced by :func:`build_ro_comparison`.

    Each ``list[str]`` holds the affected ``Item #`` values (deduped,
    sorted) so the page can render a single banner that references the
    exact rows the planner must inspect.

    Boolean / free-text fields capture failures that are not
    item-scoped (e.g. "dp_dimitems could not be loaded at all").
    """

    missing_brand: list[str] = field(default_factory=list)
    missing_portfolio: list[str] = field(default_factory=list)
    missing_supply_format: list[str] = field(default_factory=list)
    unparseable_dates: list[str] = field(default_factory=list)
    unparseable_numerics: list[str] = field(default_factory=list)
    dimitems_unavailable: bool = False
    extras: list[str] = field(default_factory=list)

    def has_any(self) -> bool:
        """True when at least one warning needs surfacing to the user."""
        return (
            bool(self.missing_brand)
            or bool(self.missing_portfolio)
            or bool(self.missing_supply_format)
            or bool(self.unparseable_dates)
            or bool(self.unparseable_numerics)
            or self.dimitems_unavailable
            or bool(self.extras)
        )


# ── Pure helpers — no I/O, no Streamlit ──────────────────────────────────────

def _is_blank(value: Any) -> bool:
    """Return True when *value* should be treated as an empty cell.

    Centralised because pandas can hand us ``None``, ``NaN``, ``NaT`` or
    the literal placeholder strings ``""`` / ``"-"``; each of these
    means "no data" in the upstream Excel-shaped CSV.
    """
    if value is None:
        return True
    try:
        if pd.isna(value):
            return True
    except (TypeError, ValueError):
        pass
    if isinstance(value, str) and value.strip() in ("", "-"):
        return True
    return False


def _coerce_to_date(value: Any) -> Optional[date]:
    """Best-effort coerce *value* to a Python :class:`date` or ``None``.

    Handles every shape the RO_History feed has historically used:

    1. Excel serials (e.g. ``46388`` → ``2026-12-15``)
    2. MDY / YMD strings (``4/1/2026``, ``05/01/2026``, ``2026-04-01``)
    3. Already-typed :class:`datetime` / :class:`pd.Timestamp` / :class:`date`

    Returns ``None`` for blanks, NaT, and unparseable input — never
    raises.  Caller can therefore safely ``.map(_coerce_to_date)`` a
    whole Series without try/except.
    """
    if _is_blank(value):
        return None

    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, pd.Timestamp):
        return value.date()
    if isinstance(value, date):
        return value

    # Excel serial path — accept int or float string forms.
    try:
        as_float = float(value)
        if _EXCEL_SERIAL_MIN <= as_float <= _EXCEL_SERIAL_MAX:
            return (_EXCEL_EPOCH + pd.Timedelta(days=int(as_float))).date()
    except (TypeError, ValueError):
        pass

    # Generic textual parser — pandas handles MDY, YMD, ISO, etc.
    try:
        parsed = pd.to_datetime(value, errors="coerce")
        if pd.isna(parsed):
            return None
        return parsed.date()
    except (TypeError, ValueError):
        return None


def _normalize_brand(value: Any) -> str:
    """Map ``PL`` / ``Pl`` / ``pl`` / ``Private Label`` → ``"Private"``.

    Returns ``""`` for blanks so the page can surface a "fill me in"
    warning for the affected rows.  All other values are passed through
    with surrounding whitespace stripped.
    """
    if _is_blank(value):
        return ""
    s = str(value).strip()
    if s.lower() in _PRIVATE_BRAND_TOKENS:
        return "Private"
    return s


def _normalize_item_id(value: Any) -> str:
    """Normalise an Item # / Item Code to a stable string key.

    The CSV gives us ints (``370072``), floats (``370072.0`` when the
    column also contains NaN) and strings (``"370072"``).  We collapse
    all three to ``"370072"`` so a dictionary lookup against
    ``dp_dimitems.Item Code`` is exact regardless of source dtype.
    """
    if _is_blank(value):
        return ""
    s = str(value).strip()
    if not s:
        return ""
    # Strip the trailing ".0" pandas inserts when an integer column gets
    # coerced to float by the presence of a NaN elsewhere.
    if s.endswith(".0") and s[:-2].lstrip("-").isdigit():
        s = s[:-2]
    return s


def _safe_div(numerator: pd.Series, denominator: pd.Series) -> pd.Series:
    """Element-wise division with Excel's ``IFERROR(..., 0)`` semantics.

    Used for ``Probability = Lbs./yr Exp / Lbs./yr`` (per the planner's
    formula).  Any non-finite result (NaN, ±inf, divide-by-zero)
    collapses to ``0.0`` so the rendered cell is never blank or
    ``"inf"``.
    """
    num = pd.to_numeric(numerator, errors="coerce").fillna(0.0)
    den = pd.to_numeric(denominator, errors="coerce").fillna(0.0)
    # ``divide`` propagates inf / NaN; we explicitly squash both back
    # to zero so the IFERROR contract holds.
    result = num.divide(den)
    return (
        result.replace([float("inf"), float("-inf")], 0.0)
              .fillna(0.0)
    )


def _coerce_optional_date(value: Any) -> Optional[date]:
    """Public-ish alias used by helpers below — kept thin for clarity."""
    return _coerce_to_date(value)


def _date_diff_days(prior_series: pd.Series, le_series: pd.Series) -> pd.Series:
    """Compute ``(LE − Prior).days`` per row; 0 when either side is missing.

    Operates element-wise to tolerate object-dtype columns that mix
    :class:`date`, :class:`pd.Timestamp`, ``None`` and ``NaT`` in the
    same Series — which is the natural state after our aggregation.
    """
    out: list[int] = []
    for prior_val, le_val in zip(prior_series, le_series):
        prior_d = _coerce_optional_date(prior_val)
        le_d = _coerce_optional_date(le_val)
        if prior_d is not None and le_d is not None:
            out.append((le_d - prior_d).days)
        else:
            out.append(0)
    return pd.Series(out, index=prior_series.index, dtype="int64")


# ── Per-month aggregation ────────────────────────────────────────────────────

def _aggregate_one_month(month_df: pd.DataFrame) -> pd.DataFrame:
    """Collapse multiple period-rows per RO Key into one row per RO Key.

    A single snapshot in ``RO_History_Tracker.csv`` can contain several
    rows that share an RO Key when the RO is sliced across ship-date
    buckets.  We roll those up so the comparison table has exactly one
    row per (snapshot, RO Key).

    Aggregation rules
    -----------------
    * ``Lbs./yr``, ``Lbs./yr Exp``, ``FY Lbs. Exp``       — sum
    * ``First Ship Date``                                 — earliest
      non-null date in the snapshot (we surface the earliest ship date
      in the planner's comparison view)
    * Invariant identifiers (``Format``, ``Customer``, ``Taxonomy``,
      ``Brand``, ``Item #``, ``Item Desc``)              — first
      non-null observed value (these are expected to be constant
      within an RO Key in a single snapshot)

    Returns
    -------
    pd.DataFrame
        Indexed by ``RO Key`` (int).  Columns mirror the source plus
        the normalised numeric/date types.  Returns an empty frame when
        ``month_df`` is empty.
    """
    if month_df.empty:
        return pd.DataFrame()

    def _first_non_null(series: pd.Series) -> Any:
        non_null = series.dropna()
        return non_null.iloc[0] if len(non_null) else None

    def _earliest(series: pd.Series) -> Any:
        # ``min`` skips NaT/None on object dtype only when comparison is
        # well-defined; we drop blanks first to be safe.
        non_null = series.dropna().tolist()
        if not non_null:
            return None
        return min(non_null)

    # ``**{}`` splat is needed because ``Item #`` / ``Item Desc`` /
    # ``Lbs./yr Exp`` contain characters that aren't legal as Python
    # keyword-argument names.
    return month_df.groupby("RO Key", dropna=True).agg(
        Format=("Format", _first_non_null),
        Customer=("Customer", _first_non_null),
        Taxonomy=("Taxonomy", _first_non_null),
        Brand=("Brand", _first_non_null),
        **{"Item #": ("Item #", _first_non_null)},
        **{"Item Desc": ("Item Desc", _first_non_null)},
        **{"Lbs./yr": ("Lbs./yr", "sum")},
        **{"Lbs./yr Exp": ("Lbs./yr Exp", "sum")},
        **{"FY Lbs. Exp": ("FY Lbs. Exp", "sum")},
        **{"First Ship Date": ("First Ship Date", _earliest)},
    )


# ── Public pure transform: build the comparison frame ───────────────────────

def build_ro_comparison(
    history_df: pd.DataFrame,
    dimitems_df: Optional[pd.DataFrame],
    prior_month: date,
    le_month: date,
    item_master_df: Optional[pd.DataFrame] = None,
) -> tuple[pd.DataFrame, ComparisonWarnings]:
    """Build the RO Comparison summary frame for two month snapshots.

    Pure function — no Fabric I/O, fully deterministic — therefore safe
    to ``@st.cache_data`` and easy to unit-test.

    Parameters
    ----------
    history_df
        Raw ``RO_History_Tracker.csv`` content (1 row per period per
        snapshot).
    dimitems_df
        Optional ``dp_dimitems`` frame for the Portfolio Major / Minor /
        Supply Format enrichment (PRIMARY tier of the cascade).  Pass
        ``None`` when the dim read failed — the next tier picks up.
    item_master_df
        Optional ``RO_Item_Master.csv`` frame.  Acts as the SECONDARY
        tier between ``dp_dimitems`` and the RO_History ``Format``
        fallback — see :func:`_enrich_portfolio_supply` for the cascade.
        Default ``None`` for backward compatibility with older callers.
    prior_month, le_month
        Distinct snapshot months to compare.

    Returns
    -------
    summary_df
        28-column comparison frame, sorted by ``Format`` (asc) then
        ``LE First Ship Date`` (asc, NaT last).
    warnings
        :class:`ComparisonWarnings` listing every row that needs the
        planner's attention.
    """
    warnings = ComparisonWarnings()

    if history_df is None or history_df.empty:
        return _empty_output_frame(), warnings

    # ── Normalise upstream columns we depend on ──────────────────────
    h = history_df.copy()

    # Track which Item # rows failed any coercion so we can warn.
    h["_month_date"] = h["Month"].map(_coerce_to_date)
    h["_first_ship_date"] = h["First Ship Date"].map(_coerce_to_date)
    h["Brand"] = h["Brand"].map(_normalize_brand)
    h["Item #"] = h["Item #"].map(_normalize_item_id)

    # Flag rows where the Month / First Ship Date couldn't be parsed
    # (but the original cell was not blank).  These rows still flow
    # through aggregation; the warning just tells the planner to look.
    _record_unparseable(h, "Month", "_month_date", warnings.unparseable_dates)
    _record_unparseable(
        h, "First Ship Date", "_first_ship_date", warnings.unparseable_dates,
    )

    # Numeric coercion — bad cells become NaN, which the sum-aggregator
    # then ignores.  We warn about the affected Item # values so the
    # planner can fix the source CSV if needed.
    for col in _NUMERIC_SUM_COLUMNS:
        raw = h[col]
        coerced = pd.to_numeric(raw, errors="coerce")
        bad_mask = coerced.isna() & raw.map(lambda v: not _is_blank(v))
        if bad_mask.any():
            warnings.unparseable_numerics = sorted({
                *warnings.unparseable_numerics,
                *(str(x) for x in h.loc[bad_mask, "Item #"]),
            })
        h[col] = coerced

    # Drop rows without a usable RO Key — they can't participate in the
    # comparison and would noise up groupby.
    h["RO Key"] = pd.to_numeric(h["RO Key"], errors="coerce")
    bad_ro_key_mask = h["RO Key"].isna()
    if bad_ro_key_mask.any():
        warnings.extras.append(
            f"{int(bad_ro_key_mask.sum())} source rows had a missing or "
            "non-numeric RO Key and were skipped."
        )
    h = h.loc[~bad_ro_key_mask].copy()
    h["RO Key"] = h["RO Key"].astype("Int64")

    # Re-bind the canonical date column the aggregator expects.
    # ``_first_ship_date`` was added above as the parsed value.
    h["First Ship Date"] = h["_first_ship_date"]

    # ── Per-month aggregation ────────────────────────────────────────
    prior_slice = h.loc[h["_month_date"] == prior_month]
    le_slice = h.loc[h["_month_date"] == le_month]

    prior_agg = _aggregate_one_month(prior_slice)
    le_agg = _aggregate_one_month(le_slice)

    # ── Union of unique RO Keys ──────────────────────────────────────
    all_ro_keys = sorted(set(prior_agg.index) | set(le_agg.index))
    if not all_ro_keys:
        return _empty_output_frame(), warnings

    # ── Build output rows ────────────────────────────────────────────
    rows: list[dict[str, Any]] = []
    for ro_key in all_ro_keys:
        prior_row = prior_agg.loc[ro_key] if ro_key in prior_agg.index else None
        le_row = le_agg.loc[ro_key] if ro_key in le_agg.index else None
        rows.append(_assemble_summary_row(ro_key, prior_row, le_row))

    out = pd.DataFrame(rows)

    # ── Derived columns (Change *, Probability, Driver, Days) ────────
    out = _recompute_derived_columns(out)

    # ── Enrich Portfolio Major/Minor + Supply Format (cascade) ───────
    out, warnings = _enrich_portfolio_supply(
        out, dimitems_df, item_master_df, warnings,
    )

    # ── Collect remaining warnings on the assembled frame ────────────
    warnings.missing_brand = sorted({
        s for s in out.loc[out["Brand"].eq(""), "Item #"].astype(str) if s
    })

    # ── Pin the column order ─────────────────────────────────────────
    out = out[list(OUTPUT_COLUMNS)]

    # ── Sort (Format asc, LE First Ship Date asc, NaT last) ──────────
    out = _sort_summary(out)

    return out, warnings


def _empty_output_frame() -> pd.DataFrame:
    """Return an empty DataFrame with the canonical 28-column ordering."""
    return pd.DataFrame(columns=list(OUTPUT_COLUMNS))


def _record_unparseable(
    df: pd.DataFrame,
    source_col: str,
    parsed_col: str,
    sink: list[str],
) -> None:
    """Add affected ``Item #`` values to *sink* for rows whose date
    failed to parse despite the source being non-blank.

    Mutates *sink* in place (kept as a list-of-strings on the warnings
    object for cheap deduping + sorting in one final pass).
    """
    if source_col not in df.columns or parsed_col not in df.columns:
        return
    bad_mask = (
        df[parsed_col].isna()
        & df[source_col].map(lambda v: not _is_blank(v))
    )
    if bad_mask.any():
        sink.extend(
            str(x) for x in df.loc[bad_mask, "Item #"]
        )
        # Dedupe + sort in-place so the warning banner stays stable.
        sink[:] = sorted(set(sink))


def _assemble_summary_row(
    ro_key: int,
    prior_row: Optional[pd.Series],
    le_row: Optional[pd.Series],
) -> dict[str, Any]:
    """Return the dict-of-cells for one comparison row.

    Identifier fields prefer LE (latest snapshot wins for naming
    drift); numerics default to ``0`` and dates to ``None`` on the
    missing side so the derived-column math is straightforward.
    """
    source = le_row if le_row is not None else prior_row
    # NOTE: dict keys on the LEFT are the OUTPUT column names (post-rename,
    # exposed to the editor / CSV / per-Format summary).  The source field
    # names on the RIGHT (``"Lbs./yr"``, ``"Lbs./yr Exp"``, ``"FY Lbs. Exp"``)
    # are the CSV column names in ``RO_History_Tracker.csv`` and must NOT
    # be renamed — they live in an upstream feed we do not own.
    return {
        "Format":              _str_or_empty(source.get("Format")),
        "Customer":            _str_or_empty(source.get("Customer")),
        "Taxonomy":            _str_or_empty(source.get("Taxonomy")),
        "Brand":               _str_or_empty(source.get("Brand")),
        "Item #":              _str_or_empty(source.get("Item #")),
        "Description":         _str_or_empty(source.get("Item Desc")),
        "Prior RO Key":        int(ro_key) if prior_row is not None else 0,
        "LE RO Key":           int(ro_key) if le_row is not None else 0,
        ANNUAL_OPP_PRIOR:        _num_or_zero(prior_row, "Lbs./yr"),
        ANNUAL_OPP_LE:           _num_or_zero(le_row, "Lbs./yr"),
        YEAR1_PROB_PRIOR:        _num_or_zero(prior_row, "Lbs./yr Exp"),
        YEAR1_PROB_LE:           _num_or_zero(le_row, "Lbs./yr Exp"),
        CUR_FISCAL_PROB_PRIOR:   _num_or_zero(prior_row, "FY Lbs. Exp"),
        CUR_FISCAL_PROB_LE:      _num_or_zero(le_row, "FY Lbs. Exp"),
        "Prior First Ship Date": (
            prior_row["First Ship Date"] if prior_row is not None else None
        ),
        "LE First Ship Date": (
            le_row["First Ship Date"] if le_row is not None else None
        ),
    }


def _str_or_empty(value: Any) -> str:
    """Coerce *value* to a clean string or ``""`` for blanks."""
    if _is_blank(value):
        return ""
    return str(value)


def _num_or_zero(row: Optional[pd.Series], column: str) -> float:
    """Pull a numeric cell from *row* (or 0.0 when row/column is missing)."""
    if row is None or column not in row.index:
        return 0.0
    value = row[column]
    if _is_blank(value):
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _compute_driver(df: pd.DataFrame) -> pd.Series:
    """Return the ``Driver`` column per the spec.

    +-----------------+----------------+--------------------------------------+-----------+
    | Prior present?  | LE present?    | ``Change Current Fiscal Prob. Lbs``  | Driver    |
    +=================+================+======================================+===========+
    | No              | Yes            | n/a                      | New       |
    | Yes             | No             | n/a                      | Exit      |
    | Yes             | Yes            | ≠ 0                      | Change    |
    | Yes             | Yes            | == 0                     | No Change |
    +-----------------+----------------+--------------------------+-----------+
    """
    has_prior = df["Prior RO Key"].astype("Int64") != 0
    has_le = df["LE RO Key"].astype("Int64") != 0
    nonzero_change = (
        pd.to_numeric(df[CUR_FISCAL_PROB_CHANGE], errors="coerce")
          .fillna(0)
        != 0
    )

    drivers = pd.Series("No Change", index=df.index, dtype="object")
    drivers.loc[has_le & ~has_prior] = "New"
    drivers.loc[has_prior & ~has_le] = "Exit"
    drivers.loc[has_prior & has_le & nonzero_change] = "Change"
    drivers.loc[has_prior & has_le & ~nonzero_change] = "No Change"
    return drivers


def _recompute_derived_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Recompute every column that depends on the editable inputs.

    Called both at initial-build time and immediately after a planner
    edits a cell, so the Change / Probability / Driver / Days columns
    always reflect the current Lbs / Date inputs.

    Idempotent: calling this on an already-fresh frame leaves it
    unchanged (modulo a defensive ``.copy()``).
    """
    out = df.copy()

    # Numeric Lbs columns — accounting display is integer (per spec),
    # but we keep one decimal of internal precision so the recomputed
    # Change columns aren't biased by integer truncation.  The
    # display-time rounding to whole pounds lives in the page's
    # column_config (NumberColumn(format="accounting")).
    for col in (
        ANNUAL_OPP_PRIOR, ANNUAL_OPP_LE,
        YEAR1_PROB_PRIOR, YEAR1_PROB_LE,
        CUR_FISCAL_PROB_PRIOR, CUR_FISCAL_PROB_LE,
    ):
        out[col] = pd.to_numeric(out[col], errors="coerce").fillna(0.0).round(1)

    out[ANNUAL_OPP_CHANGE]     = (out[ANNUAL_OPP_LE]       - out[ANNUAL_OPP_PRIOR]).round(1)
    out[YEAR1_PROB_CHANGE]     = (out[YEAR1_PROB_LE]       - out[YEAR1_PROB_PRIOR]).round(1)
    out[CUR_FISCAL_PROB_CHANGE] = (out[CUR_FISCAL_PROB_LE] - out[CUR_FISCAL_PROB_PRIOR]).round(1)

    # Probability = IFERROR(Year1 Probabilized / Annual Opportunity, 0).
    # Rounded to 2dp to match the spec's 0.25-style display.
    out["Prior Probability"] = _safe_div(out[YEAR1_PROB_PRIOR], out[ANNUAL_OPP_PRIOR]).round(2)
    out["LE Probability"]    = _safe_div(out[YEAR1_PROB_LE],    out[ANNUAL_OPP_LE]).round(2)
    out["Change Probability"] = (out["LE Probability"] - out["Prior Probability"]).round(2)

    # Change (Days) — 0 when either ship date is missing.
    out["Change (Days)"] = _date_diff_days(
        out["Prior First Ship Date"], out["LE First Ship Date"],
    )

    # Driver depends on Change Current Fiscal Probabilized Lbs → compute last.
    out["Driver"] = _compute_driver(out)

    return out


def _enrich_portfolio_supply(
    summary_df: pd.DataFrame,
    dimitems_df: Optional[pd.DataFrame],
    item_master_df: Optional[pd.DataFrame],
    warnings: ComparisonWarnings,
) -> tuple[pd.DataFrame, ComparisonWarnings]:
    """Cascade Portfolio Major / Minor / Supply Format from dim sources.

    Lookup cascade per column (first non-blank value wins):

        Portfolio Major / Minor:  dp_dimitems → RO_Item_Master → blank
        Supply Format:            dp_dimitems → RO_Item_Master → RO_History "Format"

    ``Existing SKUs`` continues to follow ``dp_dimitems`` membership ONLY —
    this column documents the canonical item-dimension and an item that
    appears only in RO_Item_Master is by definition NOT an existing SKU
    in the planner's mental model.

    Join key for every source: ``Item #`` ↔ ``Item Code`` / ``Item #``
    (both normalised via :func:`_normalize_item_id`).

    Warnings raised:
      * ``missing_portfolio``      — Item # found in NEITHER dim source
      * ``missing_supply_format``  — Supply Format ended up blank even
        after the RO_History Format fallback
      * ``dimitems_unavailable``   — dp_dimitems frame is None / empty
        AND RO_Item_Master also failed to load (so the planner knows
        they're seeing degraded data)
    """
    out = summary_df.copy()

    # Build indexed lookups for both sources.  Empty / None inputs are
    # handled inside ``_index_portfolio_source`` and produce a no-match
    # contribution from that tier (silently skipped by the cascade).
    dim_idx = _index_portfolio_source(dimitems_df)
    item_idx = _index_portfolio_source(item_master_df)

    item_keys = out["Item #"].astype(str)

    # ── Existing SKUs (dp_dimitems membership ONLY, not RO_Item_Master) ──
    out["Existing SKUs"] = (
        item_keys.isin(dim_idx.index).map({True: "Yes", False: "No"})
        if not dim_idx.empty
        else pd.Series("No", index=out.index, dtype="object")
    )

    # ── Three cascaded fills ─────────────────────────────────────────
    out["Portfolio Major"] = _cascade_lookup(
        item_keys, dim_idx, item_idx, "Portfolio Major", fallback="",
    )
    out["Portfolio Minor"] = _cascade_lookup(
        item_keys, dim_idx, item_idx, "Portfolio Minor", fallback="",
    )
    out["Supply Format"] = _cascade_lookup(
        item_keys, dim_idx, item_idx, "Supply Format",
        fallback=out["Format"].fillna(""),
    )

    # ── Warnings ─────────────────────────────────────────────────────
    # An item that matches NEITHER dim source can't have its portfolio
    # filled at all — surface that to the planner.
    no_match_anywhere = ~(item_keys.isin(dim_idx.index) | item_keys.isin(item_idx.index))
    warnings.missing_portfolio = sorted({
        s for s in out.loc[no_match_anywhere, "Item #"].astype(str) if s
    })

    warnings.missing_supply_format = sorted({
        s for s in out.loc[
            out["Supply Format"].astype(str).str.strip().eq(""), "Item #",
        ].astype(str) if s
    })

    # Only flag dimitems_unavailable when BOTH sources are empty — a
    # working item_master fully covers for a missing dimitems.
    if dim_idx.empty and item_idx.empty:
        warnings.dimitems_unavailable = True

    return out, warnings


def _index_portfolio_source(raw: Optional[pd.DataFrame]) -> pd.DataFrame:
    """Project + dedupe + Item-Code-index a portfolio source for fast lookup.

    Returns an empty DataFrame (no columns, no rows) when *raw* is
    ``None`` / empty — the consumer ``_cascade_lookup`` treats that as
    "no contribution from this tier" without raising.
    """
    if raw is None or raw.empty:
        return pd.DataFrame()

    projected = _project_portfolio_source(raw)
    projected["Item Code"] = projected["Item Code"].map(_normalize_item_id)
    return (
        projected.dropna(subset=["Item Code"])
        .drop_duplicates(subset=["Item Code"], keep="first")
        .set_index("Item Code")
    )


def _cascade_lookup(
    keys: pd.Series,
    primary: pd.DataFrame,
    secondary: pd.DataFrame,
    column: str,
    fallback,
) -> pd.Series:
    """Return values cascading primary → secondary → *fallback*.

    A "blank" value (``None``, ``NaN``, or whitespace-only string)
    in any tier is treated as "no match" and the search falls
    through to the next tier.  This means a row with an explicit
    blank in dp_dimitems can still inherit a non-blank value from
    RO_Item_Master rather than being stuck on the blank — which
    matches the planner's mental model of "fill anything you can".

    *fallback* may be a single scalar (used for every row) OR a
    pandas Series aligned to ``keys.index`` (used for per-row
    defaults, e.g. RO_History Format).

    Returns an object-dtype Series aligned to ``keys.index`` with
    ``""`` for rows that resolved nowhere.
    """
    primary_vals = _safe_map(keys, primary, column)
    secondary_vals = _safe_map(keys, secondary, column)

    primary_ok = _nonblank(primary_vals)
    secondary_ok = _nonblank(secondary_vals)

    fallback_series = (
        fallback if isinstance(fallback, pd.Series)
        else pd.Series([fallback] * len(keys), index=keys.index, dtype="object")
    )

    # Vectorised pick: primary if ok, else secondary if ok, else fallback.
    out = fallback_series.astype(object).copy()
    out = out.where(~secondary_ok, secondary_vals)
    out = out.where(~primary_ok, primary_vals)
    return out.fillna("")


def _safe_map(keys: pd.Series, idx: pd.DataFrame, column: str) -> pd.Series:
    """Map *keys* through *idx[column]* — NaN-filled when idx lacks the column."""
    if idx is None or idx.empty or column not in idx.columns:
        return pd.Series([pd.NA] * len(keys), index=keys.index, dtype="object")
    return keys.map(idx[column])


def _nonblank(s: pd.Series) -> pd.Series:
    """Return a boolean Series — True where *s* is a non-blank string/value."""
    return s.notna() & (s.astype(str).str.strip() != "")


def _project_portfolio_source(raw: pd.DataFrame) -> pd.DataFrame:
    """Return a 4-column projection of *raw* with canonical lookup names.

    Used for BOTH dp_dimitems (Delta table) and RO_Item_Master.csv —
    the canonical schema is the same once you alias their differing
    Item-key columns ("Item Code" vs "Item #").

    The upstream Fabric schemas are owned by another team; we tolerate
    the most common name variants here so a harmless rename upstream
    doesn't silently break Portfolio / Supply Format enrichment.
    Missing columns degrade to empty strings rather than raising.
    """
    aliases: dict[str, tuple[str, ...]] = {
        # ``Item #`` is the join key used by RO_Item_Master.csv; ``Item Code``
        # is the equivalent in dp_dimitems.  Treat them as aliases.
        "Item Code":       ("Item Code", "Item #", "ItemCode", "Item_Code", "item_code", "ITEM CODE"),
        "Portfolio Major": ("Portfolio Major", "PortfolioMajor", "Portfolio_Major", "portfolio_major"),
        "Portfolio Minor": ("Portfolio Minor", "PortfolioMinor", "Portfolio_Minor", "portfolio_minor"),
        "Supply Format":   ("Supply Format", "SupplyFormat", "Supply_Format", "supply_format"),
    }
    projected = pd.DataFrame(index=raw.index)
    for canonical, candidates in aliases.items():
        match = next((c for c in candidates if c in raw.columns), None)
        projected[canonical] = raw[match] if match else ""
    return projected


def _sort_summary(df: pd.DataFrame) -> pd.DataFrame:
    """Return *df* sorted by Format (asc), LE First Ship Date (asc, NaT last).

    Sorting on an object column that mixes :class:`date` and ``None``
    raises in Python 3 (TypeError: '<' not supported), so we sort on a
    temporary datetime64 column and drop it once the row order is
    fixed.  ``kind="mergesort"`` makes the sort stable so rows that
    share the same key keep their relative order from earlier passes.
    """
    helper = pd.to_datetime(df["LE First Ship Date"], errors="coerce")
    out = df.assign(_le_sort=helper)
    out = out.sort_values(
        by=["Format", "_le_sort"],
        ascending=[True, True],
        na_position="last",
        kind="mergesort",
    ).drop(columns=["_le_sort"]).reset_index(drop=True)
    return out


# ── Per-Format change summary ────────────────────────────────────────────────

# Column names emitted by ``compute_per_format_summary``.  Exposed as
# module-level constants so the page can pin display order / column
# widths without re-typing the literal strings.
PER_FORMAT_FORMAT_COL: str = "Format"
PER_FORMAT_DELTA_COL: str = "Δ " + CUR_FISCAL_PROB_CHANGE.removeprefix("Change ")
# Annualized (Year-1) equivalent used by the "Annualized Probabilized
# Driver" diagnostic — same layout as the FY27 per-format summary but
# the delta is ``LE Year1 − Prior Year1`` per row (steady-state run-rate
# impact, insensitive to First Ship Date phasing).
PER_FORMAT_ANNUAL_DELTA_COL: str = "Δ " + YEAR1_PROB_CHANGE.removeprefix("Change ")
PER_FORMAT_DRIVER_COLS: tuple[str, str, str] = (
    "#1 Driver", "#2 Driver", "#3 Driver",
)
PER_FORMAT_TOTAL_LABEL: str = "TOTAL"


# Sentinel display value for blank (Customer, Portfolio Minor) tuples
# so a row whose Customer or Portfolio Minor is missing still appears
# in the driver ranking instead of vanishing under a None group key.
# The same string is the join key the page uses to look the driver up
# in the drill-down helper.
PER_FORMAT_DRIVER_BLANK_LABEL: str = "(blank)"


def compute_per_format_summary(view_df: pd.DataFrame) -> pd.DataFrame:
    """Return a per-Format roll-up of ``Change Current Fiscal Probabilized Lbs``.

    Output layout — one row per Format plus a TOTAL footer row:

        Format │ Δ Current Fiscal Probabilized Lbs │ #1 Driver │ #2 Driver │ #3 Driver
        ───────┼───────────────────────────────────┼───────────┼───────────┼───────────
        WMC    │   +245,300                        │ Costco — Yogurt (+180,000) │ … │ …
        DCO    │   −180,000                        │ Sysco — Cottage Cheese (−120,000) │ … │ …
        TOTAL  │    +65,300                        │                            │   │

    Rules
    -----
    * Operates on the *already-filtered* view so every filter
      (Format/Customer/Brand/…) and the current edit state are
      reflected in the totals and driver picks.
    * Rows with a blank Format are bucketed under ``"(Unspecified)"``
      so the planner can still see them rather than having them
      silently disappear from the roll-up.
    * Format rows are sorted by ``|Δ|`` descending so the biggest
      movers are at the top.  Ties are broken by Format name (asc).
    * **Drivers are aggregated by (Customer, Portfolio Minor)** —
      every Item belonging to the same Customer + Portfolio Minor
      within a Format collapses into a single driver bucket whose Δ
      is the sum of those Items' Δ.  Top 3 buckets by ``|aggregated
      Δ|`` are reported per Format.  This matches the planner's
      mental model when chasing root-cause attribution (one customer
      flipping its yogurt commit shows up as ONE driver, not as five
      separate Item rows).  Items are reachable via the drill-down
      helper :func:`compute_driver_items` (one call per driver cell).
    * Driver cell format: ``"{Customer} — {Portfolio Minor}  ({signed
      Δ in accounting})"``.  Empty string for Formats with fewer than
      3 buckets.  Signed Δ uses commas + a leading ``+``/``−`` so the
      page can colour-style it without re-parsing.
    * Customer / Portfolio Minor blanks are bucketed under the literal
      :data:`PER_FORMAT_DRIVER_BLANK_LABEL` (``"(blank)"``) so they
      still appear in the ranking — and the drill-down helper takes
      that same sentinel back as a lookup key.
    * TOTAL row sums Δ across all visible Formats; its driver cells
      are blank (the per-Format drivers don't compose into a single
      cross-Format pick).
    * Returns an empty DataFrame (canonical column order) when
      *view_df* is empty so the page can render "no rows" gracefully.

    Pure function — no I/O, no Streamlit dependencies — safe to
    unit-test and to call inside an ``st.fragment``.
    """
    return _compute_per_format_summary_impl(
        view_df,
        delta_source_col=CUR_FISCAL_PROB_CHANGE,
        delta_display_col=PER_FORMAT_DELTA_COL,
    )


def compute_per_format_summary_annualized(view_df: pd.DataFrame) -> pd.DataFrame:
    """Per-Format roll-up of the ANNUALIZED (Year-1) probabilized delta.

    Same layout, sorting, driver-bucketing and TOTAL semantics as
    :func:`compute_per_format_summary`, but the per-row delta is the
    annualized swing ``LE Year1 − Prior Year1`` (i.e. steady-state
    run-rate impact) instead of the FY-pro-rated
    ``Change Current Fiscal Probabilized Lbs``.

    Rationale — the FY27 per-format table answers *"what lands this
    fiscal year?"*; this one answers *"what's the run-rate hit next
    year and beyond?"*.  Committed risks, phasing-only shifts, and
    genuine volume moves separate cleanly here because Days-in-Year
    proration is out of the picture.

    Pure function — safe to unit-test and to call inside an
    ``st.fragment``.
    """
    return _compute_per_format_summary_impl(
        view_df,
        delta_source_col=YEAR1_PROB_CHANGE,
        delta_display_col=PER_FORMAT_ANNUAL_DELTA_COL,
    )


def _compute_per_format_summary_impl(
    view_df: pd.DataFrame,
    *,
    delta_source_col: str,
    delta_display_col: str,
) -> pd.DataFrame:
    """Shared implementation for the per-Format driver diagnostics.

    Both the FY27 (current-fiscal) and FY28 (annualized) variants
    differ only in *which column supplies the per-row delta* and
    *what the output delta column is called*; every other rule
    (grouping by Format, top-3 (Customer, PMinor) buckets, ``|Δ|``
    sort, TOTAL footer, blank-sentinel normalisation) is identical.
    Extracting the shared core here keeps the two public entry points
    thin and guarantees they never drift apart.
    """
    columns = [
        PER_FORMAT_FORMAT_COL,
        delta_display_col,
        *PER_FORMAT_DRIVER_COLS,
    ]

    if view_df is None or view_df.empty:
        return pd.DataFrame(columns=columns)

    # Defensive copy + numeric coercion so blank cells / strings don't
    # break the groupby sum or the abs() sort below.
    work = view_df.copy()
    work[PER_FORMAT_FORMAT_COL] = (
        work[PER_FORMAT_FORMAT_COL].astype(str).str.strip()
        .replace({"": "(Unspecified)", "nan": "(Unspecified)"})
    )
    # Tolerate a missing source column (e.g. a legacy frame lacking
    # the Year-1 change) — treat as zero so the diagnostic still
    # renders instead of surfacing a KeyError.
    if delta_source_col in work.columns:
        work["_delta"] = pd.to_numeric(
            work[delta_source_col], errors="coerce",
        ).fillna(0.0)
    else:
        work["_delta"] = 0.0
    # Canonical (Customer, Portfolio Minor) bucket keys.  Coerce both
    # to stripped strings and replace blank/NaN with the same sentinel
    # the drill-down API understands, so empty values aren't silently
    # dropped by ``groupby`` (which drops NaN keys by default unless
    # ``dropna=False`` is passed — we go a step further and explicitly
    # label them so the user can SEE the blank bucket in the table).
    work["_customer"] = _normalise_driver_key(work.get("Customer"))
    work["_pminor"]   = _normalise_driver_key(work.get("Portfolio Minor"))

    # ── 1. Per-Format net Δ + top-3 (Customer, PMinor) drivers ──────
    rows: list[dict[str, Any]] = []
    for fmt, group in work.groupby(PER_FORMAT_FORMAT_COL, sort=False):
        net_delta = float(group["_delta"].sum())

        # Aggregate Δ by (Customer, Portfolio Minor) bucket.  Sum first
        # — that's what the planner means by "driver"; sorting by |sum|
        # then surfaces the buckets that moved the Format's needle
        # furthest in either direction.
        bucketed = (
            group.groupby(["_customer", "_pminor"], dropna=False, sort=False)
                 ["_delta"].sum().reset_index()
        )
        ranked = bucketed.assign(_abs=bucketed["_delta"].abs()).sort_values(
            by=["_abs", "_customer", "_pminor"],
            ascending=[False, True, True],
            kind="mergesort",
        )

        drivers: list[str] = []
        for _, r in ranked.head(3).iterrows():
            drivers.append(_format_driver_cell(
                customer=str(r["_customer"]),
                pminor=str(r["_pminor"]),
                delta=float(r["_delta"]),
            ))
        while len(drivers) < 3:
            drivers.append("")

        rows.append({
            PER_FORMAT_FORMAT_COL: fmt,
            delta_display_col: round(net_delta, 1),
            PER_FORMAT_DRIVER_COLS[0]: drivers[0],
            PER_FORMAT_DRIVER_COLS[1]: drivers[1],
            PER_FORMAT_DRIVER_COLS[2]: drivers[2],
        })

    out = pd.DataFrame(rows, columns=columns)

    # ── 2. Sort Formats by |Δ| desc, Format asc ──────────────────────
    out = out.assign(_abs=out[delta_display_col].abs()).sort_values(
        by=["_abs", PER_FORMAT_FORMAT_COL],
        ascending=[False, True],
        kind="mergesort",
    ).drop(columns="_abs").reset_index(drop=True)

    # ── 3. TOTAL footer row ──────────────────────────────────────────
    total_row = {
        PER_FORMAT_FORMAT_COL: PER_FORMAT_TOTAL_LABEL,
        delta_display_col: round(float(out[delta_display_col].sum()), 1),
        PER_FORMAT_DRIVER_COLS[0]: "",
        PER_FORMAT_DRIVER_COLS[1]: "",
        PER_FORMAT_DRIVER_COLS[2]: "",
    }
    out = pd.concat([out, pd.DataFrame([total_row])], ignore_index=True)
    return out


def _normalise_driver_key(series: Any) -> Any:
    """Return a ``Series`` whose blank/NaN entries become the blank sentinel.

    Centralised so the per-Format summary, the drill-down lookup, and
    every consumer of "driver buckets" use the SAME normalisation —
    a blank Customer in the table and a "(blank)" pick from the drill-
    down selector will always reference the same set of rows.

    When *series* is ``None`` (the source frame lacks the column
    entirely — e.g. a legacy frame without ``Portfolio Minor``) the
    helper returns the blank sentinel as a SCALAR.  The caller assigns
    the result with ``df["_col"] = _normalise_driver_key(...)`` so
    pandas broadcasts the scalar to fill every row uniformly — no
    length-mismatch errors, no per-call boilerplate.
    """
    if series is None:
        return PER_FORMAT_DRIVER_BLANK_LABEL
    s = pd.Series(series).astype("string").fillna("").str.strip()
    return s.where(s.ne(""), PER_FORMAT_DRIVER_BLANK_LABEL)


def compute_driver_items(
    view_df: pd.DataFrame,
    format_name: str,
    customer: str,
    pminor: str,
) -> pd.DataFrame:
    """Return the items that compose a single driver bucket — for drill-down.

    Parameters
    ----------
    view_df
        The same in-memory comparison frame the per-Format summary
        was computed from (so item-level rows reflect every active
        filter + edit).
    format_name
        Format the driver bucket belongs to.
    customer, pminor
        The bucket's Customer + Portfolio Minor values, as displayed
        in the driver cell.  Pass :data:`PER_FORMAT_DRIVER_BLANK_LABEL`
        (or an empty string — both are normalised to the same key) to
        target the blank-bucket.

    Returns
    -------
    pd.DataFrame
        Item-level columns useful for a planner's drill-down:
        ``Item #``, ``Description``, ``Brand``, ``Driver``,
        ``Prior Current Fiscal Probabilized Lbs``,
        ``LE Current Fiscal Probabilized Lbs``,
        ``Change Current Fiscal Probabilized Lbs``.
        Sorted by ``|Δ|`` desc so the biggest movers are at the top.
        Returns an empty DataFrame (with the canonical column order)
        when no rows match — the page renders "no items" gracefully.

    Pure function — no I/O, no Streamlit dependencies.
    """
    output_cols = [
        "Item #", "Description", "Brand", "Driver",
        CUR_FISCAL_PROB_PRIOR, CUR_FISCAL_PROB_LE, CUR_FISCAL_PROB_CHANGE,
    ]
    if view_df is None or view_df.empty:
        return pd.DataFrame(columns=output_cols)

    # Mirror the bucket-key normalisation used by
    # ``compute_per_format_summary`` so a "(blank)" selection from the
    # UI matches every blank row in the source frame.
    fmt_key = (
        (format_name or "").strip()
        or "(Unspecified)"
    )
    customer_key = (customer or "").strip() or PER_FORMAT_DRIVER_BLANK_LABEL
    pminor_key   = (pminor   or "").strip() or PER_FORMAT_DRIVER_BLANK_LABEL

    work = view_df.copy()
    work[PER_FORMAT_FORMAT_COL] = (
        work[PER_FORMAT_FORMAT_COL].astype(str).str.strip()
        .replace({"": "(Unspecified)", "nan": "(Unspecified)"})
    )
    work["_customer"] = _normalise_driver_key(work.get("Customer"))
    work["_pminor"]   = _normalise_driver_key(work.get("Portfolio Minor"))
    work["_delta"]    = pd.to_numeric(
        work[CUR_FISCAL_PROB_CHANGE], errors="coerce",
    ).fillna(0.0)

    mask = (
        work[PER_FORMAT_FORMAT_COL].eq(fmt_key)
        & work["_customer"].eq(customer_key)
        & work["_pminor"].eq(pminor_key)
    )
    filtered = work.loc[mask]
    if filtered.empty:
        return pd.DataFrame(columns=output_cols)

    # Sort by |Δ| desc, then Item # asc for a deterministic order.
    filtered = filtered.assign(_abs=filtered["_delta"].abs()).sort_values(
        by=["_abs", "Item #"],
        ascending=[False, True],
        kind="mergesort",
    )
    # Hand back ONLY the display columns the drill-down UI uses — keeps
    # the rendered table tight and avoids surfacing every internal
    # comparison field to the planner.
    return filtered.loc[:, output_cols].reset_index(drop=True)


def _format_driver_cell(
    customer: str, pminor: str, delta: float,
) -> str:
    """Render a driver cell as ``"{Customer} — {Portfolio Minor}  ({signed Δ})"``.

    The cell summarises one (Customer, Portfolio Minor) bucket inside a
    Format.  Both labels are surfaced verbatim — including the blank
    sentinel — so the planner can spot driver buckets whose Customer
    or Portfolio Minor is missing.  When every label is blank the
    cell falls back to ``"?"`` so the row is still discoverable.

    Signed delta uses commas + an explicit ``+`` / ``−`` (Unicode
    minus to match accounting / spreadsheet aesthetics) so the page
    can colour-style the cell without re-parsing.  ``0`` deltas come
    out as ``"0"`` (no sign).
    """
    rounded = int(round(delta))
    if rounded > 0:
        signed = f"+{rounded:,}"
    elif rounded < 0:
        signed = f"\u2212{abs(rounded):,}"
    else:
        signed = "0"

    parts = [p.strip() for p in (customer, pminor) if p and p.strip()]
    base = " — ".join(parts) if parts else "?"
    return f"{base}  ({signed})"


# ── Streamlit-cached Fabric I/O ──────────────────────────────────────────────

def list_months(history_df: pd.DataFrame) -> list[date]:
    """Return every distinct snapshot ``Month`` found in *history_df*, ascending."""
    if history_df is None or history_df.empty or "Month" not in history_df.columns:
        return []
    parsed = history_df["Month"].map(_coerce_to_date)
    unique = {d for d in parsed.tolist() if isinstance(d, date)}
    return sorted(unique)


# Underlying cached readers — wrapped by the public ``fetch_*_df``
# functions below.  Splitting the wrapper from the cached impl gives us
# a single place to wire a ``force_refresh`` escape hatch without
# polluting every call site.

@st.cache_data(ttl=_HISTORY_CACHE_TTL_SECONDS, show_spinner=False)
def _cached_history_df(_signature: str) -> pd.DataFrame:
    """Cached read of ``RO_History_Tracker.csv``.

    The leading-underscore ``_signature`` argument participates in the
    cache key (so a different value triggers a re-read) but is not
    hashed for its contents — that's the documented Streamlit pattern
    for a manual cache-busting flag.
    """
    try:
        df, _etag = read_csv(_SECRETS_SECTION, _RO_HISTORY_BLOB_PATH)
    except LakehouseIOError as exc:
        raise RoComparisonError(
            "Could not read RO_History_Tracker.csv from Microsoft Fabric: "
            f"{exc}"
        ) from exc

    if df is None:
        raise RoComparisonError(
            f"OneLake blob 'Files/{_RO_HISTORY_BLOB_PATH}' does not exist."
        )

    logger.info(
        "Loaded RO_History_Tracker.csv: %s rows, %s columns.",
        len(df), len(df.columns),
    )
    return df


def _read_fabric_htst_cfg() -> dict[str, str]:
    """Return the ``[fabric_htst]`` secrets section (or an empty dict).

    We piggy-back on the same secrets block ``data_sources/htst_shipment.py``
    uses because the only keys we read here are the optional
    ``ca_cert_file`` / ``ssl_verify`` overrides — no required keys.
    A missing or malformed section degrades silently to the
    ``certifi.where()`` default in :func:`resolve_ca_cert_file`.
    """
    try:
        return dict(read_section("fabric_htst"))
    except FabricAuthError:
        return {}


@st.cache_data(ttl=_DIMITEMS_CACHE_TTL_SECONDS, show_spinner=False)
def _cached_dimitems_df(_signature: str) -> pd.DataFrame:
    """Cached read of the ``dbo.dp_dimitems`` Delta table via DuckDB.

    Mirrors the TLS-hardened read path in
    ``data_sources/htst_shipment.py`` — both readers share
    :mod:`data_sources.fabric_tls` so the libcurl CA-bundle plumbing
    is done identically on every OneLake Delta scan.  Without that
    plumbing the bundled libcurl (statically linked into DuckDB's
    azure extension) falls back to the RHEL CA path on Linux and to
    no CA bundle at all on Windows, producing
    ``Fail to get a new connection for: https://onelake.blob...
    SSL connect error`` on a workstation whose Python install relies
    on ``certifi``.

    Failures escalate as :class:`RoComparisonError`; the page handles
    them as a soft degradation (rows still render, Portfolio columns
    are blank, big warning at the top).
    """
    try:
        token = acquire_storage_token()
    except FabricAuthError as exc:
        raise RoComparisonError(
            f"Could not acquire OneLake token for dp_dimitems: {exc}"
        ) from exc

    cfg = _read_fabric_htst_cfg()
    ca_cert_file = resolve_ca_cert_file(cfg)
    ssl_verify = ssl_verify_enabled(cfg)

    apply_ca_cert_env(ca_cert_file)
    if not ssl_verify:
        logger.warning(
            "TLS certificate verification is DISABLED for the dp_dimitems "
            "Delta scan ([fabric_htst].ssl_verify = false).  This is a "
            "corporate-MITM workaround — restore verification by removing "
            "the flag (or setting it to true) and providing a proper "
            "[fabric_htst].ca_cert_file path."
        )

    table_uri = (
        f"abfss://{_WORKSPACE_GUID}@onelake.dfs.fabric.microsoft.com/"
        f"{_LAKEHOUSE_GUID}/Tables/{_DIMITEMS_SCHEMA}/{_DIMITEMS_TABLE}"
    )
    try:
        con = get_duckdb_connection()
        with duckdb_lock():
            bind_storage_token(con, token, ssl_verify=ssl_verify)
            df = con.execute(f"SELECT * FROM delta_scan('{table_uri}')").df()
    except Exception as exc:  # noqa: BLE001 — wrapped for the caller
        msg = str(exc)
        hint = ""
        if "SSL" in msg or "ssl" in msg or "certificate" in msg.lower():
            hint = (
                "\n\nThis looks like a TLS / CA-certificate failure inside "
                "DuckDB's bundled libcurl, NOT a problem with your bearer "
                "token, workspace identifiers, or lakehouse permissions.  "
                "Two ways to fix it:\n"
                "  (a) [PREFERRED] Point the connector at a CA bundle that "
                "your environment trusts.  Add to .streamlit/secrets.toml "
                "under [fabric_htst]:\n"
                "        ca_cert_file = \"C:/path/to/your/corporate_ca.pem\"\n"
                "      If your machine has no MITM proxy, the certifi bundle "
                "is auto-detected — the most likely root cause is then a "
                "corporate firewall rewriting *.fabric.microsoft.com "
                "certificates.\n"
                "  (b) [LAST RESORT] Disable TLS verification by adding to "
                "[fabric_htst]:\n"
                "        ssl_verify = false\n"
                "      Insecure — only use on a trusted network and revert "
                "as soon as you have a proper CA bundle."
            )
        raise RoComparisonError(
            f"Could not read dp_dimitems via DuckDB at {table_uri}: {exc}{hint}"
        ) from exc

    logger.info(
        "Loaded dp_dimitems: %s rows, %s columns.",
        len(df), len(df.columns),
    )
    return df


def _compute_history_blob_signature() -> str:
    """Return a cheap, monotone-with-content signature for RO_History_Tracker.

    Issues ONE Fabric ``get_file_properties`` round-trip (a HEAD-style
    metadata read — no body bytes, sub-100ms on a warm token) and
    serialises ``(etag, last_modified, size)`` into a single string.

    Why
    ----
    The ``@st.cache_data`` decorator on :func:`_cached_history_df`
    keys cache slots by the function's call arguments.  By passing
    this signature as the argument, ANY Fabric-side change to the
    blob (a re-upload changes etag + last_modified, an in-place
    rewrite changes etag + size, etc.) yields a different signature,
    misses the cache, and triggers a fresh body read on the very
    next render.  Without this check, a fresh upload to Fabric
    would not be visible to the page until the 15-min TTL expired.

    Graceful degradation
    --------------------
    If the properties fetch fails (auth blip, network), we fall back
    to the literal sentinel ``"default"`` — the same key we used
    before this freshness check existed.  That means in the worst
    case we degrade to the previous TTL-only behaviour, never to a
    hard error.  Logged at INFO so the operator can see it without
    a noisy WARN ladder.
    """
    try:
        props = get_file_properties(_SECRETS_SECTION, _RO_HISTORY_BLOB_PATH)
    except LakehouseIOError as exc:
        logger.info(
            "RO_History freshness check failed (non-fatal — falling back "
            "to TTL-only caching for this render): %s", exc,
        )
        return "default"
    if props is None:
        # Blob absent — keep a stable key so we don't churn the cache.
        return "default"
    return f"etag={props.etag}|lm={props.last_modified}|sz={props.size}"


def fetch_ro_history_df(*, force_refresh: bool = False) -> pd.DataFrame:
    """Return the latest ``RO_History_Tracker.csv`` as a DataFrame.

    Parameters
    ----------
    force_refresh : bool, default False
        When True, clears this connector's Streamlit cache before
        reading.  Wire this to a "Refresh from Fabric" button.

    Freshness model
    ---------------
    Each call issues a cheap Fabric ``get_file_properties`` round-trip
    (a HEAD-style metadata read — see
    :func:`_compute_history_blob_signature`) and uses the resulting
    ``(etag, last_modified, size)`` triple as the cache key.  Any
    Fabric-side update to the blob produces a different key, misses
    the cache, and triggers a fresh body read on the very next
    render — without waiting for the 15-min TTL to expire.

    This means downstream cascades (auto-regenerated
    ``RO_Comparison_Output.csv``, Early-Start-Date table, RO Summary
    Report) automatically pick up source-CSV updates on the next
    page load.  See ``_maybe_auto_regenerate_comparison_output``
    in :mod:`pages.demand_planner_analytics_view` for the full
    end-to-end flow.
    """
    if force_refresh:
        _cached_history_df.clear()
    signature = _compute_history_blob_signature()
    return _cached_history_df(signature)


def fetch_dimitems_df(*, force_refresh: bool = False) -> pd.DataFrame:
    """Return the latest ``dbo.dp_dimitems`` as a DataFrame.

    See :func:`fetch_ro_history_df` for the ``force_refresh`` contract.
    """
    if force_refresh:
        _cached_dimitems_df.clear()
    return _cached_dimitems_df("default")


@st.cache_data(ttl=_ITEM_MASTER_CACHE_TTL_SECONDS, show_spinner=False)
def _cached_item_master_df(_signature: str) -> pd.DataFrame:
    """Cached read of ``RO_Item_Master.csv`` — middle-tier portfolio source.

    Schema (per the planner's spec):
        ``Item #, Item Desc, Brand Category, Portfolio Major,
          Portfolio Minor, Supply Format``

    Acts as the SECONDARY tier in :func:`_enrich_portfolio_supply` —
    consulted only when ``dp_dimitems`` has no row for the item.  A
    missing or empty blob is NOT fatal: the function raises
    :class:`RoComparisonError` only on a true I/O failure; a "blob
    doesn't exist" path returns an empty DataFrame so the cascade
    silently degrades to the final tier (RO_History Format).
    """
    try:
        df, _etag = read_csv(_SECRETS_SECTION, _RO_ITEM_MASTER_BLOB_PATH)
    except LakehouseIOError as exc:
        raise RoComparisonError(
            "Could not read RO_Item_Master.csv from Microsoft Fabric: "
            f"{exc}"
        ) from exc

    if df is None or df.empty:
        # Blob missing OR empty: not an error — the planner just hasn't
        # published one yet.  Cascade falls through to the RO_History
        # Format fallback for Supply Format and leaves Portfolio Major /
        # Minor blank for items not in dp_dimitems.
        logger.info(
            "RO_Item_Master.csv is missing or empty at Files/%s — cascade "
            "will use only dp_dimitems + RO_History fallback.",
            _RO_ITEM_MASTER_BLOB_PATH,
        )
        return pd.DataFrame()

    logger.info(
        "Loaded RO_Item_Master.csv: %s rows, %s columns.",
        len(df), len(df.columns),
    )
    return df


def fetch_ro_item_master_df(*, force_refresh: bool = False) -> pd.DataFrame:
    """Return the latest ``RO_Item_Master.csv`` as a DataFrame.

    Returns an empty DataFrame when the blob is missing — callers
    should treat that as "this tier contributes nothing" rather than
    raising.

    See :func:`fetch_ro_history_df` for the ``force_refresh`` contract.
    """
    if force_refresh:
        _cached_item_master_df.clear()
    return _cached_item_master_df("default")


def ro_item_master_blob_path() -> str:
    """Return the OneLake path for ``RO_Item_Master.csv`` (no ``Files/`` prefix)."""
    return _RO_ITEM_MASTER_BLOB_PATH


@st.cache_data(ttl=_ITEM_MASTER_CACHE_TTL_SECONDS, show_spinner=False)
def _cached_item_master_raw_bytes(_signature: str) -> bytes:
    """Cached raw read of ``RO_Item_Master.csv`` for download buttons."""
    try:
        raw, _etag = read_bytes(_SECRETS_SECTION, _RO_ITEM_MASTER_BLOB_PATH)
    except LakehouseIOError as exc:
        raise RoComparisonError(
            "Could not download RO_Item_Master.csv from Microsoft Fabric: "
            f"{exc}"
        ) from exc
    if raw is None:
        raise RoComparisonError(
            f"OneLake blob 'Files/{_RO_ITEM_MASTER_BLOB_PATH}' does not exist."
        )
    return raw


def fetch_ro_item_master_raw_bytes(*, force_refresh: bool = False) -> bytes:
    """Return raw bytes of ``RO_Item_Master.csv`` for Streamlit downloads.

    Preserves byte-for-byte fidelity with the Fabric source (no
    re-serialisation through pandas).  Shares the Item Master cache TTL.
    """
    if force_refresh:
        _cached_item_master_raw_bytes.clear()
    return _cached_item_master_raw_bytes("default")


def upload_customer_input(filename: str, payload: bytes) -> str:
    """Save *payload* under ``Files/RO Tracking/Append_New_History/<filename>``.

    Returns the resulting blob path so the UI can echo the destination
    in the success toast.

    Strips path-like characters from *filename* to prevent a malicious
    or malformed name from escaping the target folder.  Raises
    :class:`RoComparisonError` on any underlying write failure.
    """
    safe_name = filename.replace("\\", "/").rsplit("/", 1)[-1].strip()
    if not safe_name:
        raise RoComparisonError("Uploaded file has no usable filename.")
    blob_path = f"{_APPEND_NEW_HISTORY_FOLDER}/{safe_name}"
    try:
        write_bytes(_SECRETS_SECTION, blob_path, payload, etag=None)
    except LakehouseIOError as exc:
        raise RoComparisonError(
            f"Could not save '{safe_name}' to 'Files/{blob_path}': {exc}"
        ) from exc
    return blob_path


def save_ro_comparison_output(df: pd.DataFrame) -> str:
    """Overwrite ``Files/RO Tracking/RO_Reporting/RO_Comparison_Output.csv``.

    Ship-date columns are formatted as ``YYYY-MM-DD`` strings before
    serialisation so the CSV on disk is human-readable instead of
    containing the pandas default ``YYYY-MM-DD HH:MM:SS`` representation.

    Returns the destination blob path.  Raises :class:`RoComparisonError`
    on any underlying write failure.
    """
    df_out = df.copy()

    # Format date columns as YYYY-MM-DD (empty for missing).  Works for
    # both ``datetime64[ns]`` and object dtype containing :class:`date`.
    for col in ("Prior First Ship Date", "LE First Ship Date"):
        if col not in df_out.columns:
            continue
        as_ts = pd.to_datetime(df_out[col], errors="coerce")
        df_out[col] = as_ts.dt.strftime("%Y-%m-%d").where(as_ts.notna(), "")

    try:
        write_csv(
            _SECRETS_SECTION, _RO_REPORTING_BLOB_PATH, df_out, etag=None,
        )
    except LakehouseIOError as exc:
        raise RoComparisonError(
            "Could not save RO_Comparison_Output.csv to "
            f"'Files/{_RO_REPORTING_BLOB_PATH}': {exc}"
        ) from exc
    return _RO_REPORTING_BLOB_PATH


def fetch_ro_comparison_output_df() -> pd.DataFrame:
    """Read the PUBLISHED ``Files/RO Tracking/RO_Reporting/RO_Comparison_Output.csv``
    back into a DataFrame — a fresh Fabric read (no cache), so it reflects the
    file as it currently stands on disk.

    Ship-date columns (serialised as ``YYYY-MM-DD`` strings by
    :func:`save_ro_comparison_output`) are coerced back to ``datetime64``.  Raises
    :class:`RoComparisonError` if the file is missing or unreadable.
    """
    try:
        df, _etag = read_csv(_SECRETS_SECTION, _RO_REPORTING_BLOB_PATH)
    except LakehouseIOError as exc:
        raise RoComparisonError(
            "Could not read RO_Comparison_Output.csv from "
            f"'Files/{_RO_REPORTING_BLOB_PATH}': {exc}"
        ) from exc
    if df is None:
        raise RoComparisonError(
            "RO_Comparison_Output.csv not found at "
            f"'Files/{_RO_REPORTING_BLOB_PATH}' — save it first (or run Generate)."
        )
    for col in ("Prior First Ship Date", "LE First Ship Date"):
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce")
    return df


def save_pipeline_review_snapshot(
    df: pd.DataFrame, *, timestamp: Optional[str] = None,
) -> str:
    """Archive a "RO Pipeline Review" table as a timestamped CSV in Fabric.

    Writes ``df`` to ``Files/RO Tracking/RO Pipeline Review Archive/
    RO_Pipeline_Review_<YYYYmmdd_HHMMSS>.csv`` (append-only — every Refresh
    click keeps its own copy).  Date-like columns are serialised as
    ``YYYY-MM-DD`` for readability.  Returns the destination blob path; raises
    :class:`RoComparisonError` on any write failure.  ``timestamp`` overrides
    the auto ``YYYYmmdd_HHMMSS`` stamp (used by tests for determinism).
    """
    df_out = df.copy()
    for col in df_out.columns:
        if pd.api.types.is_datetime64_any_dtype(df_out[col]):
            as_ts = pd.to_datetime(df_out[col], errors="coerce")
            df_out[col] = as_ts.dt.strftime("%Y-%m-%d").where(as_ts.notna(), "")
    payload = df_out.to_csv(index=False).encode("utf-8")
    try:
        return archive_bytes(
            _SECRETS_SECTION, _RO_PIPELINE_REVIEW_ARCHIVE_DIR,
            "RO_Pipeline_Review.csv", payload, timestamp=timestamp,
        )
    except LakehouseIOError as exc:
        raise RoComparisonError(
            "Could not archive the RO Pipeline Review snapshot to "
            f"'Files/{_RO_PIPELINE_REVIEW_ARCHIVE_DIR}': {exc}"
        ) from exc


def list_pipeline_review_snapshots() -> list[LakehouseFile]:
    """List archived "RO Pipeline Review" snapshots, newest first.

    Reads the archive folder in one round-trip and returns the ``.csv`` files
    sorted by name descending (the timestamped filenames sort chronologically),
    so the page can surface the audit trail the Refresh button writes.  Returns
    ``[]`` when the folder doesn't exist yet.  Raises :class:`RoComparisonError`
    on an underlying I/O failure.
    """
    try:
        files = list_files(
            _SECRETS_SECTION, _RO_PIPELINE_REVIEW_ARCHIVE_DIR, suffix=".csv")
    except LakehouseIOError as exc:
        raise RoComparisonError(
            "Could not list the RO Pipeline Review archive at "
            f"'Files/{_RO_PIPELINE_REVIEW_ARCHIVE_DIR}': {exc}"
        ) from exc
    return sorted(files, key=lambda f: f.name, reverse=True)


# ── Auto-regenerate on RO_History refresh ────────────────────────────────────
#
# Behaviour (per planner spec, 2026-05-27):
#
#   When ``RO_History_Tracker.csv`` is refreshed in Microsoft Fabric,
#   the saved ``RO_Comparison_Output.csv`` should be automatically
#   regenerated and overwritten — no manual click required, planner
#   edits are intentionally lost (they re-edit on top of the new
#   baseline next time).  The (Prior, LE) pair is whatever the
#   page's month picker is currently set to.
#
# Detection mechanism:
#
#   * On every page load, we compute a content SHA-256 of the
#     just-fetched History frame.
#   * The fingerprint of the History snapshot used to generate the
#     CURRENT saved CSV lives in a tiny sidecar at
#     ``Files/RO Tracking/RO_Reporting/.history_fingerprint.txt``.
#   * If the two fingerprints disagree (or the sidecar is missing),
#     a regen is needed.  We rebuild from the picker's pair, save,
#     and update the sidecar atomically (sidecar is updated AFTER
#     a successful save so a crash mid-write doesn't lie about state).
#
# Why a content hash and not the Fabric ETag:
#
#   * ETag semantics differ across Azure SDK versions (some return
#     ``None`` for in-place overwrites).  A content hash is portable
#     and deterministic — same bytes ↔ same fingerprint regardless
#     of which client read the file.
#   * Cost is negligible for the ~K-row History size; SHA-256 over a
#     CSV serialisation is sub-100 ms on every realistic input.

@dataclass(frozen=True)
class AutoRegenResult:
    """Outcome of a successful auto-regenerate run.

    Returned by :func:`auto_regenerate_if_history_changed` so the
    page can render an accurate banner without re-deriving anything
    that was just computed in the orchestrator.

    Attributes
    ----------
    prior_month, le_month
        The (Prior, LE) pair used for the rebuild — exactly what the
        picker was set to when the orchestrator ran.
    rows_saved
        Row count of the freshly-saved comparison frame (header
        excluded).  Surfaced in the info banner so the planner can
        sanity-check at a glance.
    warnings
        :class:`ComparisonWarnings` from the rebuild.  The page
        decides whether to surface them — e.g. "X items missing
        Brand" — through its existing warning banner.
    blob_path
        POSIX path of the saved CSV.  Echoed in the banner.
    """
    prior_month: date
    le_month: date
    rows_saved: int
    warnings: "ComparisonWarnings"
    blob_path: str


def compute_history_fingerprint(df: pd.DataFrame) -> str:
    """Return a stable SHA-256 hex digest of the History frame.

    All cells are cast to string before hashing so dtype drift (e.g.
    a numeric column read as int on one machine and float on another)
    doesn't false-trigger a fingerprint mismatch.  Index is excluded
    for the same reason — pandas' default RangeIndex isn't part of
    the data semantics.

    Public because the page caches this value in ``st.session_state``
    so its manual Save handler can anchor the sidecar to the same
    snapshot the orchestrator used — without re-fetching History.
    """
    if df is None:
        return ""
    payload = df.astype(str).to_csv(index=False).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def read_history_fingerprint() -> Optional[str]:
    """Return the saved History fingerprint, or ``None`` if absent.

    Absent sidecar is NOT an error — it means we've never run an
    auto-regen against this lakehouse before, OR the sidecar was
    manually deleted.  Either way, the orchestrator treats the
    response as "needs regen" so the next save will (re-)anchor the
    sidecar to the current History state.
    """
    try:
        raw, _etag = read_bytes(_SECRETS_SECTION, _HISTORY_FINGERPRINT_BLOB_PATH)
    except LakehouseIOError as exc:
        raise RoComparisonError(
            "Could not read History fingerprint sidecar from "
            f"'Files/{_HISTORY_FINGERPRINT_BLOB_PATH}': {exc}"
        ) from exc
    if raw is None:
        return None
    return raw.decode("utf-8").strip() or None


def write_history_fingerprint(fingerprint: str) -> None:
    """Overwrite the History fingerprint sidecar with *fingerprint*.

    Caller is responsible for sequencing — write the sidecar AFTER a
    successful comparison Save so a half-completed regen doesn't
    leave the sidecar lying about provenance.
    """
    if not fingerprint:
        raise RoComparisonError(
            "Refusing to write an empty History fingerprint — caller bug."
        )
    try:
        write_bytes(
            _SECRETS_SECTION,
            _HISTORY_FINGERPRINT_BLOB_PATH,
            fingerprint.encode("utf-8"),
            etag=None,
        )
    except LakehouseIOError as exc:
        raise RoComparisonError(
            "Could not write History fingerprint sidecar to "
            f"'Files/{_HISTORY_FINGERPRINT_BLOB_PATH}': {exc}"
        ) from exc


def detect_history_change(history_df: pd.DataFrame) -> Optional[str]:
    """Return current fingerprint when a regen is needed, else ``None``.

    Cheap pre-flight check the page should call BEFORE deciding whether
    to spin / show a "regenerating…" toast.  Returns:

      * ``None`` when the saved sidecar fingerprint matches the
        in-memory History fingerprint (no regen needed).
      * The current fingerprint hex string otherwise (regen needed —
        pass this fingerprint to :func:`regenerate_comparison_output`
        so the heavy path doesn't re-hash).

    Splitting this from the heavy path lets the caller skip the
    spinner-induced widget greyout on the common no-op render — see
    the page's ``_maybe_auto_regenerate_comparison_output`` for the
    UX rationale.
    """
    if history_df is None or history_df.empty:
        return None
    current_fp = compute_history_fingerprint(history_df)
    saved_fp = read_history_fingerprint()
    if saved_fp == current_fp:
        return None
    return current_fp


def regenerate_comparison_output(
    history_df: pd.DataFrame,
    dimitems_df: Optional[pd.DataFrame],
    item_master_df: Optional[pd.DataFrame],
    prior_month: date,
    le_month: date,
    *,
    history_fingerprint: Optional[str] = None,
) -> AutoRegenResult:
    """Heavy path: build → save → update sidecar — UNCONDITIONAL.

    Caller is responsible for deciding WHEN to invoke this (typically
    after :func:`detect_history_change` returns non-None, OR via a
    user-driven "Refresh from Fabric" button that always wants a
    rebuild regardless of fingerprint state).

    *history_fingerprint* is optional only so callers without a
    pre-computed value can use this as a one-shot helper; when
    omitted we hash *history_df* here.  Pass the value returned by
    :func:`detect_history_change` to avoid re-hashing.

    Sequencing (matters — a crash here must not corrupt state):
      1. Build the comparison via :func:`build_ro_comparison`.
      2. Save the comparison via :func:`save_ro_comparison_output`.
      3. ONLY on save success, write the sidecar.  If the sidecar
         write fails AFTER a successful save, the next page load
         will simply re-detect via fingerprint mismatch and re-save
         — same end state, no data loss.

    Raises :class:`RoComparisonError` on any underlying I/O or build
    failure so the page can surface a single clear error toast.
    """
    if history_df is None or history_df.empty:
        raise RoComparisonError(
            "Cannot regenerate RO_Comparison_Output.csv from empty History."
        )

    fp = history_fingerprint or compute_history_fingerprint(history_df)

    logger.info(
        "Regenerating RO_Comparison_Output.csv for (Prior=%s, LE=%s) "
        "with fingerprint %s.",
        prior_month, le_month, fp[:12],
    )

    summary_df, warnings = build_ro_comparison(
        history_df, dimitems_df, prior_month, le_month,
        item_master_df=item_master_df,
    )
    blob_path = save_ro_comparison_output(summary_df)
    write_history_fingerprint(fp)

    return AutoRegenResult(
        prior_month=prior_month,
        le_month=le_month,
        rows_saved=int(len(summary_df)),
        warnings=warnings,
        blob_path=blob_path,
    )


def auto_regenerate_if_history_changed(
    history_df: pd.DataFrame,
    dimitems_df: Optional[pd.DataFrame],
    item_master_df: Optional[pd.DataFrame],
    prior_month: date,
    le_month: date,
) -> Optional[AutoRegenResult]:
    """Thin wrapper: detect + regen iff needed.

    Convenience for callers that don't need the cheap-vs-heavy split
    (e.g., tests).  Returns ``None`` when no regen was needed,
    :class:`AutoRegenResult` otherwise.  See
    :func:`detect_history_change` and :func:`regenerate_comparison_output`
    for the underlying semantics.
    """
    fp = detect_history_change(history_df)
    if fp is None:
        return None
    return regenerate_comparison_output(
        history_df, dimitems_df, item_master_df, prior_month, le_month,
        history_fingerprint=fp,
    )


# ── Re-export contract ──────────────────────────────────────────────────────

__all__ = [
    "OUTPUT_COLUMNS",
    "SUBTOTAL_COLUMNS",
    # Renamed column constants — single source of truth for the
    # planner-friendly names the page / CSV / per-Format summary share.
    "ANNUAL_OPP_PRIOR", "ANNUAL_OPP_LE", "ANNUAL_OPP_CHANGE",
    "YEAR1_PROB_PRIOR", "YEAR1_PROB_LE", "YEAR1_PROB_CHANGE",
    "CUR_FISCAL_PROB_PRIOR", "CUR_FISCAL_PROB_LE", "CUR_FISCAL_PROB_CHANGE",
    # Per-Format summary surface.
    "PER_FORMAT_FORMAT_COL", "PER_FORMAT_DELTA_COL",
    "PER_FORMAT_ANNUAL_DELTA_COL",
    "PER_FORMAT_DRIVER_COLS", "PER_FORMAT_TOTAL_LABEL",
    "PER_FORMAT_DRIVER_BLANK_LABEL",
    "compute_per_format_summary",
    "compute_per_format_summary_annualized",
    "compute_driver_items",
    # Value objects + errors.
    "ComparisonWarnings",
    "RoComparisonError",
    "AutoRegenResult",
    # Pure transforms.
    "build_ro_comparison",
    # Fabric I/O.
    "fetch_dimitems_df",
    "fetch_ro_history_df",
    "fetch_ro_item_master_df",
    "fetch_ro_item_master_raw_bytes",
    "ro_item_master_blob_path",
    "list_months",
    "save_ro_comparison_output",
    "fetch_ro_comparison_output_df",
    "upload_customer_input",
    # Auto-regenerate flow.
    "auto_regenerate_if_history_changed",
    "compute_history_fingerprint",
    "detect_history_change",
    "read_history_fingerprint",
    "regenerate_comparison_output",
    "write_history_fingerprint",
    # Exposed for the page's "live recompute after edits" path; not for
    # general consumption.
    "_recompute_derived_columns",
]
