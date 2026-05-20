"""
OneLake-backed writer for ``Files/Activity_Model/Product_Milk Base Cost.csv``.

Refresh-driven contract (May-2026)
----------------------------------
The Monthly Movers section of the Market Barometer page is the **sole
writer** of this file.  Whenever the user clicks **Refresh** next to
the Movers Non-Milk Tracker, this module:

1. Reads ``Product_Milk Base Cost.csv`` from
   ``Files/Activity_Model/`` in the Pricing Lakehouse.
2. Compares the file's max ``Month`` against the slicer's End Month.
3. If ``end_month >= max(Month) + 1 month`` (i.e. the End Month is at
   least one calendar month newer than the latest already in the file),
   left-merges the per-item ``End Month Milk Cost`` payload onto PMBC
   by **Item** and overwrites the ``Base Milk Cost per Gallon`` column.
   Unmatched rows are blanked + stale-stamped exactly the way the
   legacy ``activity_model_monthly_updater`` did so audit history
   continues to read consistently.
4. Stamps ``Month`` to the new End Month and ``Source`` to a
   refresh-specific label, then writes the file back through the
   ETag-based optimistic-concurrency layer.

The "End Month newer than file" gate is what makes the write idempotent:
clicking Refresh twice for the same End Month is a no-op on the second
click.  The legacy monthly-cursor PMBC path in
``activity_model_monthly_updater`` has been retired in favour of this
Refresh-driven path so PMBC reflects the operator's explicit action,
not an opaque calendar rollover.

Storage layout
--------------
``Files/Activity_Model/Product_Milk Base Cost.csv``

Configuration
-------------
Reads ``workspace`` and ``lakehouse`` from
``[fabric_product_milk_base_cost]`` when present, falling back to
``[fabric_activity_model]`` and then ``[fabric_htst]`` so existing
deployments don't need to add new secret blocks.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import pandas as pd

from data_sources import fabric_lakehouse_io as _io


logger = logging.getLogger(__name__)


# ── Public errors ────────────────────────────────────────────────────────────

class ProductMilkBaseCostStoreError(RuntimeError):
    """Raised on configuration / auth / I/O failure for this store."""


# ── Constants ────────────────────────────────────────────────────────────────

# Preferred secrets block. Falls back to the shared Activity_Model block
# through :func:`fabric_lakehouse_io._read_lakehouse_config`.
_SECRETS_SECTION: str = "fabric_product_milk_base_cost"

# Blob path matches the URL the user shared (and the registered entry
# in ``htst_activity_store.EXPECTED_FILES``).
_BLOB_PATH: str = "Activity_Model/Product_Milk Base Cost.csv"

# Canonical column names — verbatim from the production file. The
# Base Milk Cost column intentionally carries leading + trailing spaces
# in its header because that is the schema downstream readers expect.
COL_ITEM:      str = "Item"
COL_ITEM_DESC: str = "Item Description"
COL_BASE_COST: str = " Base Milk Cost per Gallon "
COL_MONTH:     str = "Month"
COL_SOURCE:    str = "Source"

# Source-stamp labels — the refresh-driven label is distinct from the
# legacy monthly-cursor label so audit logs can tell the two writers
# apart.  When the legacy cursor path is fully retired this label
# becomes the only one in use.
SOURCE_REFRESH_LABEL: str = "Auto-update from Market Barometer Refresh"
STALE_SOURCE_TEMPLATE: str = "Stale — no End Month Milk Cost for {month}"


# ── Result type ──────────────────────────────────────────────────────────────

@dataclass
class PMBCUpdateResult:
    """Structured outcome of one Refresh-driven PMBC write."""
    ok:             bool                   = False
    skipped_reason: Optional[str]          = None
    rows_changed:   int                    = 0
    matched_items:  int                    = 0
    unmatched_items: int                   = 0
    end_month:      Optional[pd.Timestamp] = None
    previous_max:   Optional[pd.Timestamp] = None
    message:        str                    = ""

    def as_caption(self) -> str:
        """Compact one-liner for ``st.caption``."""
        if not self.ok:
            return f"⚠️ Product_Milk Base Cost not updated: {self.message or self.skipped_reason or 'unknown error'}"
        if self.skipped_reason:
            return f"ℹ️ Product_Milk Base Cost: {self.skipped_reason}"
        em = self.end_month.strftime("%Y-%m") if self.end_month is not None else "?"
        return (
            f"✅ Product_Milk Base Cost updated to End Month {em}: "
            f"{self.matched_items} matched, {self.unmatched_items} stale-stamped."
        )


# ── Internal helpers ─────────────────────────────────────────────────────────

def _first_of_month(value) -> Optional[pd.Timestamp]:
    if value is None:
        return None
    ts = pd.to_datetime(value, errors="coerce")
    if pd.isna(ts):
        return None
    return pd.Timestamp(ts).normalize().replace(day=1)


def _format_month(ts: pd.Timestamp) -> str:
    """Render End Month in PMBC's native ``M/D/YYYY`` form (matches legacy)."""
    return f"{ts.month}/{ts.day}/{ts.year}"


def _max_month(df: pd.DataFrame) -> Optional[pd.Timestamp]:
    """Return the latest first-of-month Timestamp in the file's ``Month`` column.

    Tolerates the mixed ``M/D/YYYY`` / ``YYYY-MM-DD`` formats the
    legacy file has carried over the years.
    """
    if df is None or df.empty or COL_MONTH not in df.columns:
        return None
    months = df[COL_MONTH].apply(_first_of_month).dropna()
    if months.empty:
        return None
    return max(months.tolist())


# ── Public API ───────────────────────────────────────────────────────────────

def read_pmbc_df() -> tuple[pd.DataFrame, Optional[str]]:
    """Return ``(DataFrame, ETag)`` for the current Activity_Model file."""
    try:
        df, etag = _io.read_csv(_SECRETS_SECTION, _BLOB_PATH)
    except _io.LakehouseIOError as exc:
        raise ProductMilkBaseCostStoreError(str(exc)) from exc
    if df is None:
        return pd.DataFrame(), etag
    out = df.copy()
    out.columns = [str(c) for c in out.columns]
    return out, etag


def maybe_update_for_end_month(
    item_to_end_cost: dict[str, float],
    end_month,
) -> PMBCUpdateResult:
    """Overwrite ``Base Milk Cost per Gallon`` by Item when End Month is newer.

    Parameters
    ----------
    item_to_end_cost
        ``{Item → End Month Milk Cost ($/gal)}`` map for the slicer's
        End Month. Built by the page from ``milk_usage_with_movers``.
    end_month
        The slicer's End Month (anything ``pd.to_datetime`` accepts).
        Normalised to first-of-month internally.

    Behaviour
    ---------
    * Reads the existing PMBC file.
    * Computes ``file_max = max(Month)``.
    * Skips with ``skipped_reason="end_month_not_newer"`` when
      ``end_month < file_max + 1 month``.
    * Otherwise left-merges by ``Item``:
        - matched rows: ``Base Milk Cost per Gallon = item_to_end_cost[Item]``,
          ``Month = end_month`` (M/D/YYYY), ``Source = SOURCE_REFRESH_LABEL``.
        - unmatched rows: ``Base Milk Cost per Gallon = <blank>``,
          ``Month = end_month``, ``Source = STALE_SOURCE_TEMPLATE.format(...)``.
    * Refuses to write when EVERY row would be unmatched — that scenario
      almost always indicates a bad ``item_to_end_cost`` (e.g. empty
      map) and would silently blank every cost. Returns
      ``skipped_reason="no_matches"`` instead.

    Idempotent: re-clicking Refresh for the same End Month re-enters
    via the "end_month not newer" branch and is a no-op.

    Failures are NEVER raised at module boundaries — they are returned
    as a :class:`PMBCUpdateResult` with ``ok=False`` so the page render
    can surface them in a small caption without breaking the rest of
    the Refresh pipeline.
    """
    result = PMBCUpdateResult()

    em = _first_of_month(end_month)
    if em is None:
        result.message = f"end_month {end_month!r} is not parseable."
        return result
    result.end_month = em

    if not item_to_end_cost:
        result.skipped_reason = (
            "no End Month Milk Cost rows — milk slicer did not produce a payload."
        )
        result.ok = True  # not an error; just nothing to push
        return result

    try:
        df, etag = read_pmbc_df()
    except ProductMilkBaseCostStoreError as exc:
        result.message = str(exc)
        return result

    if df.empty:
        result.message = (
            f"{_BLOB_PATH} is empty or absent; cannot overwrite Base Milk Cost."
        )
        return result

    required = (COL_ITEM, COL_BASE_COST, COL_MONTH, COL_SOURCE)
    missing = [c for c in required if c not in df.columns]
    if missing:
        result.message = (
            f"{_BLOB_PATH} is missing required column(s) {missing!r}. "
            f"Expected verbatim: {list(required)}."
        )
        return result

    file_max = _max_month(df)
    result.previous_max = file_max

    # Gate: End Month must be at least one full month newer than the file's max.
    if file_max is not None and em < (file_max + pd.DateOffset(months=1)):
        result.skipped_reason = (
            f"End Month {em:%Y-%m} is not newer than file's max "
            f"({file_max:%Y-%m}); no update."
        )
        result.ok = True
        return result

    # Build the working copy and apply the left-merge by Item.
    out = df.copy()
    items_normalised = out[COL_ITEM].astype(str).str.strip()
    new_costs = items_normalised.map(
        {str(k).strip(): float(v) for k, v in item_to_end_cost.items()}
    )
    matched_mask = new_costs.notna()
    result.matched_items   = int(matched_mask.sum())
    result.unmatched_items = int((~matched_mask).sum())

    if result.matched_items == 0:
        result.skipped_reason = (
            f"No Items in {_BLOB_PATH} matched the End Month Milk Cost payload; "
            "refusing to blank every row."
        )
        return result

    existing_costs = pd.to_numeric(out[COL_BASE_COST], errors="coerce")
    target_str = _format_month(em)
    stale_label = STALE_SOURCE_TEMPLATE.format(month=target_str)

    rows_changed = 0
    for idx in out.index:
        if matched_mask.loc[idx]:
            new_val = float(new_costs.loc[idx])
            existing = existing_costs.loc[idx]
            if pd.isna(existing) or float(existing) != new_val:
                rows_changed += 1
            out.at[idx, COL_BASE_COST] = new_val
            out.at[idx, COL_MONTH]     = target_str
            out.at[idx, COL_SOURCE]    = SOURCE_REFRESH_LABEL
        else:
            existing = existing_costs.loc[idx]
            if not pd.isna(existing):
                rows_changed += 1
            out.at[idx, COL_BASE_COST] = pd.NA
            out.at[idx, COL_MONTH]     = target_str
            out.at[idx, COL_SOURCE]    = stale_label

    if df.equals(out):
        result.skipped_reason = (
            f"PMBC already reflects End Month {em:%Y-%m}; no rewrite needed."
        )
        result.ok = True
        result.rows_changed = 0
        return result

    try:
        _io.write_csv(_SECRETS_SECTION, _BLOB_PATH, out, etag=etag)
    except _io.LakehouseIOError as exc:
        result.message = f"Wrote nothing (read OK, write failed): {exc}"
        return result

    result.rows_changed = rows_changed
    result.ok = True
    return result


def get_store_label() -> str:
    """Short human-readable label for UI captions."""
    return _io.LakehouseRef(
        secrets_section=_SECRETS_SECTION,
        blob_path=_BLOB_PATH,
    ).display


__all__ = [
    "ProductMilkBaseCostStoreError",
    "PMBCUpdateResult",
    "COL_ITEM",
    "COL_ITEM_DESC",
    "COL_BASE_COST",
    "COL_MONTH",
    "COL_SOURCE",
    "SOURCE_REFRESH_LABEL",
    "STALE_SOURCE_TEMPLATE",
    "read_pmbc_df",
    "maybe_update_for_end_month",
    "get_store_label",
]
