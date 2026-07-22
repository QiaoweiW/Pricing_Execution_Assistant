"""
Auto-update orchestrator for the Milk Mover Tracker.

Strategy
--------
When the user clicks **🔄 USDA refresh** (or a routine tick fires for
the first time in the session), the orchestrator scrapes the headline
values from ``dymadvancedprices.pdf`` and asks: **"is the PDF's page-1
banner announcing the month that is already the latest in the file?"**.
Only when the announced month equals ``max(file)`` do we ingest a new
month, labelled ``max(file) + 1 calendar month`` per the operator
contract.  Existing rows are NEVER mutated by this path (May-2026-late
contract: "no change to existing data").

Anchoring the write to the announced month IS the dedup guard: a month
is only appended when USDA has "caught up" to the file's latest month,
so the label counter cannot run past the real month (it will not write
September while the PDF still announces July) and an identical month
cannot be appended twice.

Class II Butterfat is published in a separate file (page-2 history of
``dymclassprices.pdf``) and lags advance-prices by ~1 month at the USDA
cadence.  When the lookup for ``target_month`` misses, we treat that
as a hard stop and surface an operator-actionable warning rather than
papering over the gap — the warning tells the user exactly where to
drop the value into the lakehouse manually.  This is intentional:
without Class II Bfat, the HTST II / ESL II / Culture II rows would
land with null Butterfat and corrupt the downstream HTST II / ESL II
/ Culture II calculations silently.

Workflow on each invocation
---------------------------
1.  **Culture backfill probe** (cheap, in-memory) — detect months in the
    JSON that lack a Culture row.  Bypasses the TTL guard when set so a
    deploy-time schema bump is reflected on the next render.
2.  **Culture skim/bfat in-place repair** (cheap, no PDF) — fix legacy
    Culture rows whose Skim/Bfat are null by copying from ESL II for the
    same month (per the May-2026 contract: Culture II Skim/Bfat mirror
    ESL II).
3.  **TTL guard** — skip the rest when we checked the PDF within
    ``_DEFAULT_CHECK_TTL`` AND there's nothing else to do.  Bypassed
    when ``force=True`` (explicit "USDA refresh" click).
4.  **Change detection** on the advanced-prices PDF (HEAD with ETag /
    Last-Modified; SHA-256 fallback).  Always persists the new
    fingerprint so the TTL math engages even on a "no change" tick.
    The fingerprint is a ROUTINE-tick optimisation only — on
    ``force=True`` we ALWAYS proceed to step 5 even when the
    fingerprint says "unchanged", because the comparator (step 5) is
    the authoritative gate.  This avoids the failure mode where a
    prior tick persisted a new fingerprint but didn't actually land
    the data (bfat lag, transient error, legacy-buggy write), which
    used to leave the file stale forever even after the operator
    explicitly clicked refresh.
5.  **New-month gate (rewired).**  Append the next calendar month
    (``max(file) + 1``) ONLY when the advanced-prices PDF page-1
    banner announces the month that is already the latest in the file
    (e.g. latest = Jul 2026 AND banner = "ADVANCED PRICES FOR JULY
    2026" -> write Aug 2026).  A different or unreadable announced
    month writes nothing.  This is the hardened dedup guard: it stops
    the label counter from running past the real month and cannot be
    bypassed by a missing latest-month lookup.  Class II Butterfat
    never gates the write (sourced as the latest page-2 row, step 6).
6.  **Source Class II Butterfat** by taking the LATEST published row
    from :func:`pdf.parse_class_ii_butterfat_history` regardless of
    target month — the operator-facing contract is "Class II
    butterfat is always the last row value on page 2 of
    dymclassprices.pdf".  Decouples the orchestrator from the
    class-prices cadence lag entirely.  Defensive fallback: when the
    class-prices PDF is unreachable or unparseable the bfat cell
    writes as ``None`` and a warning is surfaced so the operator
    can fill it in manually.
7.  **Insert** one new five-row month at ``target_month`` using
    :func:`store.insert_rows` — append-only by construction; existing
    rows are not touched.
8.  **Legacy Culture backfill** — months in the JSON without a Culture
    row are backfilled with Culture II Skim/Bfat copied from the
    stored ESL II row.  Protein/Other Solids fall back to ``null`` for
    the pre-NFS vintage; the just-ingested ``target_month`` is
    excluded by construction (it already carries a Culture row from
    step 7).

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
Culture         II    same as ESL II Skim ¹          same as ESL II Bfat ¹          Class II Nonfat Solids
==============  ====  ============================  =============================  ===================

¹ Culture II Skim/Bfat NEVER drift from ESL II — by binding them to
  the same derived values in one place, we make divergence
  structurally impossible (May-2026 contract).

The orchestrator is **safe to call on every page render** — the TTL guard
plus the change-detection layer make repeated calls cheap (one HEAD or
zero network at all) once we've checked within the cooldown window.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Iterable, Optional

import pandas as pd

from data_sources import milk_mover_store as store
from data_sources import usda_milk_pdf as pdf


logger = logging.getLogger(__name__)


# Default minimum interval between PDF change-detection checks.  The HEAD
# call is cheap (~200 ms) but doing it on every Streamlit rerun would be
# wasteful; 1 hour balances freshness against bandwidth.
_DEFAULT_CHECK_TTL = timedelta(hours=1)


# Canonical "Culture" category label.  Centralised so any future rename
# only touches one place; downstream UI / chart code matches
# case-insensitively but writes use this canonical spelling.  The
# May-2026-late operator update renamed the legacy "Cottage Cheese"
# label across the milk usage stable file and ``fmmo_tracker.json``,
# and the symbol was renamed to match in the follow-on cleanup once
# the lakehouse JSON was confirmed clean.
_CATEGORY_CULTURE: str = "Culture"


# ── Result type ──────────────────────────────────────────────────────────────

@dataclass
class AutoUpdateResult:
    """Structured result the UI can render as a status caption.

    All counts are mutually disjoint — a given row is counted in exactly
    one of ``rows_inserted`` / ``rows_updated`` / ``backfill_inserted``
    / ``culture_rows_repaired``.  ``as_caption`` composes a single-line
    summary from whichever counters are non-zero.
    """
    checked_at:        datetime
    advanced_changed:  bool                   = False
    # Rows the ingest appended — i.e. new ``(Category, Month, Class)``
    # keys that didn't exist in the JSON before this run.  Under the
    # May-2026-late "no change to existing data" contract this is 5
    # on a successful new-month tick and 0 on every other path
    # (rates unchanged, bfat lag, error).
    rows_inserted:     int                    = 0
    # Culture rows inserted by the legacy backfill (months that predate
    # the NFS-publication window).  Disjoint from ``rows_inserted``.
    backfill_inserted: int                    = 0
    # Culture rows patched in-place by the one-shot Culture-skim/bfat
    # repair pass (May-2026 contract: Culture II skim/bfat mirror ESL
    # II).
    culture_rows_repaired:  int               = 0
    # The month label the orchestrator chose for this tick, derived as
    # ``store.latest_month() + 1`` per the operator-facing contract.
    # Surfaced to the UI so the operator can sanity-check the labelling.
    target_month:      Optional[pd.Timestamp] = None
    # Non-empty when the class-prices PDF was unreachable or unparseable,
    # so the Class II Butterfat cell on the new month landed as ``None``.
    # The orchestrator STILL inserts the new-month row (per the
    # May-2026-late contract: "only the advance-prices PDF publish
    # should trigger / block new-month writes"); the warning is surfaced
    # so the operator knows to fill the missing Bfat cell into the
    # lakehouse manually.  The string is the already-formatted
    # "Month YYYY" label of ``target_month``.
    class_ii_bfat_lag_target: Optional[str]   = None
    skipped_reason:    Optional[str]          = None
    errors:            list[str]              = field(default_factory=list)

    def as_caption(self) -> str:
        """Compact one-liner suitable for ``st.caption``.

        See also :attr:`is_warning` — pages should escalate the
        caption to ``st.warning`` when that returns True so the
        operator sees the missing-bfat call-out as a banner instead
        of small grey text.
        """
        when = self.checked_at.strftime("%Y-%m-%d %H:%M")
        if self.errors:
            return f"⚠️ Auto-update at {when}: {self.errors[0]}"

        parts: list[str] = []
        tm = self.target_month.strftime("%b %Y") if self.target_month else "?"
        if self.rows_inserted:
            parts.append(f"appended {self.rows_inserted} row(s) for {tm}")
        if self.backfill_inserted:
            parts.append(
                f"backfilled {self.backfill_inserted} Culture row(s)"
            )
        if self.culture_rows_repaired:
            parts.append(
                f"repaired {self.culture_rows_repaired} Culture skim/bfat row(s)"
            )

        # Class II Bfat lag is now a SOFT warning — the new-month row
        # is still written with Bfat=None, the operator just needs to
        # fill in the cell when class-prices catches up.  Append the
        # call-out to the normal status line so the operator sees
        # both "what we wrote" and "what still needs filling".
        if self.class_ii_bfat_lag_target:
            parts.append(
                f"⚠️ Class II Butterfat for {self.class_ii_bfat_lag_target} "
                "wrote as NULL — fill the cell into `fmmo_tracker.json` "
                "manually when class-prices PDF publishes"
            )

        if self.skipped_reason and not parts:
            return f"✅ Auto-update at {when}: {self.skipped_reason}"
        if not parts:
            return f"✅ Auto-update at {when}: no change."
        prefix = "⚠️" if self.is_warning else "✅"
        return f"{prefix} Auto-update at {when}: " + "; ".join(parts) + "."

    @property
    def is_warning(self) -> bool:
        """True when callers should render the caption as ``st.warning``.

        Used by the page so the bfat-lag soft warning gets the amber
        banner treatment instead of being buried in a small caption.
        """
        return bool(self.errors) or bool(self.class_ii_bfat_lag_target)


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
        advance-prices); HTST II / ESL II / Culture II Butterfat then write
        as ``None`` and the store preserves whatever was previously
        written for those cells on a re-tick.

    Returns
    -------
    Exactly five row dicts (HTST I, HTST II, ESL I, ESL II, Culture II)
    in the schema :func:`store.upsert_rows` expects.  Skim/Butterfat
    are bound through shared locals so ESL II and Culture II can never
    drift from HTST II.
    """
    htst_class_i_skim   = round(headline["class_i_skim_raw"]    / 100, 4)
    htst_class_ii_skim  = round(headline["class_ii_skim_raw"]   / 100, 4)
    htst_class_i_bfat   = round(headline["advanced_butterfat"],       4)
    htst_class_ii_bfat  = _round_or_none(class_ii_butterfat)
    cc_nonfat_solids    = round(headline["class_ii_nonfat_solids"],   4)

    # ESL II / Culture II skim/bfat mirror HTST II by spec — bind once.
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
            store.COL_CATEGORY:     _CATEGORY_CULTURE,
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


def _new_month_skip_reason(
    file_max:        Optional[pd.Timestamp],
    announced_month: Optional[date],
) -> Optional[str]:
    """Return why a new month must NOT be written, or ``None`` when it should.

    Rewired new-month gate: we only append ``file_max + 1`` when the
    advanced-prices PDF's page-1 banner is announcing the month that is already
    the latest in the file (``announced_month == file_max``).  This ties each
    appended month to a genuine USDA publication and is the hardened dedup guard
    — the label counter can't run past the real month, and an unreadable /
    mismatched banner writes nothing.  ``file_max is None`` (empty pre-seed
    file) returns ``None`` so the bootstrap write can proceed.
    """
    if file_max is None:
        return None
    if announced_month is None:
        return (
            "Could not read the announced month from the advanced-prices "
            "PDF banner - no new month written."
        )
    if (announced_month.year, announced_month.month) != (
        file_max.year, file_max.month
    ):
        return (
            f"Advanced-prices PDF announces {announced_month:%b %Y}, but "
            f"the latest stored month is {file_max:%b %Y} - no new month "
            "written (only advance when the two match)."
        )
    return None


def _derive_legacy_culture_rows(
    *,
    legacy_months:   Iterable[pd.Timestamp],
    esl_ii_by_month: dict[pd.Timestamp, tuple[Optional[float], Optional[float]]],
) -> list[dict]:
    """Build Culture rows for months OLDER than the page-1 history window.

    The reconciliation pass (see :func:`_derive_rows_for_target`) handles
    every month USDA's current PDF exposes.  This helper handles the
    long tail of pre-history months in ``fmmo_tracker.json`` (typically
    from the seed CSV): Culture II Skim/Bfat copy from the stored ESL II
    row and Protein/Other Solids are left ``null`` because no NFS source
    survives for that vintage.

    Idempotent — months that already have a Culture row are excluded by
    the caller (see ``store.months_missing_category``).
    """
    rows: list[dict] = []
    for month in legacy_months:
        ts = pd.Timestamp(month).normalize().replace(day=1)
        skim_val, bfat_val = esl_ii_by_month.get(ts, (None, None))
        rows.append({
            store.COL_CATEGORY:     _CATEGORY_CULTURE,
            store.COL_MONTH:        ts,
            store.COL_CLASS:        "II",
            store.COL_SKIM:         _round_or_none(skim_val),
            store.COL_BUTTERFAT:    _round_or_none(bfat_val),
            # NFS unavailable for legacy months — preserved as null so
            # the row's coverage parity (every month has a Culture
            # row) is restored even when the source is incomplete.
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

    # ── 0a. Culture backfill probe (cheap, in-memory) ───────────────────────
    #
    # Any month already in the JSON without a Culture row needs
    # backfilling — even when the PDFs haven't changed since the last
    # check.  Detecting this is free (iterates the cached row list).
    try:
        missing_culture_months = store.months_missing_category(_CATEGORY_CULTURE)
    except store.MilkMoverStoreError as exc:
        # Treat as no backfill needed — the main ingest below will
        # surface any persistent storage problem.
        logger.warning("Culture backfill probe failed: %s", exc)
        missing_culture_months = []
    backfill_needed = bool(missing_culture_months)

    # ── 0b. Culture skim/bfat repair probe (cheap, in-memory) ───────────────
    #
    # Culture rows written before the "Culture II skim/bfat mirror ESL
    # II" contract carry null Skim and need an in-place rewrite.
    try:
        culture_skim_repair_months = store.culture_months_with_null_skim()
    except store.MilkMoverStoreError as exc:
        logger.warning("Culture skim-repair probe failed: %s", exc)
        culture_skim_repair_months = []
    repair_needed = bool(culture_skim_repair_months)

    # ── 0c. Culture skim/bfat in-place repair (cheap, no PDF) ───────────────
    #
    # Fire the repair pass BEFORE the TTL guard so a stale repair always
    # closes before the next routine tick.  Idempotent — once every
    # Culture row has skim/bfat populated, this branch becomes a free
    # in-memory probe on every subsequent invocation.
    if repair_needed:
        try:
            esl_lookup = store.esl_class_ii_rates_by_month()
            patches: dict[pd.Timestamp, tuple[Optional[float], Optional[float]]] = {
                m: esl_lookup.get(m, (None, None))
                for m in culture_skim_repair_months
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
                result.culture_rows_repaired = store.patch_culture_rates(patches)
        except store.MilkMoverStoreError as exc:
            # Non-fatal — surface and continue with the rest of the pipeline.
            result.errors.append(f"Culture skim/bfat repair failed: {exc}")

    # ── 1. TTL guard ────────────────────────────────────────────────────────
    #
    # Skip the TTL short-circuit when a backfill is pending — otherwise
    # the page would render with incomplete Culture coverage for up to
    # ``ttl`` after a deploy that introduced the schema.  Once the
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

    # The HTTP fingerprint (ETag/Last-Modified/sha) is a routine-tick
    # optimisation, NOT a write-gating signal.  ``force=True`` is the
    # operator's explicit "USDA refresh" click — they want a fresh
    # answer from the comparator regardless of whatever fingerprint
    # we cached on a prior tick.  Without this carve-out, a prior tick
    # that persisted the new fingerprint but didn't land the new data
    # (legacy buggy paths, transient errors, prior bfat lag) would
    # leave the file stale FOREVER, because every subsequent
    # fingerprint check returns "unchanged".
    if not force and not adv_changed and not backfill_needed:
        result.skipped_reason = (
            "advanced-prices PDF unchanged (HTTP fingerprint cache hit)."
        )
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
    # Both PDFs are needed past this point.  Class II Bfat is published
    # on a separate file that lags advance-prices by ~1 month at USDA's
    # cadence; the lookup-miss case is handled explicitly below so the
    # operator can react.
    try:
        adv_bytes, _ = pdf.fetch_pdf_bytes(pdf.ADVANCED_PRICES_URL)
    except Exception as exc:
        result.errors.append(f"advanced-prices PDF download failed: {exc}")
        return result

    cls_bytes: Optional[bytes] = None
    try:
        cls_bytes, _ = pdf.fetch_pdf_bytes(pdf.CLASS_PRICES_URL)
    except Exception as exc:
        # Non-fatal at fetch time — we still parse advance-prices and
        # apply the bfat-lag warning below if the lookup is absent.
        logger.warning("class-prices PDF download failed: %s", exc)

    # ── 5. Parse headline values ────────────────────────────────────────────
    try:
        advanced_headline = pdf.parse_advanced_prices(adv_bytes)
    except Exception as exc:
        result.errors.append(f"advanced-prices headline parse failed: {exc}")
        return result

    # Class II Butterfat sourcing — per operator contract: "Class II
    # butterfat is always the last row value on page 2 of
    # dymclassprices.pdf".  We take the LATEST published row from the
    # parsed history regardless of which month it carries.  This
    # decouples the orchestrator from the class-prices cadence lag
    # entirely: even when the new-target month hasn't been published
    # yet on the class-prices PDF, we still emit a row carrying the
    # most-recent published Bfat — which is exactly what the operator
    # wants since Class II Bfat does NOT trigger new-month writes
    # (it never gates the new-month write - see step 5).
    #
    # Defensive fallback: if the class-prices PDF is unreachable or
    # unparseable we land here with ``class_ii_butterfat = None``.  The
    # candidate row writes with Bfat = None and the page surfaces a
    # warning so the operator can fill the cell into the lakehouse
    # manually.  This path is rare (class-prices is a stable USDA
    # publication) but matters for resilience.
    class_ii_butterfat: Optional[float] = None
    if cls_bytes is not None:
        try:
            bfat_history = pdf.parse_class_ii_butterfat_history(cls_bytes)
        except Exception as exc:
            # Non-fatal parse error — fall through to the None
            # fallback below, which surfaces the warning to the page.
            logger.warning("class-prices history parse failed: %s", exc)
            bfat_history = {}
        if bfat_history:
            latest_key = max(bfat_history.keys())
            class_ii_butterfat = bfat_history.get(latest_key)

    # When the class-prices PDF wasn't reachable / parseable we still
    # proceed with bfat=None.  The cell will land as ``None`` in the
    # JSON; surface the actionable warning so the operator knows to
    # fill it in.  The label points at ``target_month`` so the operator
    # knows exactly which row needs the manual fill.
    if class_ii_butterfat is None:
        result.class_ii_bfat_lag_target = target_month.strftime("%b %Y")

    # ── 6. New-month gate ─────────────────────────────────────────────────
    #
    # Rewired rule: append the NEXT calendar month (target_month =
    # file_max + 1) ONLY when the advanced-prices PDF page-1 banner is
    # announcing the month that is already the latest in the file.  Both
    # must line up - e.g. latest stored month = Jul 2026 AND the banner
    # says "ADVANCED PRICES FOR JULY 2026" -> write Aug 2026.  This ties
    # every appended month to a genuine USDA publication and is the
    # hardened dedup guard: once Aug is written, file_max = Aug while the
    # PDF still announces Jul, so Sep is NOT written until USDA publishes
    # August's advance prices.
    try:
        candidate_rows = _derive_rows_for_target(
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

    announced_month = pdf.parse_advanced_prices_month(adv_bytes)
    skip_reason = _new_month_skip_reason(file_max, announced_month)
    if skip_reason:
        result.skipped_reason = skip_reason
        target_rows_to_insert: list[dict] = []
    else:
        target_rows_to_insert = candidate_rows

    # ── 7. Append the new-month rows (existing rows untouched) ─────────────
    #
    # The May-2026-late contract is "no change to existing data".  By
    # construction ``target_month = store.latest_month() + 1`` — a fresh
    # key set, so ``upsert_rows`` only ever inserts here.  We surface the
    # ``inserted`` count and IGNORE ``updated`` — a non-zero ``updated``
    # would mean the comparator gate let an already-stored month through
    # (the surrounding tests guarantee that can't happen).
    if target_rows_to_insert:
        try:
            inserted, _updated_unused = store.upsert_rows(
                target_rows_to_insert, source="auto-update",
            )
            result.rows_inserted = inserted
        except Exception as exc:
            result.errors.append(f"target-month insert failed: {exc}")
            return result

    # ── 8. Legacy Culture backfill ──────────────────────────────────────────
    #
    # Months in the JSON without a Culture row are backfilled from the
    # stored ESL II row (Culture II skim/bfat mirror ESL II by spec).
    # ``target_month`` already carries a Culture row from step 6 so it is
    # excluded by construction.  Idempotent — once every legacy month
    # has a Culture row, this branch is a no-op.
    if backfill_needed:
        legacy_months = [
            m for m in missing_culture_months
            if pd.Timestamp(m).normalize().replace(day=1) != target_month
        ]
        if legacy_months:
            try:
                esl_ii_by_month = store.esl_class_ii_rates_by_month()
            except store.MilkMoverStoreError as exc:
                logger.warning("ESL II rate lookup failed: %s", exc)
                esl_ii_by_month = {}
            culture_rows = _derive_legacy_culture_rows(
                legacy_months   = legacy_months,
                esl_ii_by_month = esl_ii_by_month,
            )
            if culture_rows:
                try:
                    inserted, _ = store.upsert_rows(
                        culture_rows, source="auto-update",
                    )
                    result.backfill_inserted = inserted
                except Exception as exc:
                    # Surface but never raise — partial success is acceptable.
                    result.errors.append(
                        f"Culture backfill failed: {exc}"
                    )

    return result
