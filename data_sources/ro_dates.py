"""
Date parsing for the R&O stages — one parser, shared.

A Distribution Tracker exported from Excel spells dates three ways, often in
the same column:

* ``2027-01-01`` — ISO, what you get from a text-formatted cell;
* ``01/15/2027`` — US, what you get from a date-formatted cell;
* ``45641``      — the raw Excel **serial**: days since 1899-12-30 (that one is
  2024-12-15).  A CSV export of a date column that Excel considers numeric
  writes the serial, not the date.

:mod:`data_sources.ro_seed_pipeline` has always read all three, so a serial in
``First Ship Date`` is not a problem — it is the normal shape of the file.  The
upload validator, however, used a bare ``pd.to_datetime`` and therefore called
932 perfectly good rows unreadable and blocked the run.  That is the bug this
module exists to prevent: the pipeline and the validator must not disagree
about what a date is, so they now call the same function.

Pandas-only, no Fabric and no Streamlit, so the pure validator can import it.

The 1899-12-30 origin (not 1900-01-01) corrects for the Lotus 1-2-3 leap-year
bug Excel inherited and never fixed.
"""
from __future__ import annotations

import pandas as pd


#: Excel's day-zero.  Serial 1 is 1899-12-31, so day zero is 1899-12-30.
EXCEL_EPOCH: str = "1899-12-30"

#: Text that means "no value" in a CSV round-trip.
_NULL_TOKENS: list = ["nan", "NaN", "NaT", "None", "NULL", ""]

#: A pure integer (optionally with a ``.0`` tail) is an Excel serial.  Anchored
#: so ``"2027"`` alone is still treated as a serial — which is correct: as a
#: date it is meaningless, as a serial it is 1905-07-18, and either way the
#: pipeline has always read it that way.
_SERIAL_PATTERN: str = r"\d+(\.0+)?"


def canonical_date_series(series: pd.Series) -> pd.Series:
    """Parse mixed date representations to ``datetime64``; unparseable → ``NaT``.

    Handles ISO, US ``mm/dd/yyyy`` and Excel serials in the same column.  This
    is the single definition of "is this a date?" for the R&O stages —
    :func:`data_sources.ro_seed_pipeline._canon_date` delegates here, and the
    upload validator calls it so it can never flag a value the pipeline is
    about to read successfully.
    """
    if series.empty:
        return pd.Series(pd.NaT, index=series.index, dtype="datetime64[ns]")

    text = series.astype(str).str.strip().replace(_NULL_TOKENS, pd.NA)
    is_serial = text.str.fullmatch(_SERIAL_PATTERN, na=False)

    out = pd.Series(pd.NaT, index=text.index, dtype="datetime64[ns]")
    if is_serial.any():
        out.loc[is_serial] = pd.to_datetime(
            pd.to_numeric(text[is_serial]), origin=EXCEL_EPOCH, unit="D",
        )

    words = text[~is_serial]
    if not words.empty:
        # Fast path: pandas infers ONE format for the whole column.
        parsed = pd.to_datetime(words, errors="coerce")

        # A column that mixes formats — "2027-01-01" next to "01/15/2027",
        # which happens whenever some cells were typed and others formatted —
        # defeats that inference: pandas locks onto the first format it sees
        # and silently NaTs the rest.  Those rows then contribute nothing to
        # the in-year number while looking fine in the file.  Retry only the
        # leftovers per-element, so the common single-format column keeps the
        # vectorised path and a mixed one still parses completely.
        retry = parsed.isna() & words.notna()
        if retry.any():
            parsed.loc[retry] = pd.to_datetime(
                words[retry], errors="coerce", format="mixed",
            )
        out.loc[words.index] = parsed
    return out


def canonical_date_strings(series: pd.Series) -> pd.Series:
    """Canonical date → ``mm/dd/yyyy`` text, blank where unparseable."""
    return canonical_date_series(series).dt.strftime("%m/%d/%Y").fillna("")


def looks_like_excel_serial(value: object) -> bool:
    """True when *value* is the bare number Excel writes for a date.

    Used only to phrase the guidance: a serial needs no fixing, so the
    validator should never tell a planner to re-type one.
    """
    import re

    return bool(re.fullmatch(_SERIAL_PATTERN, str(value).strip()))


__all__ = [
    "EXCEL_EPOCH",
    "canonical_date_series",
    "canonical_date_strings",
    "looks_like_excel_serial",
]
