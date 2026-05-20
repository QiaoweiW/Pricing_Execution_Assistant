"""
Auto-update orchestrator for the Milk Mover Tracker.

Strategy
--------
When the advance-prices PDF changes, scrape the headline values once and
write them under a **single new month** whose label is derived purely
from the persisted file's state:

    target_month = store.latest_month() + 1 calendar month

This is the operator-facing contract — "Once the pdf is updated, the
system scrapes data from that pdf and labels the month = existing month
in the file + 1, always first-of-month."  The labeling is intentionally
decoupled from any text-mined month-name on the PDF so a USDA layout
shift (e.g. footnote re-numbering, year-header rephrasing) cannot
mis-label a row.  ``store.upsert_rows`` keeps the write idempotent — a
duplicate Refresh-tick for the same already-published month is a no-op,
and a corrected re-publish of the headline for the same month lands in
the same row.

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
5.  **Single-month ingest** — derive ``target_month`` from the file's
    current max plus one calendar month, scrape the headline values
    from advance-prices, look up Class II Butterfat for ``target_month``
    in the page-2 class-prices history, build the canonical
    ``(Category, Class)`` row set and upsert it.
6.  **Legacy Cottage Cheese backfill** — months in the JSON without a
    CC row are backfilled with CC II Skim/Bfat copied from the stored
    ESL II row.  Protein/Other Solids fall back to ``null`` for the
    pre-NFS vintage; the just-ingested ``target_month`` is excluded by
    construction (it already carries a CC row from step 5).

Row derivation matrix
---------------------

==============  ====  ============================  =============================  ===================
Category        Cl    Skim Rate                     Butterfat Rate                 Protein / Other
==============  ====  ============================  =============================  ===================
HTST            I     Base Skim Class I / 100        Advanced Butterfat Factor      null
HTST            II    Class II Skim Price / 100      Class II Butterfat (p.2)       null
ESL             I     HTST Class I Skim              same as HTST Class I Bfat      null
                      + Class I ESL Adj / 100
ESL             II    same as HTST Class II Skim     same as HTST Class II Bfat     null
Cottage Cheese  II    same as ESL II Skim ¹          same as ESL II Bfat ¹          Class II Nonfat Solids
==============  ====  ============================  =============================  ===================

¹ CC II Skim/Bfat NEVER drift from ESL II — by binding them to the same
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
    # Rows the ingest added — i.e. new ``(Category, Month, Class)``
    # keys that didn't exist in the JSON before this run.  Under the
    # ``max(file) + 1`` contract this is 5 on a clean tick (or 0 when
    # the upsert is a no-op for a re-issued unchanged PDF).
    rows_inserted:     int                    = 0
    # Rows whose existing rate cells the upsert overwrote because USDA
    # re-published the headline for ``target_month`` with a corrected
    # number.  Idempotent on repeated unchanged ticks.
    rows_updated:      int                    = 0
    # Cottage Cheese rows inserted by the legacy backfill (months that
    # predate the NFS-publication window).  Disjoint from
    # ``rows_inserted``.
    backfill_inserted: int                    = 0
    # Cottage Cheese rows patched in-place by the one-shot CC-skim/bfat
    # repair pass (May-2026 contract: CC II skim/bfat mirror ESL II).
    cc_rows_repaired:  int                    = 0
    # The month label the orchestrator chose for this tick, derived as
    # ``store.latest_month() + 1`` per the operator-facing contract.
    # Surfaced to the UI so the operator can sanity-check the labelling.
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


def _derive_rows_for_target(
    *,
    month:              pd.Timestamp,
    headline:           dict[str, float],
    class_ii_butterfat: Optional[float],
) -> list[dict]:
    """Build the canonical five-row set for ``month`` from headline values.

    All five rows share the same first-of-month ``month`` label; the
    label is supplied by the caller (typically ``store.latest_month() +
    1 calendar month``) and is INTENTIONALLY decoupled from any
    text-mined month name on the PDF.

    Parameters
    ----------
    month
        First-of-month timestamp the row set should carry.  All five
        rows are written under this exact label.
    headline
        The dict returned by :func:`pdf.parse_advanced_prices`.  Must
        contain every key the function documents — KeyError if any
        is missing, which surfaces cleanly to the orchestrator.
    class_ii_butterfat
        Class II Butterfat for ``month``, looked up from page-2 of
        ``dymclassprices.pdf``.  Pass ``None`` when the class-prices
        PDF lags (USDA cadence: class-prices publishes ~1 month behind
        advance-prices); HTST II / ESL II / CC II Butterfat then write
        as ``None`` and the store preserves whatever was previously
        written for those cells on a re-tick.

    Returns
    -------
    Exactly five row dicts (HTST I, HTST II, ESL I, ESL II, CC II)
    in the schema :func:`store.upsert_rows` expects.  Skim/Butterfat
    are bound through shared locals so ESL II and CC II can never
    drift from HTST II.
    """
    htst_class_i_skim   = round(headline["class_i_skim_raw"]    / 100, 4)
    htst_class_ii_skim  = round(headline["class_ii_skim_raw"]   / 100, 4)
    htst_class_i_bfat   = round(headline["advanced_butterfat"],       4)
    htst_class_ii_bfat  = _round_or_none(class_ii_butterfat)
    cc_nonfat_solids    = round(headline["class_ii_nonfat_solids"],   4)

    # ESL II / CC II skim/bfat mirror HTST II by spec — bind once.
    esl_class_ii_skim = htst_class_ii_skim
    esl_class_ii_bfat = htst_class_ii_bfat

    # ESL Class I Skim = HTST I Skim + ESL Adjustment ($/cwt → $/lb).
    # The headline parser raises if the label is missing, so by the
    # time we get here the adjustment is guaranteed to be present.
    esl_class_i_skim = round(
        htst_class_i_skim + headline["class_i_esl_adj_raw"] / 100, 4
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
            store.COL_SKIM:         esl_class_i_skim,
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


def _next_calendar_month(month: pd.Timestamp) -> pd.Timestamp:
    """Return ``month + 1`` calendar month, normalised to first-of-month.

    Pure helper so the "target_month = file_max + 1" derivation lives in
    one place and the orchestrator stays declarative.
    """
    return (
        pd.Timestamp(month).normalize().replace(day=1)
        + pd.DateOffset(months=1)
    )


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
    """Check the USDA PDFs and ingest one new month into ``fmmo_tracker.json``.

    On every detected advance-prices PDF change the orchestrator picks
    ``target_month = store.latest_month() + 1 calendar month`` (the
    operator-facing labelling contract), scrapes the headline values
    once, and upserts the canonical five-row set under that label.

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
    # Ensure the store has a baseline before we attempt to ingest:
    # without a seed, ``store.latest_month()`` would be None and we'd
    # fall back to today's calendar month + 1 (defensive only).
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
        # Treat as no backfill needed — the main ingest below will
        # surface any persistent storage problem.
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

    # ── 3. Derive target_month from FILE STATE ──────────────────────────────
    #
    # Operator-facing contract: "label the month = existing month in the
    # file + 1".  We read the persisted max BEFORE any write so a
    # re-tick on the same PDF doesn't drift the target forward.
    file_max = store.latest_month()
    if file_max is None:
        # Defensive fallback — should be unreachable after
        # ``store.seed_from_csv_if_empty()`` above.  Anchor on today's
        # calendar month + 1 so the orchestrator can still make
        # forward progress without a seed.
        from datetime import date as _date
        target_month = _next_calendar_month(
            pd.Timestamp(_date.today().replace(day=1))
        )
    else:
        target_month = _next_calendar_month(file_max)
    result.target_month = target_month

    # ── 4. Pull PDFs ─────────────────────────────────────────────────────────
    #
    # Both PDFs are needed past this point.  The class-prices PDF lags
    # ~1 month behind advance-prices (USDA cadence), so a missing
    # ``target_month`` lookup is a tolerable gap — Class II Butterfat
    # falls back to ``None`` and the store preserves whatever was
    # previously written for that cell on a re-tick.
    try:
        adv_bytes, _ = pdf.fetch_pdf_bytes(pdf.ADVANCED_PRICES_URL)
    except Exception as exc:
        result.errors.append(f"advanced-prices PDF download failed: {exc}")
        return result

    cls_bytes: Optional[bytes] = None
    try:
        cls_bytes, _ = pdf.fetch_pdf_bytes(pdf.CLASS_PRICES_URL)
    except Exception as exc:
        # Non-fatal: ingest what we can from advance-prices alone.
        logger.warning("class-prices PDF download failed: %s", exc)

    # ── 5. Parse headline values ────────────────────────────────────────────
    try:
        advanced_headline = pdf.parse_advanced_prices(adv_bytes)
    except Exception as exc:
        result.errors.append(f"advanced-prices headline parse failed: {exc}")
        return result

    # Class II Butterfat is a page-2 lookup on the class-prices PDF.
    # The lookup key is structural ``(year, month)`` of ``target_month``;
    # USDA's table-row month label is the authoritative source for THIS
    # one field — without it we have no way to associate Bfat with a
    # specific month, since the class-prices PDF has no headline block
    # the way advance-prices does.
    class_ii_butterfat: Optional[float] = None
    if cls_bytes is not None:
        try:
            bfat_history = pdf.parse_class_ii_butterfat_history(cls_bytes)
            class_ii_butterfat = bfat_history.get(
                (target_month.year, target_month.month)
            )
        except Exception as exc:
            # Non-fatal — preserve any value the store already holds.
            logger.warning("class-prices history parse failed: %s", exc)

    # ── 6. Build the canonical row set and upsert ───────────────────────────
    #
    # The upsert is idempotent: a re-tick on the same PDF that doesn't
    # mutate any cell is a clean no-op (zero inserts, zero updates),
    # and a corrected re-publish of the headline for ``target_month``
    # lands in the SAME row by ``(Category, Month, Class)`` key.
    try:
        target_rows = _derive_rows_for_target(
            month               = target_month,
            headline            = advanced_headline,
            class_ii_butterfat  = class_ii_butterfat,
        )
    except KeyError as exc:
        result.errors.append(
            f"advanced-prices headline is missing required label {exc!s}; "
            "skipping write."
        )
        return result

    try:
        inserted, updated = store.upsert_rows(
            target_rows, source="auto-update",
        )
        result.rows_inserted = inserted
        result.rows_updated  = updated
    except Exception as exc:
        result.errors.append(f"target-month upsert failed: {exc}")
        return result

    # ── 7. Legacy Cottage Cheese backfill ───────────────────────────────────
    #
    # Months in the JSON without a CC row are backfilled from the
    # stored ESL II row (CC II skim/bfat mirror ESL II by spec).
    # ``target_month`` already carries a CC row from step 6 so it is
    # excluded by construction.  Idempotent — once every legacy month
    # has a CC row, this branch is a no-op.
    if backfill_needed:
        legacy_months = [
            m for m in missing_cc_months
            if pd.Timestamp(m).normalize().replace(day=1) != target_month
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
                        cc_rows, source="auto-update",
                    )
                    result.backfill_inserted = inserted
                except Exception as exc:
                    # Surface but never raise — partial success is acceptable.
                    result.errors.append(
                        f"Cottage Cheese backfill failed: {exc}"
                    )

    return result
