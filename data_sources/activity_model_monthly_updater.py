"""
Monthly auto-updater for files in the ``Activity_Model`` folder.

Two rules fire once per calendar month, the first time the New Price
Quote page renders on or after the 1st of a new month (vs. the cursor's
last-handled value):

1. **Delivery rule** — append ``Rest HTST Freight Mover ($/Gal)`` from
   ``Movers_Non_Milk_Tracker.csv`` (for the new month) to every row of
   ``Delivery_Miles Tier_Drop Size Tier_Fee.csv`` EXCEPT the
   "non-applicable" / "n/a" Mileage Fee Tier row, which is set / kept
   at 0 forever.

2. **PPPI rule** — left-merge ``rest_htst_resin_mover_fg.csv`` onto
   ``Product_Processing_Pkg_Ing.csv`` by Product ID / Item; ADD
   ``Resin Mover ($/Gal)`` to existing ``Packaging ($/Gal)`` for
   matched rows; leave unmatched rows untouched.

The May-2026 PMBC rule that used to live here has been **retired**.
``Product_Milk Base Cost.csv`` is now rewritten exclusively from the
Market Barometer's **Refresh** handler via
``data_sources/product_milk_base_cost_store.py``.  Nothing in this
module touches that file any more.

All-or-nothing cursor semantics
-------------------------------
The two live rules share ONE cursor (``activity_model_monthly_state.json``
→ ``last_handled_month``). A render fires the rules only when:

* Today's first-of-month > the cursor's value, AND
* Both rules' upstream prerequisites are satisfied
  (Movers_Non_Milk_Tracker has a row for the new month, and
  rest_htst_resin_mover_fg is non-empty).

If ANY prerequisite is missing the entire render is a no-op and the
cursor stays put — the operator gets a yellow status caption naming
the missing prerequisite, and a Market-Barometer refresh on a later
render will let the orchestrator complete the catch-up.

Archive
-------
Before mutation, the Delivery and PPPI files are snapshotted to
``Files/Activity_Model/archive/<basename>__<YYYY-MM>__pre__<YYYY-MM-DD>.csv``
(date suffix ensures multiple snapshots in the same month never
overwrite).

Catch-up
--------
If the cursor is stale by >= 2 months (e.g. cursor = March, today =
June), the rules fire ONCE for today's calendar month only. Intervening
months are intentionally skipped — the upstream drivers don't carry
intermediate movers, and re-deriving them from scratch is out of
scope for this pass.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

import pandas as pd

from data_sources import fabric_lakehouse_io as _io
from data_sources import htst_activity_store as _activity_store
from data_sources import monthly_pricing_execution_store as _mpe_store


logger = logging.getLogger(__name__)


# ── Constants ────────────────────────────────────────────────────────────────

# Where the cursor / state blob lives. Co-located with the Activity_Model
# folder so it's discoverable next to the files it gates.
_STATE_BLOB_PATH:   str = "Activity_Model/activity_model_monthly_state.json"
_ARCHIVE_PREFIX:    str = "Activity_Model/archive"
_ACTIVITY_SECRETS:  str = "fabric_activity_model"
_MPE_SECRETS:       str = "fabric_monthly_pricing_execution"

# Source filenames (resolved through htst_activity_store so the registry
# of Activity_Model files stays single-source-of-truth).
_DELIVERY_FILENAME: str = "Delivery_Miles Tier_Drop Size Tier_Fee.csv"
_PPPI_FILENAME:     str = "Product_Processing_Pkg_Ing.csv"

# Canonical column names (verbatim — every Activity_Model column is
# "load-bearing" per htst_activity_store.EXPECTED_FILES; preserve the
# leading/trailing spaces in " Delivery Charge ($/Gal) ").
_DELIVERY_COL_MILEAGE_TIER:  str = "Mileage Fee Tier (Mi)"
_DELIVERY_COL_DROP_TIER:     str = "Drop Fee Tier (lbs/Drop Size)"
_DELIVERY_COL_CHARGE:        str = " Delivery Charge ($/Gal) "

_PPPI_COL_ITEM:        str = "Item"
_PPPI_COL_ITEM_DESC:   str = "Item Description"
_PPPI_COL_PROCESSING:  str = "Total Processing ($/Gal)"
_PPPI_COL_PACKAGING:   str = "Packaging ($/Gal)"
_PPPI_COL_INGREDIENTS: str = "Ingredients ($/Gal)"

# Drivers in Monthly_Pricing_Execution.
_NMT_COL_MONTH:        str = "Month"
_NMT_COL_REST_FREIGHT: str = "Rest HTST Freight Mover ($/Gal)"
_FG_COL_PRODUCT_ID:    str = "Product ID"
_FG_COL_MOVER:         str = "Resin Mover ($/Gal)"

# Case-insensitive substrings that mark the "delivery N/A" row. Anything
# containing "applicable", "n/a", or "non" (with the literal applicable)
# is treated as the row whose Delivery Charge is permanently 0.
_NA_TIER_RE: re.Pattern[str] = re.compile(
    r"(?:^|\b)(?:n/?a|non\s*applicable|not\s*applicable|applicable)\b",
    re.IGNORECASE,
)


# ── Currency parsing / formatting ────────────────────────────────────────────
#
# The Activity_Model money columns (" Delivery Charge ($/Gal) ",
# "Packaging ($/Gal)", …) are stored as DOLLAR-FORMATTED STRINGS, e.g.
# "$0.82".  A plain ``pd.to_numeric`` on those yields NaN — which, when
# ``.fillna(0.0)``-ed, silently turns "base + mover" into "0 + mover" and so
# OVERWRITES the base with the delta.  These helpers mirror
# ``processing/new_pricing_processor.parse_dollar`` so the base is parsed
# correctly, and re-emit the "$X.XX" format the file (and its downstream
# consumers) expect.

# Delivery charges / packaging are cents-denominated $/Gal — 2 dp matches the
# file's existing "$0.82" convention.  Kept as a constant so the precision is
# easy to revisit in one place.
_CURRENCY_DECIMALS: int = 2


def _parse_currency(value: object) -> float:
    """Parse a possibly ``$``/comma-formatted money cell → float (0.0 on blank).

    Accepts ``"$0.82"``, ``"1,234.5"``, ``0.82`` (already numeric), ``""`` /
    ``NaN`` (→ 0.0).  Returns 0.0 for anything unparseable so a stray cell can
    never crash the monthly run (it just contributes nothing).
    """
    if value is None:
        return 0.0
    try:
        if pd.isna(value):
            return 0.0
    except (TypeError, ValueError):
        pass
    if isinstance(value, (int, float)):
        return float(value)
    cleaned = str(value).replace("$", "").replace(",", "").strip()
    if not cleaned:
        return 0.0
    try:
        return float(cleaned)
    except (TypeError, ValueError):
        return 0.0


def _parse_currency_series(series: pd.Series) -> pd.Series:
    """Vectorised :func:`_parse_currency` over a column → float Series."""
    return series.map(_parse_currency).astype(float)


def _format_currency(value: float) -> str:
    """Render a float back as the file's ``"$X.XX"`` string convention."""
    return f"${value:,.{_CURRENCY_DECIMALS}f}"


# ── Exceptions ───────────────────────────────────────────────────────────────

class ActivityModelMonthlyUpdaterError(RuntimeError):
    """Any failure surfaced by this orchestrator."""


# ── Result types ─────────────────────────────────────────────────────────────

@dataclass
class RuleResult:
    """Outcome of one of the three rules."""
    rule:           str
    ok:             bool
    rows_changed:   int                = 0
    archive_path:   Optional[str]      = None
    message:        str                = ""
    skipped_reason: Optional[str]      = None


@dataclass
class ActivityModelUpdateResult:
    """Aggregate outcome of one orchestrator render."""
    checked_at:        datetime           = field(default_factory=datetime.now)
    fired:             bool               = False  # at least one rule mutated
    cursor_before:     Optional[pd.Timestamp] = None
    cursor_after:      Optional[pd.Timestamp] = None
    target_month:      Optional[pd.Timestamp] = None
    delivery:          Optional[RuleResult]   = None
    pppi:              Optional[RuleResult]   = None
    skipped_reason:    Optional[str]      = None
    errors:            list[str]          = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """All-or-nothing: True iff both live rules succeeded or the
        render was a clean no-op (cursor already current)."""
        if self.errors:
            return False
        if not self.fired:
            return True
        return all(
            r is not None and r.ok
            for r in (self.delivery, self.pppi)
        )

    def as_caption(self) -> str:
        """One-liner suitable for ``st.caption`` on the New Price Quote
        page."""
        when = self.checked_at.strftime("%Y-%m-%d %H:%M")
        if self.errors:
            return f"⚠️ Activity-Model monthly update at {when}: {self.errors[0]}"
        if not self.fired:
            if self.skipped_reason == "cursor-current":
                return (
                    f"✅ Activity-Model monthly update at {when}: "
                    f"already up-to-date for "
                    f"{self.cursor_after:%Y-%m}." if self.cursor_after
                    else f"✅ Activity-Model monthly update at {when}: up-to-date."
                )
            if self.skipped_reason:
                return (
                    f"⏸ Activity-Model monthly update at {when}: "
                    f"deferred — {self.skipped_reason}."
                )
            return f"✅ Activity-Model monthly update at {when}: no changes."
        # fired and ok
        target = self.target_month.strftime("%Y-%m") if self.target_month else "?"
        bits: list[str] = []
        if self.delivery and self.delivery.ok:
            bits.append(f"Delivery {self.delivery.rows_changed} row(s)")
        if self.pppi and self.pppi.ok:
            bits.append(f"PPPI {self.pppi.rows_changed} row(s)")
        joined = "; ".join(bits) if bits else "no row changes"
        return (
            f"✅ Activity-Model monthly update at {when} for {target}: "
            f"{joined}."
        )


# ── State (monthly cursor) ───────────────────────────────────────────────────

def _read_cursor() -> Optional[pd.Timestamp]:
    """Return the cursor's ``last_handled_month`` as a first-of-month
    Timestamp, or ``None`` when the state blob doesn't exist yet.

    A missing state blob means "fresh deployment". On the first ever
    render the cursor is seeded to today's calendar month so we don't
    immediately fire a rule for the current month (rules only fire
    when today.first_of_month > cursor — the seed prevents an
    accidental same-month fire).
    """
    try:
        state, _etag = _io.read_json(_ACTIVITY_SECRETS, _STATE_BLOB_PATH)
    except _io.LakehouseIOError as exc:
        msg = str(exc).lower()
        if "not found" in msg or "does not exist" in msg:
            return None
        raise ActivityModelMonthlyUpdaterError(str(exc)) from exc
    if not isinstance(state, dict):
        return None
    raw = state.get("last_handled_month")
    if not raw:
        return None
    ts = pd.to_datetime(raw, errors="coerce")
    if pd.isna(ts):
        return None
    return pd.Timestamp(ts).normalize().replace(day=1)


def _write_cursor(value: pd.Timestamp) -> None:
    """Persist the cursor's last-handled month + audit metadata."""
    def _mutate(current: Any) -> dict[str, Any]:
        if not isinstance(current, dict):
            current = {}
        current["last_handled_month"] = value.strftime("%Y-%m-%d")
        current["last_updated_at"] = datetime.utcnow().isoformat(timespec="seconds") + "Z"
        return current

    try:
        _io.update_json(_ACTIVITY_SECRETS, _STATE_BLOB_PATH, _mutate, initial_default={})
    except _io.LakehouseIOError as exc:
        raise ActivityModelMonthlyUpdaterError(
            f"Failed updating activity-model cursor at {_STATE_BLOB_PATH}: {exc}"
        ) from exc


def _seed_cursor_if_missing(today_first_of_month: pd.Timestamp) -> pd.Timestamp:
    """First-deployment seed.

    Sets the cursor to today's calendar month so the first render after
    deployment is a no-op (the user's spec — "start tracking current
    month as May and new month as June" — relies on this).

    Returns the (possibly-seeded) cursor value.
    """
    current = _read_cursor()
    if current is not None:
        return current
    _write_cursor(today_first_of_month)
    return today_first_of_month


# ── Path resolution helpers ──────────────────────────────────────────────────

def _activity_blob_path(filename: str) -> str:
    """Resolve an Activity_Model filename to its lakehouse blob path."""
    for spec in _activity_store.EXPECTED_FILES:
        if spec.filename == filename:
            return spec.blob_path
    raise ActivityModelMonthlyUpdaterError(
        f"{filename!r} is not registered in htst_activity_store.EXPECTED_FILES."
    )


def _first_of_month(ts: pd.Timestamp) -> pd.Timestamp:
    """Normalise any timestamp to the 1st of its calendar month."""
    return pd.Timestamp(ts).normalize().replace(day=1)


# ── Pre-flight: read the two live drivers ────────────────────────────────────

@dataclass
class _Preflight:
    """Read-only snapshot of every input needed to fire the live rules.

    Holds only the live (Delivery + PPPI) inputs.  PMBC was retired in
    May-2026 — the Market Barometer Refresh handler is the sole writer.
    """
    delivery_df:    pd.DataFrame
    delivery_etag:  Optional[str]
    pppi_df:        pd.DataFrame
    pppi_etag:      Optional[str]
    rest_freight:   Optional[float]   # the new-month NMT lookup
    fg_df:          pd.DataFrame      # rest_htst_resin_mover_fg
    missing:        list[str]          # human-readable list of prereqs that failed


def _preflight(target_month: pd.Timestamp) -> _Preflight:
    """Read every Activity_Model + driver file once. Caller decides
    whether to proceed based on :attr:`_Preflight.missing`."""
    missing: list[str] = []

    # 1. Delivery.
    try:
        delivery_df, delivery_etag = _io.read_csv(
            _ACTIVITY_SECRETS, _activity_blob_path(_DELIVERY_FILENAME),
        )
    except _io.LakehouseIOError as exc:
        delivery_df, delivery_etag = pd.DataFrame(), None
        missing.append(f"could not read {_DELIVERY_FILENAME}: {exc}")

    # 2. PPPI.
    try:
        pppi_df, pppi_etag = _io.read_csv(
            _ACTIVITY_SECRETS, _activity_blob_path(_PPPI_FILENAME),
        )
    except _io.LakehouseIOError as exc:
        pppi_df, pppi_etag = pd.DataFrame(), None
        missing.append(f"could not read {_PPPI_FILENAME}: {exc}")

    # 3. Movers_Non_Milk_Tracker → Rest HTST Freight Mover for target_month.
    rest_freight = _lookup_rest_freight_mover(target_month)
    if rest_freight is None:
        missing.append(
            f"no Rest HTST Freight Mover ($/Gal) row for "
            f"{target_month:%Y-%m} in Movers_Non_Milk_Tracker.csv — "
            f"run a Market-Barometer Refresh for {target_month:%B %Y} first"
        )

    # 4. rest_htst_resin_mover_fg (FG file for the PPPI rule).
    try:
        fg_df, _fg_etag = _io.read_csv(
            _MPE_SECRETS, _mpe_store.REST_FG_BLOB_PATH,
        )
    except _io.LakehouseIOError as exc:
        fg_df = pd.DataFrame()
        missing.append(f"could not read rest_htst_resin_mover_fg.csv: {exc}")
    if fg_df.empty:
        missing.append(
            "rest_htst_resin_mover_fg.csv is empty — publish a Market-Barometer "
            "Refresh so the FG has rows before the monthly update can fire"
        )

    return _Preflight(
        delivery_df=delivery_df,
        delivery_etag=delivery_etag,
        pppi_df=pppi_df,
        pppi_etag=pppi_etag,
        rest_freight=rest_freight,
        fg_df=fg_df,
        missing=missing,
    )


def _lookup_rest_freight_mover(target_month: pd.Timestamp) -> Optional[float]:
    """Return the ``Rest HTST Freight Mover ($/Gal)`` value for the row
    in ``Movers_Non_Milk_Tracker.csv`` whose ``Month`` equals
    ``target_month``. Returns ``None`` when the file is missing, the
    column is missing, no row matches, or the cell isn't numeric.
    """
    try:
        df, _etag = _io.read_csv(_MPE_SECRETS, _mpe_store.MOVERS_NON_MILK_TRACKER_BLOB_PATH)
    except _io.LakehouseIOError:
        return None
    if df is None or df.empty:
        return None
    if _NMT_COL_MONTH not in df.columns or _NMT_COL_REST_FREIGHT not in df.columns:
        return None
    months = pd.to_datetime(df[_NMT_COL_MONTH], errors="coerce")
    mask = months.dt.normalize().dt.to_period("M") == target_month.to_period("M")
    if not mask.any():
        return None
    # Currency-aware (defensive): strip $/commas then to_numeric so a
    # "$0.05"-style mover cell resolves instead of coercing to NaN (which would
    # make the whole monthly run no-op with "no Rest HTST Freight Mover row").
    val = pd.to_numeric(
        df.loc[mask, _NMT_COL_REST_FREIGHT].astype(str)
        .str.replace("$", "", regex=False)
        .str.replace(",", "", regex=False)
        .str.strip(),
        errors="coerce",
    ).dropna()
    if val.empty:
        return None
    return float(val.iloc[-1])


# ── Archive helper ───────────────────────────────────────────────────────────

def _archive_existing_blob(
    *, secrets: str, source_blob_path: str, target_month: pd.Timestamp,
) -> Optional[str]:
    """Snapshot the current bytes of ``source_blob_path`` into the
    archive folder before mutation.

    Returns the archive blob path on success or ``None`` when the
    source doesn't exist yet (first-ever run — nothing to archive).
    """
    try:
        body, _etag = _io.read_bytes(secrets, source_blob_path)
    except _io.LakehouseIOError as exc:
        msg = str(exc).lower()
        if "not found" in msg or "does not exist" in msg:
            return None
        raise ActivityModelMonthlyUpdaterError(
            f"Failed reading {source_blob_path} for archive: {exc}"
        ) from exc

    basename = source_blob_path.rsplit("/", 1)[-1]
    if basename.lower().endswith(".csv"):
        stem = basename[: -len(".csv")]
    else:
        stem = basename
    date_suffix = datetime.now().strftime("%Y-%m-%d")
    archive_blob = (
        f"{_ARCHIVE_PREFIX}/{stem}__{target_month:%Y-%m}__pre__{date_suffix}.csv"
    )
    try:
        _io.write_bytes(secrets, archive_blob, body, etag=None)
    except _io.LakehouseIOError as exc:
        raise ActivityModelMonthlyUpdaterError(
            f"Failed writing archive copy {archive_blob}: {exc}"
        ) from exc
    return archive_blob


# ── Rule implementations ─────────────────────────────────────────────────────

def _apply_delivery_rule(
    df: pd.DataFrame, *,
    etag: Optional[str],
    target_month: pd.Timestamp,
    rest_freight: float,
) -> RuleResult:
    """Add ``rest_freight`` to every Delivery Charge except the
    'N/A'-tier row (set/keep that row at 0)."""
    if df.empty:
        return RuleResult(
            rule="delivery", ok=False,
            message=f"{_DELIVERY_FILENAME} is empty.",
        )
    required = (_DELIVERY_COL_MILEAGE_TIER, _DELIVERY_COL_CHARGE)
    missing = [c for c in required if c not in df.columns]
    if missing:
        return RuleResult(
            rule="delivery", ok=False,
            message=(
                f"{_DELIVERY_FILENAME} is missing required column(s) "
                f"{missing!r}. Expected verbatim: {list(required)}."
            ),
        )

    archive_path = _archive_existing_blob(
        secrets=_ACTIVITY_SECRETS,
        source_blob_path=_activity_blob_path(_DELIVERY_FILENAME),
        target_month=target_month,
    )

    out = df.copy()
    # Parse the existing "$X.XX" base with the currency-aware parser (a plain
    # to_numeric would read "$0.82" as NaN→0 and OVERWRITE the base with the
    # delta — the bug this fixes).
    existing = _parse_currency_series(out[_DELIVERY_COL_CHARGE])
    is_na_tier = out[_DELIVERY_COL_MILEAGE_TIER].astype(str).apply(_is_na_tier)

    # N/A row: 0 forever. All other rows: existing + rest_freight (incorporate
    # the delta on top of the base — NOT overwrite it).
    new_charge = existing + rest_freight
    new_charge = new_charge.where(~is_na_tier, 0.0)
    rows_changed = int((new_charge != existing).sum())
    # Re-emit in the file's "$X.XX" string convention.
    out[_DELIVERY_COL_CHARGE] = new_charge.map(_format_currency)

    try:
        _io.write_csv(
            _ACTIVITY_SECRETS, _activity_blob_path(_DELIVERY_FILENAME),
            out, etag=etag,
        )
    except _io.LakehouseIOError as exc:
        return RuleResult(
            rule="delivery", ok=False, archive_path=archive_path,
            message=f"Wrote nothing (read OK, write failed): {exc}",
        )
    return RuleResult(
        rule="delivery", ok=True, rows_changed=rows_changed,
        archive_path=archive_path,
        message=(
            f"Added Rest HTST Freight Mover ${rest_freight:.4f}/gal to "
            f"{rows_changed} row(s); N/A-tier row kept at $0."
        ),
    )


def _is_na_tier(label: object) -> bool:
    """Fuzzy match for the 'non-applicable' Mileage Fee Tier row.

    Anything that looks like "N/A", "n.a.", "non applicable", "not
    applicable", "applicable" (the last covers the most common typo
    of just "applicable") wins. Empty / NaN cells return False.
    """
    if label is None:
        return False
    s = str(label).strip()
    if not s or s.lower() in {"nan", "none"}:
        return False
    return bool(_NA_TIER_RE.search(s))


def _apply_pppi_rule(
    df: pd.DataFrame, *,
    etag: Optional[str],
    target_month: pd.Timestamp,
    fg_df: pd.DataFrame,
) -> RuleResult:
    """Add ``Resin Mover ($/Gal)`` to ``Packaging ($/Gal)`` by Item /
    Product ID. Unmatched rows are left untouched.
    """
    if df.empty:
        return RuleResult(
            rule="pppi", ok=False,
            message=f"{_PPPI_FILENAME} is empty.",
        )
    if _PPPI_COL_ITEM not in df.columns or _PPPI_COL_PACKAGING not in df.columns:
        return RuleResult(
            rule="pppi", ok=False,
            message=(
                f"{_PPPI_FILENAME} missing required column(s). "
                f"Expected at minimum: '{_PPPI_COL_ITEM}', "
                f"'{_PPPI_COL_PACKAGING}'."
            ),
        )
    if _FG_COL_PRODUCT_ID not in fg_df.columns or _FG_COL_MOVER not in fg_df.columns:
        return RuleResult(
            rule="pppi", ok=False,
            message=(
                f"rest_htst_resin_mover_fg.csv missing required column(s). "
                f"Expected: '{_FG_COL_PRODUCT_ID}', '{_FG_COL_MOVER}'."
            ),
        )

    archive_path = _archive_existing_blob(
        secrets=_ACTIVITY_SECRETS,
        source_blob_path=_activity_blob_path(_PPPI_FILENAME),
        target_month=target_month,
    )

    # Build {Product ID → Resin Mover} lookup. Convert to string keys so
    # int/string mismatches don't silently miss-match. If duplicates
    # exist, the LAST row wins (preserves the FG's intended ordering).
    fg_clean = fg_df[[_FG_COL_PRODUCT_ID, _FG_COL_MOVER]].copy()
    fg_clean[_FG_COL_PRODUCT_ID] = fg_clean[_FG_COL_PRODUCT_ID].astype(str).str.strip()
    # Currency-aware parse of the mover, but keep the drop-invalid behaviour:
    # strip $/commas then to_numeric so blank / unparseable cells become NaN and
    # are dropped from the lookup (rather than silently added as 0).
    fg_clean[_FG_COL_MOVER] = pd.to_numeric(
        fg_clean[_FG_COL_MOVER].astype(str)
        .str.replace("$", "", regex=False)
        .str.replace(",", "", regex=False)
        .str.strip(),
        errors="coerce",
    )
    fg_clean = fg_clean.dropna(subset=[_FG_COL_MOVER])
    lookup: dict[str, float] = dict(zip(
        fg_clean[_FG_COL_PRODUCT_ID],
        fg_clean[_FG_COL_MOVER].astype(float),
    ))
    if not lookup:
        return RuleResult(
            rule="pppi", ok=False, archive_path=archive_path,
            message="rest_htst_resin_mover_fg.csv had no valid Resin Mover rows.",
        )

    out = df.copy()
    keys = out[_PPPI_COL_ITEM].astype(str).str.strip()
    movers = keys.map(lookup)  # NaN where unmatched
    matched_mask = movers.notna()
    # Parse the existing "$X.XX" packaging base with the currency-aware parser
    # (a plain to_numeric would read "$1.23" as NaN→0 and OVERWRITE the base
    # with the mover — the same bug fixed in the Delivery rule).
    existing_packaging = _parse_currency_series(out[_PPPI_COL_PACKAGING])
    addition = movers.fillna(0.0)
    new_packaging = existing_packaging + addition

    # Only matched rows change (base + mover), re-emitted in the "$X.XX"
    # convention; unmatched rows keep their original packaging cell untouched.
    rows_changed = int(matched_mask.sum())
    out.loc[matched_mask, _PPPI_COL_PACKAGING] = (
        new_packaging.loc[matched_mask].map(_format_currency).values)

    try:
        _io.write_csv(
            _ACTIVITY_SECRETS, _activity_blob_path(_PPPI_FILENAME),
            out, etag=etag,
        )
    except _io.LakehouseIOError as exc:
        return RuleResult(
            rule="pppi", ok=False, archive_path=archive_path,
            message=f"Wrote nothing (read OK, write failed): {exc}",
        )

    return RuleResult(
        rule="pppi", ok=True, rows_changed=rows_changed,
        archive_path=archive_path,
        message=(
            f"Added Resin Mover to {rows_changed} matched row(s). "
            f"{int((~matched_mask).sum())} row(s) had no FG match — left unchanged."
        ),
    )


# ── Public entry points ──────────────────────────────────────────────────────

def is_update_due(today: Optional[datetime] = None) -> bool:
    """Cheap pre-render check: True iff a fire would actually run.

    Compares today's first-of-month against the persisted cursor.
    Does NOT touch the upstream drivers — those are checked lazily
    inside :func:`run_if_due`. Useful for the New Price Quote page so
    it can decide whether to surface the "Run now" button prominently.
    """
    today = today or datetime.now()
    today_first = _first_of_month(pd.Timestamp(today))
    try:
        cursor = _read_cursor()
    except ActivityModelMonthlyUpdaterError:
        return False
    if cursor is None:
        # Never seeded → first render will seed-and-noop, not fire.
        return False
    return today_first > cursor


def run_if_due(
    *,
    force: bool = False,
    today: Optional[datetime] = None,
) -> ActivityModelUpdateResult:
    """Render-side entry point: fire all three rules if the calendar
    month has rolled over since the cursor was last bumped.

    Parameters
    ----------
    force
        When True, bypass the cursor check entirely. Used by the
        manual "Run monthly updates" button so an operator can replay
        a missed update after fixing whichever prerequisite was
        previously missing.
    today
        Override for the current date. Defaults to ``datetime.now()``
        so production callers don't have to construct one.

    Returns
    -------
    ActivityModelUpdateResult
        Always returns — never raises. The caller renders the
        :meth:`as_caption` for status and inspects the per-rule
        ``RuleResult.ok`` flags if it wants finer-grained UI.
    """
    result = ActivityModelUpdateResult()
    today = today or datetime.now()
    today_first = _first_of_month(pd.Timestamp(today))
    result.target_month = today_first

    # 1. Seed the cursor on first-ever deployment.
    try:
        cursor = _seed_cursor_if_missing(today_first)
    except ActivityModelMonthlyUpdaterError as exc:
        result.errors.append(str(exc))
        return result
    result.cursor_before = cursor
    result.cursor_after = cursor

    # 2. Cursor-current short-circuit.
    if not force and today_first <= cursor:
        result.skipped_reason = "cursor-current"
        return result

    # 3. Pre-flight all prerequisites (read-only).
    try:
        preflight = _preflight(today_first)
    except ActivityModelMonthlyUpdaterError as exc:
        result.errors.append(str(exc))
        return result

    if preflight.missing:
        # Yellow status — cursor stays put, retry on next render.
        result.skipped_reason = preflight.missing[0]
        for extra in preflight.missing[1:]:
            logger.info("activity-model preflight blocker (extra): %s", extra)
        return result

    # 4. Apply rules in fixed order. ``fired = True`` once we start
    #    mutating; the cursor advances ONLY when both live rules
    #    (Delivery + PPPI) succeed.
    result.fired = True

    # Delivery
    result.delivery = _apply_delivery_rule(
        preflight.delivery_df,
        etag=preflight.delivery_etag,
        target_month=today_first,
        rest_freight=preflight.rest_freight,  # type: ignore[arg-type]
    )

    # PPPI
    result.pppi = _apply_pppi_rule(
        preflight.pppi_df,
        etag=preflight.pppi_etag,
        target_month=today_first,
        fg_df=preflight.fg_df,
    )

    # 5. Both live rules must report ok before we advance the cursor.
    if all(r.ok for r in (result.delivery, result.pppi)):
        try:
            _write_cursor(today_first)
            result.cursor_after = today_first
        except ActivityModelMonthlyUpdaterError as exc:
            # The mutations succeeded but the cursor failed to advance —
            # very unusual. Log and surface; next render will see the
            # mutations succeed again (Delivery / PPPI would double-
            # apply — which is why we ALWAYS try to write the cursor
            # immediately and surface this error so the operator
            # manually inspects).
            result.errors.append(
                f"All rules succeeded but the cursor failed to advance: {exc}. "
                "DO NOT re-run the monthly update — Delivery/PPPI would "
                "double-apply. Inspect the state blob in OneLake."
            )
    else:
        # Mixed result. Cursor stays put — next render retries. The
        # mutations that DID succeed are visible in the archive folder
        # and in the live CSVs; the operator must inspect.
        failing = [r.rule for r in (result.delivery, result.pppi) if not r.ok]
        result.errors.append(
            "One or more rules failed: " + ", ".join(failing) +
            ". Cursor NOT advanced. Inspect the per-rule messages."
        )

    return result


def get_state_blob_label() -> str:
    """Short human-readable label for the state blob in OneLake."""
    return _io.LakehouseRef(
        secrets_section=_ACTIVITY_SECRETS,
        blob_path=_STATE_BLOB_PATH,
    ).display


__all__ = [
    "ActivityModelMonthlyUpdaterError",
    "ActivityModelUpdateResult",
    "RuleResult",
    "get_state_blob_label",
    "is_update_due",
    "run_if_due",
]
