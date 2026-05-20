"""
Auto-update orchestrator for the Milk Mover Tracker.

Strategy
--------
When the user clicks **🔄 USDA refresh** (or a routine tick fires for
the first time in the session), the orchestrator scrapes the headline
values from ``dymadvancedprices.pdf`` and asks: **"do these rates
actually differ from the latest month already in the file?"**.  Only
when at least one of the seven canonical reconcilable cells differs do
we ingest a new month — labelled ``max(file) + 1 calendar month`` per
the operator contract.  Existing rows are NEVER mutated by this path
(May-2026-late contract: "no change to existing data").

The seven canonical cells live in
:data:`_RECONCILABLE_FIELDS` and cover HTST I Skim/Bfat, HTST II
Skim/Bfat, ESL I Skim, CC II Protein, CC II Other Solids.  Every other
cell of the 5-row month group is *derived* from those seven by spec
(ESL II / CC II Skim+Bfat mirror HTST II, etc.), so "any of the seven
differ" is the necessary and sufficient signal that USDA actually
published new rates.

Class II Butterfat is published in a separate file (page-2 history of
``dymclassprices.pdf``) and lags advance-prices by ~1 month at the USDA
cadence.  When the lookup for ``target_month`` misses, we treat that
as a hard stop and surface an operator-actionable warning rather than
papering over the gap — the warning tells the user exactly where to
drop the value into the lakehouse manually.  This is intentional:
without Class II Bfat, the HTST II / ESL II / CC II rows would land
with null Butterfat and corrupt the downstream HTST II / ESL II / CC
II calculations silently.

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
5.  **Compare scraped rates vs the latest stored month.**  Identical →
    no write, surface "rates unchanged" reason.  Differ on at least
    one of the seven canonical cells → proceed.  Class II Bfat lookup
    missing for ``target_month`` → surface
    ``class_ii_bfat_lag_target`` so the page renders an actionable
    warning; no write.
6.  **Insert** one new five-row month at ``target_month`` using
    :func:`store.insert_rows` — append-only by construction; existing
    rows are not touched.
7.  **Legacy Cottage Cheese backfill** — months in the JSON without a
    CC row are backfilled with CC II Skim/Bfat copied from the stored
    ESL II row.  Protein/Other Solids fall back to ``null`` for the
    pre-NFS vintage; the just-ingested ``target_month`` is excluded by
    construction (it already carries a CC row from step 6).

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
    # Rows the ingest appended — i.e. new ``(Category, Month, Class)``
    # keys that didn't exist in the JSON before this run.  Under the
    # May-2026-late "no change to existing data" contract this is 5
    # on a successful new-month tick and 0 on every other path
    # (rates unchanged, bfat lag, error).
    rows_inserted:     int                    = 0
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
    # Non-empty when the Class II Butterfat lookup for ``target_month``
    # came back missing (page-2 of ``dymclassprices.pdf`` lags advance-
    # prices by ~1 month at the USDA cadence).  When set, the
    # orchestrator REFUSES to insert the new month — the page surfaces
    # the warning so the operator can fill the missing cell into the
    # lakehouse manually, then re-Refresh.  The string is the
    # already-formatted "Month YYYY" label of ``target_month``.
    class_ii_bfat_lag_target: Optional[str]   = None
    skipped_reason:    Optional[str]          = None
    errors:            list[str]              = field(default_factory=list)

    def as_caption(self) -> str:
        """Compact one-liner suitable for ``st.caption``."""
        when = self.checked_at.strftime("%Y-%m-%d %H:%M")
        if self.errors:
            return f"⚠️ Auto-update at {when}: {self.errors[0]}"

        # Class II Bfat lag is a hard-stop warning that supersedes the
        # normal status line — the operator has to act on it.
        if self.class_ii_bfat_lag_target:
            return (
                f"⚠️ Auto-update at {when}: USDA Class II Butterfat for "
                f"{self.class_ii_bfat_lag_target} is not yet published "
                "on dymclassprices.pdf — fill the row into "
                "`fmmo_tracker.json` manually, then click USDA refresh."
            )

        parts: list[str] = []
        tm = self.target_month.strftime("%b %Y") if self.target_month else "?"
        if self.rows_inserted:
            parts.append(f"appended {self.rows_inserted} row(s) for {tm}")
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


# ── Compare-vs-latest gate ───────────────────────────────────────────────────
#
# The seven reconcilable cells the comparator looks at.  Every other
# cell of a 5-row month group is derived from these by spec:
#
#   * ESL II Skim/Bfat       = HTST II Skim/Bfat
#   * CC II  Skim/Bfat       = ESL II Skim/Bfat (= HTST II)
#   * ESL I  Bfat            = HTST I  Bfat
#
# So "any of the seven differ" is necessary AND sufficient to conclude
# that USDA has published new rates for the announced month.  Adding
# the derived cells would just be redundant comparisons.
@dataclass(frozen=True)
class _CellSpec:
    """Locate one reconcilable rate cell by ``(Category, Class, field)``."""
    category: str
    klass:    str
    field:    str   # ``store.COL_SKIM`` / ``store.COL_BUTTERFAT`` / etc.


_RECONCILABLE_FIELDS: tuple[_CellSpec, ...] = (
    _CellSpec("HTST",                   "I",  store.COL_SKIM),
    _CellSpec("HTST",                   "I",  store.COL_BUTTERFAT),
    _CellSpec("HTST",                   "II", store.COL_SKIM),
    _CellSpec("HTST",                   "II", store.COL_BUTTERFAT),
    _CellSpec("ESL",                    "I",  store.COL_SKIM),
    _CellSpec(_CATEGORY_COTTAGE_CHEESE, "II", store.COL_PROTEIN),
    _CellSpec(_CATEGORY_COTTAGE_CHEESE, "II", store.COL_OTHER_SOLIDS),
)


def _index_rows_by_key(
    rows: Iterable[dict],
) -> dict[tuple[str, str], dict]:
    """Build a ``(Category, Class) → row`` lookup, casefolded keys."""
    out: dict[tuple[str, str], dict] = {}
    for r in rows:
        cat = str(r.get(store.COL_CATEGORY, "")).strip().casefold()
        cls = str(r.get(store.COL_CLASS, "")).strip().casefold()
        if cat and cls:
            out[(cat, cls)] = r
    return out


def _row_for_spec(
    index: dict[tuple[str, str], dict],
    spec:  _CellSpec,
) -> Optional[dict]:
    """Look up the row matching ``spec`` (case-insensitive Category/Class)."""
    return index.get((spec.category.casefold(), spec.klass.casefold()))


def _cell_value(row: Optional[dict], field: str) -> Optional[float]:
    """Coerce ``row[field]`` to a finite float, or return ``None``."""
    if row is None:
        return None
    v = row.get(field)
    if v is None:
        return None
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _values_differ(
    a: Optional[float],
    b: Optional[float],
    *,
    rel_tol: float = 1e-9,
    abs_tol: float = 1e-6,
) -> bool:
    """Return True when ``a`` and ``b`` represent materially different rates.

    Treats ``None`` ≠ float as a difference (one side has a published
    rate, the other doesn't), but ``None`` vs ``None`` as equal.  Float
    comparison uses a tight ``math.isclose`` tolerance: USDA publishes
    to 4 decimal places, so a single trailing-digit jitter wouldn't
    survive ``round(..., 4)`` and we shouldn't trigger a new-month
    write on representational noise.
    """
    if a is None and b is None:
        return False
    if a is None or b is None:
        return True
    import math
    return not math.isclose(a, b, rel_tol=rel_tol, abs_tol=abs_tol)


def _scraped_rates_differ_from_latest(
    candidate_rows:     list[dict],
    latest_month_rows:  list[dict],
) -> bool:
    """Compare the seven canonical cells between candidate and stored rows.

    ``candidate_rows`` and ``latest_month_rows`` must each be the
    five-row group for a single month.  Returns ``True`` as soon as
    any of the cells in :data:`_RECONCILABLE_FIELDS` differs.
    Short-circuits — every other cell is bound to one of these seven
    by spec, so a single difference is sufficient to publish.
    """
    cand_index = _index_rows_by_key(candidate_rows)
    prev_index = _index_rows_by_key(latest_month_rows)
    for spec in _RECONCILABLE_FIELDS:
        a = _cell_value(_row_for_spec(cand_index, spec), spec.field)
        b = _cell_value(_row_for_spec(prev_index, spec), spec.field)
        if _values_differ(a, b):
            return True
    return False


def _latest_month_row_group(latest_month: pd.Timestamp) -> list[dict]:
    """Return every row of ``latest_month`` from the store as plain dicts.

    Used by the comparator to read the seven canonical cells without
    forcing a DataFrame round-trip.  Empty list when the store has no
    row for ``latest_month`` (e.g. cold start before the seed lands).

    Matches the store's ``M/D/YYYY`` (un-zero-padded) Month format —
    that's the canonical shape ``read_milk_mover_df`` returns and
    sidesteps Windows' missing ``%-m`` strftime directive.
    """
    df = store.read_milk_mover_df()
    if df.empty or store.COL_MONTH not in df.columns:
        return []
    target_str = f"{latest_month.month}/{latest_month.day}/{latest_month.year}"
    mask = df[store.COL_MONTH].astype(str).str.strip() == target_str
    return df.loc[mask].to_dict(orient="records")


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

    # Class II Butterfat is a page-2 lookup on the class-prices PDF.
    # The lookup key is structural ``(year, month)`` of ``target_month``;
    # USDA's table-row month label is the authoritative source for THIS
    # one field — without it we have no way to associate Bfat with a
    # specific month, since the class-prices PDF has no headline block
    # the way advance-prices does.  A miss surfaces the bfat-lag warning
    # and stops the new-month insert dead — we refuse to write a row
    # with null Bfat by spec.
    class_ii_butterfat: Optional[float] = None
    if cls_bytes is not None:
        try:
            bfat_history = pdf.parse_class_ii_butterfat_history(cls_bytes)
            class_ii_butterfat = bfat_history.get(
                (target_month.year, target_month.month)
            )
        except Exception as exc:
            # Non-fatal parse error — fall through to the lag handler.
            logger.warning("class-prices history parse failed: %s", exc)

    if class_ii_butterfat is None:
        result.class_ii_bfat_lag_target = target_month.strftime("%b %Y")
        return result

    # ── 6. Compare scraped rates vs the latest stored month ─────────────────
    #
    # Build the candidate 5-row set first (headline-driven), then read
    # the equivalent five rows for the latest month already on disk and
    # ask: "do any of the seven canonical cells differ?"  Identical →
    # we do NOT write — that's exactly the May-2026-late contract:
    # "if rates are different, then those rates should be written".
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

    if file_max is not None:
        latest_rows = _latest_month_row_group(file_max)
        if latest_rows and not _scraped_rates_differ_from_latest(
            candidate_rows, latest_rows,
        ):
            result.skipped_reason = (
                f"USDA rates unchanged vs {file_max.strftime('%b %Y')} — "
                "no new month written."
            )
            # Fall through to the legacy CC backfill below; we still want
            # to close gaps in the existing data even on a no-op tick.
            target_rows_to_insert: list[dict] = []
        else:
            target_rows_to_insert = candidate_rows
    else:
        # Empty file (no seed) — every rate is "different from null".
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

    # ── 8. Legacy Cottage Cheese backfill ───────────────────────────────────
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
