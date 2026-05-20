"""
Auto-update orchestrator for the Milk Mover Tracker.

Strategy
--------
The orchestrator runs a **reconciliation pass** against the page-1 history
tables of ``dymadvancedprices.pdf`` (and the page-2 history of
``dymclassprices.pdf``) on every detected change.  Each month present in
the advance-prices history is re-derived from its own labelled row in the
table — never from the page-1 headline — and the resulting five
``(Category, Class)`` rows are upserted into the
``fmmo_tracker.json`` blob.  ``store.upsert_rows`` overwrites existing
rate cells when the incoming value differs, so any prior wrong-month
write self-heals the moment USDA's PDF carries the corrected number.

Why a reconciliation pass instead of "insert next month"?
    The previous design assumed ``_next_month(latest_in_db)`` always
    matched the month USDA had announced on page 1.  That invariant
    broke whenever USDA edited the PDF for a non-data reason while the
    headline still referenced the previous month — the orchestrator
    then wrote stale headline values *under the wrong month* and the
    append-only ``insert_rows`` API made the bad data permanent.  The
    history table carries explicit ``(year, month)`` labels for every
    row, eliminating the entire class of label-drift bugs by
    construction.

Workflow on each invocation
---------------------------
1.  **Cottage Cheese backfill probe** (cheap, in-memory) — detect months
    in the JSON that lack a CC row.  Bypasses the TTL guard when set so
    a deploy-time schema bump is reflected on the next render.
2.  **Cottage Cheese skim/bfat in-place repair** (cheap, no PDF) — fix
    legacy CC rows whose Skim/Bfat are null by copying from ESL II for
    the same month (per the May-2026 contract: CC II Skim/Bfat mirror
    ESL II).
3.  **TTL guard** — skip the rest when we checked the PDF within
    ``_DEFAULT_CHECK_TTL`` AND there's nothing else to do.
4.  **Change detection** on the advanced-prices PDF (HEAD with ETag /
    Last-Modified; SHA-256 fallback).  Always persists the new
    fingerprint so the TTL math engages even on a "no change" tick.
5.  **Pull PDFs and parse** — both advance-prices and class-prices, all
    history tables in one shot.
6.  **Reconciliation upsert** — for every month in the advance-prices
    history, build the canonical row set
    (HTST I/II, ESL I/II, CC II) and upsert.  ``upsert_rows`` overwrites
    rate cells in place when the incoming value differs; ``None``
    incoming values preserve the stored value (so prior months' ESL I
    Skim is never clobbered — the ESL Adjustment is announced only for
    the upcoming month and only that month's ESL I Skim is reconcilable
    here).
7.  **Legacy Cottage Cheese backfill** — months in the JSON that
    predate the PDF's history window (typically ≥ 24 months old) are
    backfilled with CC II Skim/Bfat from the stored ESL II row and
    ``null`` Protein/Other Solids (no NFS source available for that
    vintage).  Months covered by step 6 are excluded by construction.

Row derivation matrix
---------------------

==============  ====  ============================  =============================  ===================
Category        Cl    Skim Rate                     Butterfat Rate                 Protein / Other
==============  ====  ============================  =============================  ===================
HTST            I     Base Skim Class I / 100        Advanced Butterfat Factor      null
HTST            II    Class II Skim Price / 100      Class II Butterfat (p.2)       null
ESL             I     HTST Class I Skim              same as HTST Class I Bfat      null
                      + Class I ESL Adj / 100 ¹
ESL             II    same as HTST Class II Skim     same as HTST Class II Bfat     null
Cottage Cheese  II    same as ESL II Skim ²          same as ESL II Bfat ²          Class II Nonfat Solids
==============  ====  ============================  =============================  ===================

¹ ESL Class I Skim is reconcilable ONLY for the announced month
  (the headline ESL Adjustment is not republished for any other month).
  For all other months the orchestrator emits ``Skim Rate=None`` so
  ``upsert_rows`` preserves whatever was correctly written at
  announcement time.

² CC II Skim/Bfat NEVER drift from ESL II — by binding them to the same
  derived values in one place, we make divergence structurally
  impossible (May-2026 contract).

The orchestrator is **safe to call on every page render** — the TTL guard
plus the change-detection layer make repeated calls cheap (one HEAD or
zero network at all) once we've checked within the cooldown window.
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


# Default minimum interval between PDF change-detection checks.  The HEAD
# call is cheap (~200 ms) but doing it on every Streamlit rerun would be
# wasteful; 1 hour balances freshness against bandwidth.
_DEFAULT_CHECK_TTL = timedelta(hours=1)


# Canonical Cottage Cheese category label.  Centralised so any future
# rename only touches one place; downstream UI / chart code matches
# case-insensitively but writes use this canonical spelling.
_CATEGORY_COTTAGE_CHEESE: str = "Cottage Cheese"


# ── Result type ──────────────────────────────────────────────────────────────

@dataclass
class AutoUpdateResult:
    """Structured result the UI can render as a status caption.

    All counts are mutually disjoint — a given row is counted in exactly
    one of ``rows_inserted`` / ``rows_updated`` / ``backfill_inserted``
    / ``cc_rows_repaired``.  ``as_caption`` composes a single-line
    summary from whichever counters are non-zero.
    """
    checked_at:        datetime
    advanced_changed:  bool                   = False
    # Rows the reconciliation pass added — i.e. new ``(Category, Month,
    # Class)`` keys that didn't exist in the JSON before this run.
    rows_inserted:     int                    = 0
    # Rows whose existing rate cells the reconciliation pass overwrote
    # because USDA published a corrected value.  These would have been
    # silently dropped by the old append-only ``insert_rows`` API.
    rows_updated:      int                    = 0
    # Cottage Cheese rows inserted by the legacy backfill (months older
    # than the PDF's history window).  Disjoint from ``rows_inserted``.
    backfill_inserted: int                    = 0
    # Cottage Cheese rows patched in-place by the one-shot CC-skim/bfat
    # repair pass (May-2026 contract: CC II skim/bfat mirror ESL II).
    cc_rows_repaired:  int                    = 0
    # The most-recent month USDA has announced on the advance-prices
    # PDF.  Used by the caption and surfaced to the UI so the operator
    # can sanity-check "the orchestrator saw USDA publish month X."
    target_month:      Optional[pd.Timestamp] = None
    skipped_reason:    Optional[str]          = None
    errors:            list[str]              = field(default_factory=list)

    def as_caption(self) -> str:
        """Compact one-liner suitable for ``st.caption``."""
        when = self.checked_at.strftime("%Y-%m-%d %H:%M")
        if self.errors:
            return f"⚠️ Auto-update at {when}: {self.errors[0]}"

        parts: list[str] = []
        tm = self.target_month.strftime("%b %Y") if self.target_month else "?"
        if self.rows_inserted:
            parts.append(f"inserted {self.rows_inserted} row(s) up to {tm}")
        if self.rows_updated:
            parts.append(f"corrected {self.rows_updated} row(s)")
        if self.backfill_inserted:
            parts.append(
                f"backfilled {self.backfill_inserted} Cottage Cheese row(s)"
            )
        if self.cc_rows_repaired:
            parts.append(
                f"repaired {self.cc_rows_repaired} Cottage Cheese skim/bfat row(s)"
            )
        if self.skipped_reason and not parts:
            return f"✅ Auto-update at {when}: {self.skipped_reason}"
        if not parts:
            return f"✅ Auto-update at {when}: no change."
        return f"✅ Auto-update at {when}: " + "; ".join(parts) + "."


# ── Pure helpers (no IO) ─────────────────────────────────────────────────────

def _round_or_none(value: Optional[float], ndigits: int = 4) -> Optional[float]:
    """Round ``value`` when not None / NaN; otherwise propagate ``None``.

    Used so partial rows (e.g. a month where the class-prices PDF lags
    and Class II Butterfat is unavailable) round-trip through
    ``upsert_rows`` as ``{... : 0.5935, ... : None}`` rather than
    collapsing the row to all-nulls.
    """
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        return None
    return round(float(value), ndigits)


def _derive_rows_for_sample(
    *,
    month:              pd.Timestamp,
    sample:             pdf.AdvancedPriceSample,
    class_ii_butterfat: Optional[float],
    esl_adj_raw:        Optional[float],
) -> list[dict]:
    """Build the canonical five-row set for one month of the page-1 history.

    Parameters
    ----------
    month
        First-of-month timestamp for the row group.
    sample
        Per-month sample from
        :func:`pdf.parse_advanced_prices_history`.
    class_ii_butterfat
        Class II Butterfat from the page-2 history of
        ``dymclassprices.pdf`` for the same month.  Pass ``None`` when
        the class-prices PDF lags (USDA cadence: class-prices publishes
        ~1 month behind advance-prices); HTST II / ESL II / CC II
        Butterfat then defaults to ``None`` and the store preserves
        whatever was previously written for those cells.
    esl_adj_raw
        Headline Class I ESL Adjustment ($/cwt, signed) from
        ``parse_advanced_prices``.  Pass ``None`` for every month
        OTHER than the announced month — USDA does not publish the
        ESL Adjustment per-month in any history table, so for
        non-announced months we propagate ``None`` for ESL Class I
        Skim and the store preserves the value written when that
        month was first announced.

    Returns
    -------
    Exactly five row dicts (HTST I, HTST II, ESL I, ESL II, CC II)
    in the schema the store expects.  Skim/Butterfat are bound through
    shared locals so ESL II and CC II can never drift from HTST II.
    """
    htst_class_i_skim   = round(sample.class_i_skim_raw    / 100, 4)
    htst_class_ii_skim  = round(sample.class_ii_skim_raw   / 100, 4)
    htst_class_i_bfat   = round(sample.advanced_butterfat,      4)
    htst_class_ii_bfat  = _round_or_none(class_ii_butterfat)
    cc_nonfat_solids    = round(sample.class_ii_nonfat_solids,  4)

    # ESL II / CC II skim/bfat mirror HTST II by spec — bind once.
    esl_class_ii_skim = htst_class_ii_skim
    esl_class_ii_bfat = htst_class_ii_bfat

    # ESL Class I Skim — only derive when this is the announced month;
    # otherwise propagate None so the upsert layer preserves the
    # correct value already in storage.
    esl_class_i_skim: Optional[float] = (
        round(htst_class_i_skim + esl_adj_raw / 100, 4)
        if esl_adj_raw is not None else None
    )

    return [
        {
            store.COL_CATEGORY:     "HTST",
            store.COL_MONTH:        month,
            store.COL_CLASS:        "I",
            store.COL_SKIM:         htst_class_i_skim,
            store.COL_BUTTERFAT:    htst_class_i_bfat,
        },
        {
            store.COL_CATEGORY:     "HTST",
            store.COL_MONTH:        month,
            store.COL_CLASS:        "II",
            store.COL_SKIM:         htst_class_ii_skim,
            store.COL_BUTTERFAT:    htst_class_ii_bfat,
        },
        {
            store.COL_CATEGORY:     "ESL",
            store.COL_MONTH:        month,
            store.COL_CLASS:        "I",
            store.COL_SKIM:         esl_class_i_skim,   # None → preserve stored
            store.COL_BUTTERFAT:    htst_class_i_bfat,
        },
        {
            store.COL_CATEGORY:     "ESL",
            store.COL_MONTH:        month,
            store.COL_CLASS:        "II",
            store.COL_SKIM:         esl_class_ii_skim,
            store.COL_BUTTERFAT:    esl_class_ii_bfat,
        },
        {
            store.COL_CATEGORY:     _CATEGORY_COTTAGE_CHEESE,
            store.COL_MONTH:        month,
            store.COL_CLASS:        "II",
            store.COL_SKIM:         esl_class_ii_skim,
            store.COL_BUTTERFAT:    esl_class_ii_bfat,
            store.COL_PROTEIN:      cc_nonfat_solids,
            store.COL_OTHER_SOLIDS: cc_nonfat_solids,
        },
    ]


def _derive_legacy_cottage_cheese_rows(
    *,
    legacy_months:   Iterable[pd.Timestamp],
    esl_ii_by_month: dict[pd.Timestamp, tuple[Optional[float], Optional[float]]],
) -> list[dict]:
    """Build CC rows for months OLDER than the page-1 history window.

    The reconciliation pass (see :func:`_derive_rows_for_sample`) handles
    every month USDA's current PDF exposes.  This helper handles the
    long tail of pre-history months in ``fmmo_tracker.json`` (typically
    from the seed CSV): CC II Skim/Bfat copy from the stored ESL II row
    and Protein/Other Solids are left ``null`` because no NFS source
    survives for that vintage.

    Idempotent — months that already have a CC row are excluded by the
    caller (see ``store.months_missing_category``).
    """
    rows: list[dict] = []
    for month in legacy_months:
        ts = pd.Timestamp(month).normalize().replace(day=1)
        skim_val, bfat_val = esl_ii_by_month.get(ts, (None, None))
        rows.append({
            store.COL_CATEGORY:     _CATEGORY_COTTAGE_CHEESE,
            store.COL_MONTH:        ts,
            store.COL_CLASS:        "II",
            store.COL_SKIM:         _round_or_none(skim_val),
            store.COL_BUTTERFAT:    _round_or_none(bfat_val),
            # NFS unavailable for legacy months — preserved as null so
            # the row's coverage parity (every month has a CC row) is
            # restored even when the source is incomplete.
            store.COL_PROTEIN:      None,
            store.COL_OTHER_SOLIDS: None,
        })
    return rows


# ── Public entry point ───────────────────────────────────────────────────────

def maybe_update_from_pdfs(
    *,
    force: bool               = False,
    now:   Optional[datetime] = None,
    ttl:   timedelta          = _DEFAULT_CHECK_TTL,
) -> AutoUpdateResult:
    """Check the USDA PDFs and reconcile ``fmmo_tracker.json`` accordingly.

    Parameters
    ----------
    force
        Skip the TTL guard and force a fresh PDF check, even if we
        checked recently.  Wired to the "Force refresh from USDA" button
        in the UI.
    now
        Override the current time (test seam — defaults to
        ``datetime.now(timezone.utc)``).
    ttl
        Minimum interval between PDF change-detection checks.

    Returns
    -------
    :class:`AutoUpdateResult`
        Always populated; the UI uses it for the status caption
        regardless of whether anything was inserted or updated.
    """
    now = now or datetime.now(timezone.utc)
    # Ensure the store has a baseline before we attempt any reconciliation:
    # without a seed, the legacy backfill path has nothing to anchor on.
    store.seed_from_csv_if_empty()

    result = AutoUpdateResult(checked_at=now)

    # ── 0a. Cottage Cheese backfill probe (cheap, in-memory) ────────────────
    #
    # Any month already in the JSON without a Cottage Cheese row needs
    # backfilling — even when the PDFs haven't changed since the last
    # check.  Detecting this is free (iterates the cached row list).
    try:
        missing_cc_months = store.months_missing_category(_CATEGORY_COTTAGE_CHEESE)
    except store.MilkMoverStoreError as exc:
        # Treat as no backfill needed — the normal reconciliation path
        # below will surface any persistent storage problem.
        logger.warning("Cottage Cheese backfill probe failed: %s", exc)
        missing_cc_months = []
    backfill_needed = bool(missing_cc_months)

    # ── 0b. Cottage Cheese skim/bfat repair probe (cheap, in-memory) ────────
    #
    # CC rows written before the "CC II skim/bfat mirror ESL II"
    # contract carry null Skim and need an in-place rewrite.
    try:
        cc_skim_repair_months = store.cottage_cheese_months_with_null_skim()
    except store.MilkMoverStoreError as exc:
        logger.warning("Cottage Cheese skim-repair probe failed: %s", exc)
        cc_skim_repair_months = []
    repair_needed = bool(cc_skim_repair_months)

    # ── 0c. Cottage Cheese skim/bfat in-place repair (cheap, no PDF) ────────
    #
    # Fire the repair pass BEFORE the TTL guard so a stale repair always
    # closes before the next routine tick.  Idempotent — once every CC
    # row has skim/bfat populated, this branch becomes a free in-memory
    # probe on every subsequent invocation.
    if repair_needed:
        try:
            esl_lookup = store.esl_class_ii_rates_by_month()
            patches: dict[pd.Timestamp, tuple[Optional[float], Optional[float]]] = {
                m: esl_lookup.get(m, (None, None))
                for m in cc_skim_repair_months
                if esl_lookup.get(m) is not None
            }
            # Only patch rows whose ESL II row exists with at least one
            # numeric value — otherwise we'd overwrite a null with a null.
            patches = {
                m: (skim, bfat)
                for m, (skim, bfat) in patches.items()
                if skim is not None or bfat is not None
            }
            if patches:
                result.cc_rows_repaired = store.patch_cottage_cheese_rates(patches)
        except store.MilkMoverStoreError as exc:
            # Non-fatal — surface and continue with the rest of the pipeline.
            result.errors.append(f"Cottage Cheese skim/bfat repair failed: {exc}")

    # ── 1. TTL guard ────────────────────────────────────────────────────────
    #
    # Skip the TTL short-circuit when a backfill is pending — otherwise
    # the page would render with incomplete Cottage Cheese coverage for
    # up to ``ttl`` after a deploy that introduced the schema.  Once the
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

    # Always update the "checked at" timestamp, even when nothing
    # changed — otherwise the TTL guard never engages.
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

    # ── 3. Pull PDFs ─────────────────────────────────────────────────────────
    #
    # Both PDFs are needed past this point.  The class-prices PDF lags
    # ~1 month behind advance-prices (USDA cadence), so any month
    # missing from the class-prices history is a tolerable gap — the
    # reconciliation simply leaves Class II Butterfat as ``None`` for
    # that month and the store preserves the previously-stored value.
    try:
        adv_bytes, _ = pdf.fetch_pdf_bytes(pdf.ADVANCED_PRICES_URL)
    except Exception as exc:
        result.errors.append(f"advanced-prices PDF download failed: {exc}")
        return result

    cls_bytes: Optional[bytes] = None
    try:
        cls_bytes, _ = pdf.fetch_pdf_bytes(pdf.CLASS_PRICES_URL)
    except Exception as exc:
        # Non-fatal: reconcile what we can from advance-prices alone.
        logger.warning("class-prices PDF download failed: %s", exc)

    # ── 4. Parse PDFs ────────────────────────────────────────────────────────
    try:
        advanced_history = pdf.parse_advanced_prices_history(adv_bytes)
    except Exception as exc:
        result.errors.append(f"advanced-prices history parse failed: {exc}")
        return result

    if not advanced_history:
        # PDF layout drift or empty body — refuse to write anything
        # rather than guess.  Surface so the UI banner is actionable.
        result.errors.append(
            "advanced-prices PDF history table is empty or unparseable; "
            "skipping reconciliation."
        )
        return result

    # Announced month = newest row of the page-1 history table.  This is
    # the structural fix for the historical "label drift" bug.
    announced_year_month = max(advanced_history.keys())
    result.target_month = pd.Timestamp(
        year=announced_year_month[0],
        month=announced_year_month[1],
        day=1,
    )

    # Headline values — used ONLY for the Class I ESL Adjustment of the
    # announced month.  The other four headline fields are also in the
    # history table (and we prefer that source) but we parse the
    # headline anyway so a label-rename in a future USDA layout drift
    # is surfaced as an error rather than silently producing a row
    # without ESL adjustment.
    try:
        advanced_headline = pdf.parse_advanced_prices(adv_bytes)
    except Exception as exc:
        result.errors.append(f"advanced-prices headline parse failed: {exc}")
        return result
    headline_esl_adj_raw = advanced_headline.get("class_i_esl_adj_raw")

    bfat_history: dict[tuple[int, int], float] = {}
    if cls_bytes is not None:
        try:
            bfat_history = pdf.parse_class_ii_butterfat_history(cls_bytes)
        except Exception as exc:
            # Non-fatal — reconciliation skips Class II Butterfat for
            # any missing month.
            logger.warning("class-prices history parse failed: %s", exc)

    # ── 5. Reconciliation upsert ─────────────────────────────────────────────
    #
    # Every month USDA exposes on page 1 gets re-derived from its own
    # explicitly-labelled row and upserted.  Existing rows whose values
    # match are no-ops; rows whose values differ are corrected in place;
    # rows that don't exist yet are inserted.  This is the loop that
    # heals the "June wrote May's value" bug on the next render after
    # USDA publishes the corrected PDF.
    reconciliation_rows: list[dict] = []
    for (year, month), sample in sorted(advanced_history.items()):
        month_ts    = pd.Timestamp(year=year, month=month, day=1)
        bfat_value  = bfat_history.get((year, month))
        is_announced = (year, month) == announced_year_month
        # Only the announced month receives an ESL Adjustment from the
        # headline; all other months propagate None and the upsert
        # preserves the ESL Class I Skim already in the store.
        esl_adj_for_month = headline_esl_adj_raw if is_announced else None
        reconciliation_rows.extend(_derive_rows_for_sample(
            month               = month_ts,
            sample              = sample,
            class_ii_butterfat  = bfat_value,
            esl_adj_raw         = esl_adj_for_month,
        ))

    try:
        inserted, updated = store.upsert_rows(
            reconciliation_rows, source="auto-update"
        )
        result.rows_inserted = inserted
        result.rows_updated  = updated
    except Exception as exc:
        result.errors.append(f"reconciliation upsert failed: {exc}")
        return result

    # ── 6. Legacy Cottage Cheese backfill ───────────────────────────────────
    #
    # The reconciliation pass above covers every month USDA's current
    # PDF exposes.  This pass handles the long tail: months in the JSON
    # that predate the page-1 history window.  Idempotent — once every
    # legacy month has a CC row, this branch is a no-op.
    if backfill_needed:
        history_months = {
            pd.Timestamp(year=y, month=m, day=1)
            for (y, m) in advanced_history.keys()
        }
        legacy_months = [
            m for m in missing_cc_months if m not in history_months
        ]
        if legacy_months:
            try:
                esl_ii_by_month = store.esl_class_ii_rates_by_month()
            except store.MilkMoverStoreError as exc:
                logger.warning("ESL II rate lookup failed: %s", exc)
                esl_ii_by_month = {}
            cc_rows = _derive_legacy_cottage_cheese_rows(
                legacy_months   = legacy_months,
                esl_ii_by_month = esl_ii_by_month,
            )
            if cc_rows:
                try:
                    inserted, _ = store.upsert_rows(
                        cc_rows, source="auto-update"
                    )
                    result.backfill_inserted = inserted
                except Exception as exc:
                    # Surface but never raise — partial success is acceptable.
                    result.errors.append(
                        f"Cottage Cheese backfill failed: {exc}"
                    )

    return result
