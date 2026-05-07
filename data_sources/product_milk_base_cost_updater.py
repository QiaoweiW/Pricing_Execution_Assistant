"""
Auto-update for ``Files/Activity_Model/Product_Milk Base Cost.csv``.

Single source of truth: the **tracker's latest End Month**.

Trigger contract
----------------
On every render of the New Price Quote page:

1. Read the latest End Month present in
   ``base_milk_cost_monthly_tracker.csv``.
2. If PMBC's max ``Month`` already equals (or exceeds) that value,
   no-op — the file is current.
3. Otherwise refresh PMBC by left-joining the tracker rows for the
   target month onto PMBC by ``Item``:
   * Matched rows  → ``Base Milk Cost per Gallon`` overwritten,
                     ``Month`` advanced to the target,
                     ``Source`` stamped
                     ``"Auto-update from Base Milk Cost Monthly Tracker"``.
   * Unmatched rows → cost blanked (``NaN``), ``Month`` still advanced,
                      ``Source`` stamped
                      ``"Stale — no tracker entry for {Month}"``
                      so the gap is visible to every downstream consumer.

Anything that previously required a calendar trigger (the 1st-of-month
auto-fire, "is_update_needed compares to today", a target_month
parameter) is gone — the tracker drives PMBC, full stop.

Failure UX
----------
``UpdateResult.ok=False`` is rendered as a yellow warning + manual
"Run auto-update now" button on the New Price Quote page.

Safety net
----------
If the tracker's latest month has rows but ZERO of them match any
``Item`` in PMBC, this module REFUSES to write — wholesale-blanking
every row almost always means the tracker is malformed (different
Item encoding, accidental empty append). The operator gets a clear
error and PMBC stays untouched.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import pandas as pd

from data_sources import base_milk_cost_tracker_store as _tracker_store
from data_sources import htst_activity_store as _activity_store
from data_sources import fabric_lakehouse_io as _io


logger = logging.getLogger(__name__)


# ── Public types ─────────────────────────────────────────────────────────────

class ProductMilkBaseCostUpdateError(RuntimeError):
    """Raised on configuration / auth / I/O failure for this updater."""


@dataclass(frozen=True)
class UpdateResult:
    """Result of an auto-update attempt.

    Attributes
    ----------
    ok
        True when the Fabric copy of PMBC reflects the target month
        (whether we wrote it just now, it was already current, or
        the tracker is empty — see ``skipped_reason`` to disambiguate).
    target_month
        The first-of-month timestamp the update aimed to land at.
        ``None`` only when the tracker is empty.
    rows_updated
        Number of PMBC rows whose ``Base Milk Cost per Gallon`` changed.
    rows_blanked
        Number of PMBC rows whose ``Base Milk Cost per Gallon`` was
        wiped to NaN because they had no tracker entry for the target.
    rows_unmatched
        Number of PMBC rows that had no match in the tracker — their
        cost was blanked (see ``rows_blanked``) but Month + Source
        were still advanced.
    message
        Human-readable summary on success or failure.
    skipped_reason
        Empty when an actual write happened.
        ``"already-current"`` when PMBC was already on the target.
        ``"tracker-empty"``    when the tracker has no rows at all.
    checked_at
        Timestamp this run started — useful for log audit trails.
    """
    ok: bool
    target_month: Optional[pd.Timestamp]
    rows_updated: int = 0
    rows_blanked: int = 0
    rows_unmatched: int = 0
    message: str = ""
    skipped_reason: str = ""
    checked_at: datetime = field(default_factory=datetime.now)


# ── Constants ────────────────────────────────────────────────────────────────

_PMBC_FILENAME: str = "Product_Milk Base Cost.csv"

# Exact column names from the canonical CSV (preserved verbatim,
# including the leading/trailing spaces in " Base Milk Cost per Gallon ",
# because the downstream processor reads by literal string).
_COL_ITEM:      str = "Item"
_COL_ITEM_DESC: str = "Item Description"
_COL_BASE_COST: str = " Base Milk Cost per Gallon "
_COL_MONTH:     str = "Month"
_COL_SOURCE:    str = "Source"

_AUTO_UPDATE_SOURCE_LABEL: str = (
    "Auto-update from Base Milk Cost Monthly Tracker"
)

# Stamped on rows whose Item is absent from the tracker for the target
# month. The literal ``{month}`` placeholder is filled in at write time
# so the human-readable banner is unambiguous (e.g. "Stale — no tracker
# entry for 5/1/2026").
_STALE_SOURCE_TEMPLATE: str = "Stale — no tracker entry for {month}"


# ── Path resolution ──────────────────────────────────────────────────────────

def _pmbc_blob_path() -> str:
    """Lakehouse Files/ path for PMBC. Resolved from htst_activity_store
    so we have ONE registry of Activity_Model files."""
    for spec in _activity_store.EXPECTED_FILES:
        if spec.filename == _PMBC_FILENAME:
            return spec.blob_path
    raise ProductMilkBaseCostUpdateError(
        f"{_PMBC_FILENAME} is not registered in htst_activity_store.EXPECTED_FILES."
    )


def _pmbc_secrets_section() -> str:
    """The secrets section the Activity_Model store uses."""
    return "fabric_activity_model"


# ── Date helpers ─────────────────────────────────────────────────────────────

def _parse_pmbc_month(value) -> Optional[pd.Timestamp]:
    """Parse a value from PMBC's Month column to a first-of-month Timestamp.

    PMBC stores dates as ``M/D/YYYY`` strings (e.g. "5/1/2026"); we
    normalise to first-of-month so equality checks aren't tripped up
    by a stray "5/2/2026" if someone manually edited a row.
    """
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    ts = pd.to_datetime(value, errors="coerce")
    if pd.isna(ts):
        return None
    return pd.Timestamp(ts).normalize().replace(day=1)


def _format_pmbc_month(ts: pd.Timestamp) -> str:
    """Render a first-of-month Timestamp in PMBC's native ``M/D/YYYY`` form."""
    return f"{ts.month}/{ts.day}/{ts.year}"


# ── PMBC I/O ─────────────────────────────────────────────────────────────────

def _read_pmbc_df_with_etag() -> tuple[pd.DataFrame, Optional[str]]:
    """Return ``(df, etag)`` for the Fabric copy of PMBC."""
    try:
        df, etag = _io.read_csv(_pmbc_secrets_section(), _pmbc_blob_path())
    except _io.LakehouseIOError as exc:
        raise ProductMilkBaseCostUpdateError(str(exc)) from exc
    if df is None:
        raise ProductMilkBaseCostUpdateError(
            f"{_PMBC_FILENAME} is not in OneLake yet. "
            "Open the New Price Quote page once to trigger first-time bootstrap."
        )
    return df, etag


def _write_pmbc_df(df: pd.DataFrame, *, etag: Optional[str]) -> str:
    """Write ``df`` back to OneLake with an ETag-guarded ``If-Match``."""
    try:
        return _io.write_csv(
            _pmbc_secrets_section(),
            _pmbc_blob_path(),
            df,
            etag=etag,
        )
    except _io.LakehouseIOError as exc:
        raise ProductMilkBaseCostUpdateError(str(exc)) from exc


def _latest_pmbc_month(df: pd.DataFrame) -> Optional[pd.Timestamp]:
    """Return the maximum first-of-month-normalised Month value in PMBC."""
    if df.empty or _COL_MONTH not in df.columns:
        return None
    parsed = df[_COL_MONTH].apply(_parse_pmbc_month).dropna()
    if parsed.empty:
        return None
    return pd.Timestamp(max(parsed))


# ── Public API ───────────────────────────────────────────────────────────────

def is_update_needed() -> bool:
    """True iff the tracker's latest month is ahead of PMBC's max ``Month``.

    Returns False on any read failure — a transient OneLake hiccup
    must not *trigger* an update; the next render will re-evaluate.
    """
    try:
        target = _tracker_store.latest_month()
    except _tracker_store.BaseMilkCostTrackerError:
        return False
    if target is None:
        return False
    try:
        df, _etag = _read_pmbc_df_with_etag()
    except ProductMilkBaseCostUpdateError:
        return False
    pmbc_latest = _latest_pmbc_month(df)
    if pmbc_latest is None:
        return True  # PMBC has no Month at all — definitely behind.
    return pmbc_latest < target


def update() -> UpdateResult:
    """Refresh PMBC against the tracker's latest End Month.

    Idempotent: re-running on a freshly-updated PMBC is a no-op
    (returns ``ok=True, skipped_reason="already-current"``).
    """
    # 1. Resolve the target from the tracker (single source of truth).
    try:
        target = _tracker_store.latest_month()
    except _tracker_store.BaseMilkCostTrackerError as exc:
        return UpdateResult(
            ok=False, target_month=None,
            message=f"Could not read base_milk_cost_monthly_tracker: {exc}",
        )
    if target is None:
        return UpdateResult(
            ok=True, target_month=None,
            skipped_reason="tracker-empty",
            message=(
                "base_milk_cost_monthly_tracker is empty. PMBC was left "
                "unchanged. Generate a milk_mover Refresh in the Market "
                "Barometer to populate the tracker."
            ),
        )

    # 2. Read PMBC.
    try:
        df, etag = _read_pmbc_df_with_etag()
    except ProductMilkBaseCostUpdateError as exc:
        return UpdateResult(ok=False, target_month=target, message=str(exc))

    expected_cols = (_COL_ITEM, _COL_ITEM_DESC, _COL_BASE_COST, _COL_MONTH, _COL_SOURCE)
    missing = [c for c in expected_cols if c not in df.columns]
    if missing:
        return UpdateResult(
            ok=False, target_month=target,
            message=(
                f"{_PMBC_FILENAME} is missing required columns {missing!r}. "
                f"Expected exactly: {list(expected_cols)}."
            ),
        )

    # 3. Short-circuit when PMBC is already at (or beyond) the target.
    pmbc_latest = _latest_pmbc_month(df)
    if pmbc_latest is not None and pmbc_latest >= target:
        return UpdateResult(
            ok=True, target_month=target,
            skipped_reason="already-current",
            message=f"PMBC already reflects End Month {target:%Y-%m}.",
        )

    # 4. Build the {Item: cost} lookup for the target.
    try:
        lookup = _tracker_store.lookup_for_end_month(target)
    except _tracker_store.BaseMilkCostTrackerError as exc:
        return UpdateResult(
            ok=False, target_month=target,
            message=f"Could not read base_milk_cost_monthly_tracker: {exc}",
        )
    if not lookup:
        # latest_month() reported a value but lookup is empty — race?
        return UpdateResult(
            ok=False, target_month=target,
            message=(
                f"base_milk_cost_monthly_tracker reported End Month "
                f"{target:%Y-%m} but returned no rows for it. "
                "Inspect the tracker file in OneLake."
            ),
        )

    # 5. Apply matched / unmatched semantics.
    out = df.copy()
    out.columns = [str(c) for c in out.columns]

    items = out[_COL_ITEM].astype(str).str.strip()
    new_costs = items.map(lookup)  # NaN where no tracker entry

    matched_mask = new_costs.notna()
    rows_unmatched = int((~matched_mask).sum())

    # Safety net: tracker has rows for target but ZERO match any PMBC
    # Item → refuse to blank every row.
    if not matched_mask.any():
        return UpdateResult(
            ok=False, target_month=target,
            rows_unmatched=rows_unmatched,
            message=(
                f"base_milk_cost_monthly_tracker has rows for End Month "
                f"{target:%Y-%m} but NONE of them match Items in PMBC. "
                "Refusing to blank every row. Inspect the tracker — "
                "likely an Item-encoding mismatch — then retry."
            ),
        )

    existing_costs = pd.to_numeric(out[_COL_BASE_COST], errors="coerce")

    target_month_str   = _format_pmbc_month(target)
    stale_source_label = _STALE_SOURCE_TEMPLATE.format(month=target_month_str)

    rows_updated = 0
    rows_blanked = 0

    for idx in out.index:
        if matched_mask.loc[idx]:
            new_val = float(new_costs.loc[idx])
            existing = existing_costs.loc[idx]
            if pd.isna(existing) or float(existing) != new_val:
                rows_updated += 1
            out.at[idx, _COL_BASE_COST] = new_val
            out.at[idx, _COL_MONTH]     = target_month_str
            out.at[idx, _COL_SOURCE]    = _AUTO_UPDATE_SOURCE_LABEL
        else:
            existing = existing_costs.loc[idx]
            if not pd.isna(existing):
                rows_blanked += 1
            out.at[idx, _COL_BASE_COST] = pd.NA
            out.at[idx, _COL_MONTH]     = target_month_str
            out.at[idx, _COL_SOURCE]    = stale_source_label

    # 6. Write — but skip the round-trip when the frame didn't change.
    if df.equals(out):
        return UpdateResult(
            ok=True, target_month=target,
            rows_unmatched=rows_unmatched,
            skipped_reason="already-current",
            message=f"PMBC already reflects End Month {target:%Y-%m}.",
        )

    try:
        _write_pmbc_df(out, etag=etag)
    except ProductMilkBaseCostUpdateError as exc:
        return UpdateResult(
            ok=False, target_month=target,
            message=f"Wrote nothing (read OK, write failed): {exc}",
        )

    return UpdateResult(
        ok=True, target_month=target,
        rows_updated=rows_updated, rows_blanked=rows_blanked,
        rows_unmatched=rows_unmatched,
        message=(
            f"Updated {rows_updated} row(s) to End Month {target:%Y-%m}. "
            f"{rows_blanked} row(s) had no tracker match — their cost was "
            f"blanked and Source stamped \"{stale_source_label}\"."
        ),
    )


def update_if_needed() -> UpdateResult:
    """Run :func:`update` only when :func:`is_update_needed` returns True.

    Cheap when up-to-date; full work when stale. Used as the page-render
    hook on the New Price Quote view.
    """
    try:
        if not is_update_needed():
            # Build a non-noisy "already current" result without the full read.
            try:
                target = _tracker_store.latest_month()
            except _tracker_store.BaseMilkCostTrackerError:
                target = None
            if target is None:
                return UpdateResult(
                    ok=True, target_month=None,
                    skipped_reason="tracker-empty",
                    message="base_milk_cost_monthly_tracker is empty.",
                )
            return UpdateResult(
                ok=True, target_month=target,
                skipped_reason="already-current",
                message="No update needed.",
            )
    except Exception as exc:  # noqa: BLE001 — defensive top-level guard
        return UpdateResult(
            ok=False, target_month=None,
            message=f"Could not check update status: {exc}",
        )
    return update()


def get_store_label() -> str:
    """Short human-readable label of where PMBC lives in OneLake."""
    return _io.LakehouseRef(
        secrets_section=_pmbc_secrets_section(),
        blob_path=_pmbc_blob_path(),
    ).display


__all__ = [
    "ProductMilkBaseCostUpdateError",
    "UpdateResult",
    "is_update_needed",
    "update",
    "update_if_needed",
    "get_store_label",
]
