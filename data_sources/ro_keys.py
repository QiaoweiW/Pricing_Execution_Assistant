"""
Canonicalisation of the R&O business key — the single source of truth.

An R&O line is identified across every stage by
``(Format, Customer, Taxonomy, Brand, Item #)``.  The trouble is that the
stages do not spell those five values identically:

* ``RO_Seed.csv`` carries the **raw** Distribution Tracker text, so a private
  label row reads ``Brand = "PL"``.
* ``RO_Comparison_Output.csv`` is built through
  :mod:`data_sources.ro_comparison`, which canonicalises the brand on the way
  through, so the same row reads ``Brand = "Private"``.
* ``Item #`` arrives as an int (``58``), a float (``58.0`` whenever a NaN
  elsewhere coerces the column) or a string, depending on which file and which
  pandas read produced it.

Any comparison that hashes those values verbatim therefore reports the *same*
line as missing from both sides at once — a false divergence that looks
exactly like a real one.  That is the bug this module exists to prevent: the
canonicalisation lives here, once, and every stage that needs to match an R&O
line across files calls into it.

Pandas-free and dependency-free on purpose (plain scalars in, ``str`` out) so
the seed pipeline, the comparison builder and the reconciliation diagnostic
can all import it without pulling Streamlit or the Fabric client into a pure
computation.
"""
from __future__ import annotations

from typing import Any


#: The five columns that identify an R&O line, in the order every stage uses.
BUSINESS_KEY_COLS: tuple = ("Format", "Customer", "Taxonomy", "Brand", "Item #")

#: Spellings of "private label" seen across the Distribution Tracker exports.
#: Lower-cased for comparison; extend here and every stage picks it up.
PRIVATE_LABEL_TOKENS: frozenset = frozenset({
    "pl", "private label", "private-label", "privatelabel",
})

#: The spelling the report and every downstream consumer expect.
PRIVATE_LABEL: str = "Private"


def _is_blank(value: Any) -> bool:
    """True for ``None``, NaN, and whitespace-only text — without pandas.

    ``value != value`` is the NaN identity, which holds for ``float("nan")``
    and for ``numpy.nan`` alike, so blank-checking needs no pandas import.
    """
    if value is None:
        return True
    try:
        if value != value:                     # NaN
            return True
    except (TypeError, ValueError):            # exotic __eq__; treat as present
        return False
    return not str(value).strip()


def canonical_cell(value: Any) -> str:
    """Canonicalise one business-key cell for cross-file comparison.

    Strips whitespace, maps blanks to ``""``, and drops the trailing ``.0``
    from a float-that-is-an-integer so ``"58.0"`` matches ``58`` and ``"58"``.
    """
    if _is_blank(value):
        return ""
    text = str(value).strip()
    if text.endswith(".0") and text[:-2].lstrip("-").isdigit():
        return text[:-2]
    return text


def canonical_brand(value: Any) -> str:
    """Map every private-label spelling to :data:`PRIVATE_LABEL`.

    ``"PL"`` / ``"pl"`` / ``"Private Label"`` / ``"private-label"`` →
    ``"Private"``.  Blanks return ``""`` so callers can surface a
    "fill me in" warning for the affected rows rather than inventing a brand.
    Anything else passes through stripped, unchanged.
    """
    if _is_blank(value):
        return ""
    text = str(value).strip()
    if text.lower() in PRIVATE_LABEL_TOKENS:
        return PRIVATE_LABEL
    return text


def canonical_key_cell(column: str, value: Any) -> str:
    """Canonicalise *value* for its position in the business key.

    Brand goes through :func:`canonical_brand` because the stages disagree on
    how to spell private label; every other column just needs
    :func:`canonical_cell`.
    """
    if column == "Brand":
        return canonical_brand(value)
    return canonical_cell(value)


__all__ = [
    "BUSINESS_KEY_COLS",
    "PRIVATE_LABEL_TOKENS",
    "PRIVATE_LABEL",
    "canonical_cell",
    "canonical_brand",
    "canonical_key_cell",
]
