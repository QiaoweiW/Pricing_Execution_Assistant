"""
Auto-update orchestrator for the Milk Mover Tracker.

Workflow on each invocation
---------------------------
1. Skip if the **advanced-prices** PDF has not changed since the last check
   (HEAD with ETag / Last-Modified, falling back to body SHA-256). The
   class-prices PDF is *only* fetched when an actual update is needed.
2. Compute the target month: ``latest_month_in_db + 1 month``. If a row for
   that month already exists for any (Category, Class) combination, exit
   early — the auto-update is idempotent.
3. Parse the four advanced-prices fields plus the Class II Butterfat figure
   for ``(today.year, today.month - 1)`` from page 2 of the class-prices PDF.
4. Derive the four (Category, Class) rows per the Skim/Butterfat formulas:

   ============== ==== ==================================== ===========================
   Category       Cl   Skim Rate                            Butterfat Rate
   ============== ==== ==================================== ===========================
   HTST           I    Base Skim Milk Price for Class I/100 Advanced Butterfat Pricing
   HTST           II   Class II Skim Milk Price/100         Class II Butterfat Price
                                                            (page 2, today.year, prev mo)
   ESL            I    HTST Class I Skim
                       + Class I ESL Adjustment/100         same as HTST Class I Bfat
   ESL            II   same as HTST Class II Skim           same as HTST Class II Bfat
   ============== ==== ==================================== ===========================

5. Append to the ``milk_mover_tracker`` JSON blob in OneLake (the store
   layer dedups by ``(Category, Month, Class)`` so re-runs are no-ops) and
   persist the updated PDF fingerprint state to the companion state blob.

The orchestrator is **safe to call on every page render** — the TTL guard
(see ``maybe_update_from_pdfs``) plus the change-detection layer make
repeated calls cheap (one in-memory check / no network) once we've checked
within the cooldown window.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

import pandas as pd

from data_sources import milk_mover_store as store
from data_sources import usda_milk_pdf as pdf


# Default minimum interval between PDF change-detection checks. The HEAD call
# is cheap (~200 ms) but doing it on every Streamlit rerun would be wasteful;
# 1 hour balances freshness against bandwidth.
_DEFAULT_CHECK_TTL = timedelta(hours=1)


# ── Result type ──────────────────────────────────────────────────────────────

@dataclass
class AutoUpdateResult:
    """Structured result the UI can render in a status caption."""
    checked_at:        datetime
    advanced_changed:  bool                = False
    rows_inserted:     int                 = 0
    target_month:      Optional[pd.Timestamp] = None
    classii_bfat_lookup: Optional[pdf.ClassIIBfatLookup] = None
    skipped_reason:    Optional[str]        = None
    errors:            list[str]            = field(default_factory=list)

    def as_caption(self) -> str:
        """Compact one-liner suitable for ``st.caption``."""
        when = self.checked_at.strftime("%Y-%m-%d %H:%M")
        if self.errors:
            return f"⚠️ Auto-update at {when}: {self.errors[0]}"
        if self.skipped_reason:
            return f"✅ Auto-update at {when}: {self.skipped_reason}"
        if self.rows_inserted:
            tm = self.target_month.strftime("%b %Y") if self.target_month else "?"
            return (
                f"✅ Auto-update at {when}: inserted {self.rows_inserted} row(s) "
                f"for {tm}."
            )
        return f"✅ Auto-update at {when}: no change."


# ── Helpers ──────────────────────────────────────────────────────────────────

def _next_month(ts: pd.Timestamp) -> pd.Timestamp:
    """Return the first day of the month immediately after ``ts``."""
    return (ts.normalize().replace(day=1) + pd.DateOffset(months=1))


def _previous_calendar_month(today: datetime) -> tuple[int, int]:
    """Return (year, month) for ``today`` minus one calendar month."""
    if today.month == 1:
        return (today.year - 1, 12)
    return (today.year, today.month - 1)


def _derive_rows(
    *,
    target_month:        pd.Timestamp,
    advanced:            dict[str, float],
    class_ii_butterfat:  float,
) -> list[dict]:
    """Compute the four (Category, Class) rows per the spec.

    All inputs are pre-validated by the parsers (they raise ``ValueError`` on
    missing labels) so this is pure arithmetic — no IO, no error-handling
    needed beyond the formulas themselves.
    """
    htst_class_i_skim   = round(advanced["class_i_skim_raw"]    / 100, 4)
    htst_class_ii_skim  = round(advanced["class_ii_skim_raw"]   / 100, 4)
    htst_class_i_bfat   = round(advanced["advanced_butterfat"], 4)
    htst_class_ii_bfat  = round(class_ii_butterfat,             4)

    # Per spec: ONLY the ESL Class I Skim differs from HTST — every other
    # ESL field mirrors its HTST counterpart.
    esl_class_i_skim = round(
        htst_class_i_skim + advanced["class_i_esl_adj_raw"] / 100,
        4,
    )

    return [
        {
            store.COL_CATEGORY:  "HTST",
            store.COL_MONTH:     target_month,
            store.COL_CLASS:     "I",
            store.COL_SKIM:      htst_class_i_skim,
            store.COL_BUTTERFAT: htst_class_i_bfat,
        },
        {
            store.COL_CATEGORY:  "HTST",
            store.COL_MONTH:     target_month,
            store.COL_CLASS:     "II",
            store.COL_SKIM:      htst_class_ii_skim,
            store.COL_BUTTERFAT: htst_class_ii_bfat,
        },
        {
            store.COL_CATEGORY:  "ESL",
            store.COL_MONTH:     target_month,
            store.COL_CLASS:     "I",
            store.COL_SKIM:      esl_class_i_skim,
            store.COL_BUTTERFAT: htst_class_i_bfat,
        },
        {
            store.COL_CATEGORY:  "ESL",
            store.COL_MONTH:     target_month,
            store.COL_CLASS:     "II",
            store.COL_SKIM:      htst_class_ii_skim,
            store.COL_BUTTERFAT: htst_class_ii_bfat,
        },
    ]


# ── Public entry point ───────────────────────────────────────────────────────

def maybe_update_from_pdfs(
    *,
    force: bool                = False,
    now:   Optional[datetime]  = None,
    ttl:   timedelta           = _DEFAULT_CHECK_TTL,
) -> AutoUpdateResult:
    """Check the USDA PDFs and insert a new month's rows when warranted.

    Parameters
    ----------
    force
        Skip the TTL guard and force a fresh PDF check, even if we checked
        recently. Wired to the "Force refresh from USDA" button in the UI.
    now
        Override the current time (test seam — defaults to ``datetime.now``).
    ttl
        Minimum interval between PDF change-detection checks.

    Returns
    -------
    :class:`AutoUpdateResult`
        Always-populated; the UI uses it for the status caption regardless
        of whether anything was inserted.
    """
    now = now or datetime.now(timezone.utc)
    # Make sure the store has a baseline before we attempt any inserts:
    # without it the "next month" math has nothing to anchor on.
    store.seed_from_csv_if_empty()

    result = AutoUpdateResult(checked_at=now)

    # ── 1. TTL guard ────────────────────────────────────────────────────────
    state = store.get_pdf_state(pdf.ADVANCED_PRICES_URL)
    if not force and state and state.get("checked_at"):
        try:
            last = datetime.fromisoformat(state["checked_at"])
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            if now - last < ttl:
                result.skipped_reason = (
                    f"checked recently (TTL {int(ttl.total_seconds() // 60)} min) — "
                    f"latest known PDF last-changed {state.get('last_change_at') or 'never'}."
                )
                return result
        except ValueError:
            # Malformed timestamp — fall through and recheck.
            pass

    # ── 2. Change detection on the ADVANCED PRICES PDF ──────────────────────
    try:
        adv_changed, adv_fp = pdf.has_pdf_changed(pdf.ADVANCED_PRICES_URL, state)
    except Exception as exc:
        result.errors.append(f"USDA HEAD/GET failed: {exc}")
        return result

    result.advanced_changed = adv_changed

    # Always update the "checked at" timestamp, even when nothing changed —
    # otherwise the TTL guard never engages.
    try:
        store.upsert_pdf_state(
            pdf.ADVANCED_PRICES_URL,
            etag=adv_fp.etag,
            last_modified=adv_fp.last_modified,
            content_sha256=adv_fp.content_sha256 or (state or {}).get("content_sha256"),
            checked_at=now,
            last_change_at=now if adv_changed else None,
        )
    except Exception as exc:
        result.errors.append(f"Could not update PDF state cache: {exc}")
        # Non-fatal — we still try to ingest if the PDF actually changed.

    if not adv_changed:
        result.skipped_reason = "advanced-prices PDF unchanged."
        return result

    # ── 3. Determine target month & idempotency ─────────────────────────────
    latest = store.latest_month()
    if latest is None:
        # Empty store and no seed CSV — nothing to anchor on. Surface a
        # clear error rather than guessing.
        result.errors.append(
            "milk_mover_tracker is empty and no seed CSV is available; "
            "cannot determine the next month to insert."
        )
        return result

    target_month = _next_month(latest)
    result.target_month = target_month

    if store.has_rows_for_month(target_month):
        result.skipped_reason = (
            f"row(s) for {target_month.strftime('%b %Y')} already exist; "
            "auto-update is idempotent — nothing to do."
        )
        return result

    # ── 4. Pull both PDFs and parse ────────────────────────────────────────
    try:
        adv_bytes, _adv_fp_full = pdf.fetch_pdf_bytes(pdf.ADVANCED_PRICES_URL)
        advanced = pdf.parse_advanced_prices(adv_bytes)
    except Exception as exc:
        result.errors.append(f"advanced-prices PDF parse failed: {exc}")
        return result

    try:
        cls_bytes, _cls_fp = pdf.fetch_pdf_bytes(pdf.CLASS_PRICES_URL)
    except Exception as exc:
        result.errors.append(f"class-prices PDF download failed: {exc}")
        return result

    target_year, target_pdf_month = _previous_calendar_month(now)
    try:
        bfat_lookup = pdf.parse_class_ii_butterfat(
            cls_bytes,
            target_year=target_year,
            target_month=target_pdf_month,
        )
    except ValueError as exc:
        # Common case: the previous month's classprices hasn't been published
        # yet (USDA cadence). Fall back to the most recent month available
        # in the year-table so we don't block the auto-update.
        bfat_lookup = _latest_available_class_ii_bfat(cls_bytes, target_year)
        if bfat_lookup is None:
            result.errors.append(f"Class II Butterfat lookup failed: {exc}")
            return result

    result.classii_bfat_lookup = bfat_lookup

    # ── 5. Derive rows & insert ─────────────────────────────────────────────
    try:
        rows = _derive_rows(
            target_month=target_month,
            advanced=advanced,
            class_ii_butterfat=bfat_lookup.value,
        )
        result.rows_inserted = store.insert_rows(rows, source="auto-update")
    except Exception as exc:
        result.errors.append(f"insert failed: {exc}")
        return result

    return result


def _latest_available_class_ii_bfat(
    pdf_bytes: bytes,
    target_year: int,
) -> Optional[pdf.ClassIIBfatLookup]:
    """Fall-back lookup: the most recent month present in the year-table.

    USDA publishes class-prices around the 5th of the following month. If an
    advance-prices update lands between roughly the 22nd and the 5th, the
    "previous calendar month" row may not yet exist in class-prices — in
    that window we accept the latest available month rather than failing.
    """
    for month in range(12, 0, -1):
        try:
            return pdf.parse_class_ii_butterfat(
                pdf_bytes, target_year=target_year, target_month=month,
            )
        except ValueError:
            continue
    return None
