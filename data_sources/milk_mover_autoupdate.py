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
3. Parse the five advanced-prices fields (the four legacy ones plus the
   new ``Class II Nonfat Solids Price``) and the Class II Butterfat
   figure for ``(today.year, today.month - 1)`` from page 2 of the
   class-prices PDF.
4. Derive the five (Category, Class) rows per the Skim/Butterfat/Protein
   /Other-Solids formulas:

   =============== ==== ============================ ============================ ============================ ============================
   Category        Cl   Skim Rate                    Butterfat Rate               Protein Rate                 Other Solids Rate
   =============== ==== ============================ ============================ ============================ ============================
   HTST            I    Base Skim Class I / 100      Advanced Butterfat Factor    null                         null
   HTST            II   Class II Skim Price / 100    Class II Butterfat (p.2)     null                         null
   ESL             I    HTST Class I Skim            same as HTST Class I Bfat    null                         null
                        + Class I ESL Adj / 100
   ESL             II   same as HTST Class II Skim   same as HTST Class II Bfat   null                         null
   Cottage Cheese  II   null                         Class II Butterfat (p.2)     Class II Nonfat Solids       Class II Nonfat Solids
   =============== ==== ============================ ============================ ============================ ============================

   Note that the Cottage Cheese row carries the SAME Class II Nonfat
   Solids Price for both Protein Rate AND Other Solids Rate (per the
   May-2026 spec). The page-1 headline value is used as-is — no
   ``/100`` rescaling — because the source figure is already $/lb.

5. **One-shot Cottage Cheese backfill** — for every month already in
   the JSON that lacks a Cottage Cheese row, look up the historical
   Class II Butterfat (page 2 of ``dymclassprices.pdf``) and Class II
   Nonfat Solids Price (page 1 monthly tables of
   ``dymadvancedprices.pdf``). Months for which neither PDF exposes a
   value get a Cottage Cheese row with ``null`` rates so the month
   coverage of Cottage Cheese matches HTST/ESL exactly. Idempotent —
   subsequent invocations are an in-memory no-op once every month is
   filled in.

6. Append to the ``milk_mover_tracker`` JSON blob in OneLake (the store
   layer dedups by ``(Category, Month, Class)`` so re-runs are no-ops) and
   persist the updated PDF fingerprint state to the companion state blob.

The orchestrator is **safe to call on every page render** — the TTL guard
(see ``maybe_update_from_pdfs``) plus the change-detection layer make
repeated calls cheap (one in-memory check / no network) once we've checked
within the cooldown window.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

import pandas as pd

from data_sources import milk_mover_store as store
from data_sources import usda_milk_pdf as pdf


logger = logging.getLogger(__name__)


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
    # Number of Cottage Cheese rows inserted by the one-shot historical
    # backfill. Tracked separately from ``rows_inserted`` (which counts
    # the per-month new rows) so the status caption can call out a
    # multi-month backfill distinctly from the routine monthly tick.
    backfill_inserted: int                 = 0
    target_month:      Optional[pd.Timestamp] = None
    classii_bfat_lookup: Optional[pdf.ClassIIBfatLookup] = None
    skipped_reason:    Optional[str]        = None
    errors:            list[str]            = field(default_factory=list)

    def as_caption(self) -> str:
        """Compact one-liner suitable for ``st.caption``."""
        when = self.checked_at.strftime("%Y-%m-%d %H:%M")
        if self.errors:
            return f"⚠️ Auto-update at {when}: {self.errors[0]}"
        # Build the success caption from up to three independent
        # outcomes: monthly insert, backfill, and "no change". They're
        # composed so a single render can convey both a new-month tick
        # AND a same-call backfill (which is the expected experience on
        # the FIRST page render after the May-2026 schema bump).
        parts: list[str] = []
        if self.rows_inserted:
            tm = self.target_month.strftime("%b %Y") if self.target_month else "?"
            parts.append(
                f"inserted {self.rows_inserted} row(s) for {tm}"
            )
        if self.backfill_inserted:
            parts.append(
                f"backfilled {self.backfill_inserted} Cottage Cheese row(s)"
            )
        if self.skipped_reason and not parts:
            return f"✅ Auto-update at {when}: {self.skipped_reason}"
        if not parts:
            return f"✅ Auto-update at {when}: no change."
        return f"✅ Auto-update at {when}: " + "; ".join(parts) + "."


# ── Helpers ──────────────────────────────────────────────────────────────────

def _next_month(ts: pd.Timestamp) -> pd.Timestamp:
    """Return the first day of the month immediately after ``ts``."""
    return (ts.normalize().replace(day=1) + pd.DateOffset(months=1))


def _previous_calendar_month(today: datetime) -> tuple[int, int]:
    """Return (year, month) for ``today`` minus one calendar month."""
    if today.month == 1:
        return (today.year - 1, 12)
    return (today.year, today.month - 1)


# Canonical Cottage Cheese category label. Centralised so any future
# rename only touches one place; downstream UI / chart code matches
# case-insensitively but writes use this canonical spelling.
_CATEGORY_COTTAGE_CHEESE: str = "Cottage Cheese"


def _round_or_none(value: Optional[float], ndigits: int = 4) -> Optional[float]:
    """Round ``value`` when not None / NaN; otherwise propagate ``None``.

    Used by the backfill helper so per-month rows with one resolvable
    rate and one unresolvable rate produce ``{... : 0.5935, ... : None}``
    rather than collapsing the row entirely.
    """
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        return None
    return round(float(value), ndigits)


def _derive_rows(
    *,
    target_month:        pd.Timestamp,
    advanced:            dict[str, float],
    class_ii_butterfat:  float,
) -> list[dict]:
    """Compute the five (Category, Class) rows per the spec.

    All inputs are pre-validated by the parsers (they raise ``ValueError`` on
    missing labels) so this is pure arithmetic — no IO, no error-handling
    needed beyond the formulas themselves.

    The trailing Cottage Cheese row carries the Class II Butterfat (same
    figure used by the HTST/ESL Class II rows) plus the Class II Nonfat
    Solids Price duplicated into both Protein Rate AND Other Solids
    Rate. Its Skim Rate is intentionally ``None`` — Cottage Cheese has
    no skim component in the cost formula.
    """
    htst_class_i_skim   = round(advanced["class_i_skim_raw"]    / 100, 4)
    htst_class_ii_skim  = round(advanced["class_ii_skim_raw"]   / 100, 4)
    htst_class_i_bfat   = round(advanced["advanced_butterfat"], 4)
    htst_class_ii_bfat  = round(class_ii_butterfat,             4)
    cc_nonfat_solids    = round(advanced["class_ii_nonfat_solids"], 4)

    # Per spec: ONLY the ESL Class I Skim differs from HTST — every other
    # ESL field mirrors its HTST counterpart.
    esl_class_i_skim = round(
        htst_class_i_skim + advanced["class_i_esl_adj_raw"] / 100,
        4,
    )

    return [
        {
            store.COL_CATEGORY:     "HTST",
            store.COL_MONTH:        target_month,
            store.COL_CLASS:        "I",
            store.COL_SKIM:         htst_class_i_skim,
            store.COL_BUTTERFAT:    htst_class_i_bfat,
        },
        {
            store.COL_CATEGORY:     "HTST",
            store.COL_MONTH:        target_month,
            store.COL_CLASS:        "II",
            store.COL_SKIM:         htst_class_ii_skim,
            store.COL_BUTTERFAT:    htst_class_ii_bfat,
        },
        {
            store.COL_CATEGORY:     "ESL",
            store.COL_MONTH:        target_month,
            store.COL_CLASS:        "I",
            store.COL_SKIM:         esl_class_i_skim,
            store.COL_BUTTERFAT:    htst_class_i_bfat,
        },
        {
            store.COL_CATEGORY:     "ESL",
            store.COL_MONTH:        target_month,
            store.COL_CLASS:        "II",
            store.COL_SKIM:         htst_class_ii_skim,
            store.COL_BUTTERFAT:    htst_class_ii_bfat,
        },
        {
            store.COL_CATEGORY:     _CATEGORY_COTTAGE_CHEESE,
            store.COL_MONTH:        target_month,
            store.COL_CLASS:        "II",
            store.COL_SKIM:         None,
            store.COL_BUTTERFAT:    htst_class_ii_bfat,
            store.COL_PROTEIN:      cc_nonfat_solids,
            store.COL_OTHER_SOLIDS: cc_nonfat_solids,
        },
    ]


def _derive_cottage_cheese_backfill_rows(
    *,
    missing_months:    Iterable[pd.Timestamp],
    cls_pdf_bytes:     bytes,
    nfs_history:       dict[tuple[int, int], float],
) -> tuple[list[dict], list[tuple[pd.Timestamp, list[str]]]]:
    """Derive a Cottage Cheese row for every historical month that lacks one.

    Parameters
    ----------
    missing_months
        Months already present in ``fmmo_tracker.json`` for some
        (HTST/ESL) category but with no Cottage Cheese row yet. Sourced
        from :func:`store.months_missing_category`.
    cls_pdf_bytes
        Already-fetched ``dymclassprices.pdf`` body. We re-use it across
        every per-month lookup so the historical backfill performs
        exactly ONE HTTP GET on the class-prices PDF.
    nfs_history
        Pre-parsed ``{(year, month) → Class II Nonfat Solids Price}``
        from the page-1 monthly tables of ``dymadvancedprices.pdf``.

    Returns
    -------
    (rows, gaps)
        ``rows`` is the list of normalised dicts (one per month, even
        when both rates were unresolvable — ``None`` then represents
        ``null`` in the JSON). ``gaps`` is a debug list of
        ``(month, [missing_fields])`` tuples so the caller can emit a
        single concise log line summarising backfill coverage.
    """
    rows:   list[dict] = []
    gaps:   list[tuple[pd.Timestamp, list[str]]] = []

    for month in missing_months:
        ts = pd.Timestamp(month).normalize().replace(day=1)

        # Class II Butterfat for (year, month) from page 2. The parser
        # raises ValueError on a missing row; we treat that as "rate
        # unresolvable" rather than fatal so we still emit a Cottage
        # Cheese stub with null butterfat.
        try:
            bfat_lookup = pdf.parse_class_ii_butterfat(
                cls_pdf_bytes,
                target_year=ts.year,
                target_month=ts.month,
            )
            bfat_val: Optional[float] = float(bfat_lookup.value)
        except ValueError:
            bfat_val = None

        nfs_val = nfs_history.get((ts.year, ts.month))

        missing_fields: list[str] = []
        if bfat_val is None:
            missing_fields.append("Butterfat Rate")
        if nfs_val is None:
            missing_fields.append("Protein/Other Solids Rate")
        if missing_fields:
            gaps.append((ts, missing_fields))

        rows.append({
            store.COL_CATEGORY:     _CATEGORY_COTTAGE_CHEESE,
            store.COL_MONTH:        ts,
            store.COL_CLASS:        "II",
            store.COL_SKIM:         None,
            store.COL_BUTTERFAT:    _round_or_none(bfat_val),
            store.COL_PROTEIN:      _round_or_none(nfs_val),
            store.COL_OTHER_SOLIDS: _round_or_none(nfs_val),
        })

    return rows, gaps


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

    # ── 0. Cottage Cheese backfill gate (cheap, in-memory) ──────────────────
    #
    # The May-2026 schema bump introduced the Cottage Cheese category.
    # Any month already in the JSON without a Cottage Cheese row must be
    # backfilled — even when the PDFs haven't changed since the last
    # check. Detecting this is free (it iterates the cached row list),
    # so we evaluate it FIRST and use the result to decide whether to
    # bypass the TTL / change-detection guards below.
    try:
        missing_cc_months = store.months_missing_category(_CATEGORY_COTTAGE_CHEESE)
    except store.MilkMoverStoreError as exc:
        # Treat as no backfill needed — the normal new-month path below
        # will surface any persistent storage problem.
        logger.warning("Cottage Cheese backfill probe failed: %s", exc)
        missing_cc_months = []
    backfill_needed = bool(missing_cc_months)

    # ── 1. TTL guard ────────────────────────────────────────────────────────
    #
    # Skip the TTL short-circuit when a backfill is pending — otherwise
    # the page would render with incomplete Cottage Cheese coverage for
    # up to ``ttl`` after a deploy that introduced the schema. Once the
    # backfill completes, future renders see ``backfill_needed=False``
    # and the TTL guard resumes its normal role.
    state = store.get_pdf_state(pdf.ADVANCED_PRICES_URL)
    if not force and not backfill_needed and state and state.get("checked_at"):
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

    if not adv_changed and not backfill_needed:
        result.skipped_reason = "advanced-prices PDF unchanged."
        return result

    # ── 3. Determine target month & idempotency ─────────────────────────────
    #
    # ``target_month`` is only populated when the advanced-prices PDF
    # actually changed — i.e. a new month is being added. When we're in
    # this branch purely for backfill, target_month stays ``None`` and
    # the new-month derive/insert step below is skipped.
    target_month: Optional[pd.Timestamp] = None
    if adv_changed:
        latest = store.latest_month()
        if latest is None:
            # Empty store and no seed CSV — nothing to anchor on. Surface
            # a clear error rather than guessing.
            result.errors.append(
                "milk_mover_tracker is empty and no seed CSV is available; "
                "cannot determine the next month to insert."
            )
            return result

        candidate = _next_month(latest)
        if store.has_rows_for_month(candidate):
            # Idempotent — somebody else already inserted this month's
            # rows. Fall through to (possibly) run backfill but skip the
            # new-month derive step.
            target_month = None
        else:
            target_month = candidate
            result.target_month = candidate

    # Short-circuit when there is nothing left to do. This guards the
    # narrow case where ``adv_changed`` flipped True (so we passed the
    # early-return on line above), but the target month is already
    # populated AND no Cottage Cheese backfill is pending. Without this
    # we'd waste two HTTPS GETs on the PDFs for no insert.
    if target_month is None and not backfill_needed:
        result.skipped_reason = (
            f"row(s) for the next month already exist; "
            "auto-update is idempotent — nothing to do."
        )
        return result

    # ── 4. Pull both PDFs and parse ────────────────────────────────────────
    #
    # Done unconditionally for this branch: either we have a new month
    # to insert (need both PDFs) or we have a backfill to run (also
    # needs both — the advanced-prices page-1 monthly tables carry the
    # Class II Nonfat Solids history, and the class-prices page 2
    # carries the Class II Butterfat history).
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

    # ── 5a. New-month derive & insert ───────────────────────────────────────
    if target_month is not None:
        target_year, target_pdf_month = _previous_calendar_month(now)
        try:
            bfat_lookup = pdf.parse_class_ii_butterfat(
                cls_bytes,
                target_year=target_year,
                target_month=target_pdf_month,
            )
        except ValueError as exc:
            # Common case: the previous month's classprices hasn't been
            # published yet (USDA cadence). Fall back to the most recent
            # month available in the year-table so we don't block the
            # auto-update.
            bfat_lookup = _latest_available_class_ii_bfat(cls_bytes, target_year)
            if bfat_lookup is None:
                result.errors.append(f"Class II Butterfat lookup failed: {exc}")
                return result

        result.classii_bfat_lookup = bfat_lookup

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

    # ── 5b. Cottage Cheese backfill ─────────────────────────────────────────
    #
    # Idempotent and gated by the in-memory check at step 0. Once every
    # historical month has a Cottage Cheese row, ``missing_cc_months``
    # is empty here and we skip the work entirely.
    if backfill_needed:
        try:
            nfs_history = pdf.parse_class_ii_nonfat_solids_history(adv_bytes)
        except Exception as exc:
            # Never fatal — backfill operates on a best-effort basis.
            logger.warning("Nonfat Solids history parse failed: %s", exc)
            nfs_history = {}

        try:
            cc_rows, gaps = _derive_cottage_cheese_backfill_rows(
                missing_months=missing_cc_months,
                cls_pdf_bytes=cls_bytes,
                nfs_history=nfs_history,
            )
            if cc_rows:
                inserted = store.insert_rows(cc_rows, source="auto-update")
                result.backfill_inserted = inserted
                if gaps:
                    logger.info(
                        "Cottage Cheese backfill: %d row(s) inserted, "
                        "%d month(s) had partial PDF coverage: %s",
                        inserted,
                        len(gaps),
                        ", ".join(
                            f"{m:%Y-%m} (missing {', '.join(fields)})"
                            for m, fields in gaps[:5]
                        ) + ("…" if len(gaps) > 5 else ""),
                    )
        except Exception as exc:
            # Surface but never raise — partial success is acceptable.
            result.errors.append(f"Cottage Cheese backfill failed: {exc}")

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
