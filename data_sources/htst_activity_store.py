"""
OneLake-backed store for the 9 HTST Activity Quote CSVs.

Replaces the per-render multi-file upload on the New Price Quote page.
The CSVs now live in Fabric at ``Files/Activity_Model/<filename>.csv``
in the same Pricing_Lakehouse as the milk-mover blobs; on each render
the page reads all 9 files from Fabric, writes them to a temp directory
(so the existing ``processing/new_pricing_processor.py`` script keeps
working unchanged), and runs the processor to rebuild the parquet.

Public API
----------
    EXPECTED_FILES                                 — tuple[ActivityFileSpec, …]
    bootstrap_from_local_if_empty(local_dir)       -> dict[name, written_bool]
    fabric_etags()                                 -> dict[name, etag|None]
    read_raw_bytes(name)                           -> bytes      (for download buttons)
    materialise_to_dir(target_dir)                 -> dict[name, Path]
    write_csv_bytes(name, raw_bytes)               -> str   (new ETag; validates schema)
    get_store_label()                              -> str

Schema validation
-----------------
``write_csv_bytes`` validates uploads against
``ActivityFileSpec.expected_columns``. The expected tuples preserve
historical artefacts — leading/trailing spaces in
``" Class I Location & Plant Fees ($/Gal)"``,
``" Base Milk Cost per Gallon "``, etc. — because
``processing/new_pricing_processor.py`` reads by literal string and
would silently break on a "cleaned" column name.

To stay friendly to operators who round-trip the CSVs through Excel
(which strips trailing whitespace), validation is **tolerant**: a
column set whose stripped names equal the stripped expected names is
accepted, AND the stored copy is then re-serialised with the
canonical (space-bearing) names so the processor never sees a
stripped header. Reordered columns are also accepted; the canonical
write order is restored on save. Genuinely-different uploads (extra
columns, missing columns, typos) still raise
:class:`ActivityModelSchemaError`.

Concurrency model
-----------------
Each blob is written via ``fabric_lakehouse_io.write_bytes`` with the
ETag we last read for that file. Concurrent writers see
``ResourceModifiedError`` and the user is asked to retry. We don't do
automatic retry here because the user's upload action is interactive
and a manual click is the right "merge resolution."
"""
from __future__ import annotations

import io
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import pandas as pd
import streamlit as st

from data_sources import fabric_lakehouse_io as _io


# How long the ETag-listing cache lives. Keeps successive renders of the
# New Price Quote page from hammering OneLake on every navigation while
# still picking up out-of-band edits within ~60 s. Writes through this
# module invalidate the cache immediately via ``_invalidate_etag_cache``,
# so user-driven uploads see the new ETag without waiting for the TTL.
_ETAG_CACHE_TTL_SECONDS: int = 60


logger = logging.getLogger(__name__)


# ── Public errors ────────────────────────────────────────────────────────────

class ActivityModelStoreError(RuntimeError):
    """Raised on configuration / auth / I/O failure for the Activity_Model store."""


class ActivityModelSchemaError(ValueError):
    """Raised when an upload's CSV columns don't match the expected schema.

    Distinct exception type so the page UI can render a friendly
    "schema mismatch" banner without conflating it with OneLake outages.
    """


# ── Constants ────────────────────────────────────────────────────────────────

# Same lakehouse as milk_mover by default — see Q2 in the migration plan.
# Override by adding a [fabric_activity_model] block to secrets.toml that
# sets workspace/lakehouse to a different lakehouse.
_SECRETS_SECTION: str = "fabric_activity_model"

# Folder under Files/ where every Activity_Model CSV lives.
_FOLDER: str = "Activity_Model"


# ── File spec table ──────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ActivityFileSpec:
    """Static metadata for one of the 9 HTST Activity Quote CSVs.

    Captured here (not in ``new_price_quote_view.py``) so the
    canonical column contract for each file lives in the data layer.

    Attributes
    ----------
    filename
        Exact filename in the local repo and in OneLake. Both must match.
    expected_columns
        Exact column-name list from the canonical CSV. Matched
        case-sensitively and INCLUDING any historical leading/trailing
        spaces — see the module docstring for why.
    description
        Short human-readable description, used in tooltips on the
        download/upload UI.
    """
    filename: str
    expected_columns: tuple[str, ...]
    description: str

    @property
    def blob_path(self) -> str:
        """Lakehouse Files/ blob path for this file."""
        return f"{_FOLDER}/{self.filename}"


# Column lists captured from the canonical local CSVs. DO NOT prettify
# the leading/trailing spaces — they're load-bearing for the processor.
EXPECTED_FILES: tuple[ActivityFileSpec, ...] = (
    ActivityFileSpec(
        filename="Custom Label_Volume Bracket_Fee.csv",
        expected_columns=("Custom Label Bracket (Gal/Yr)", "Custom Label Fee ($/Gal)"),
        description="Custom-label volume → fee per gallon.",
    ),
    ActivityFileSpec(
        filename="Delivery_Miles Tier_Drop Size Tier_Fee.csv",
        expected_columns=(
            "Mileage Fee Tier (Mi)",
            "Drop Fee Tier (lbs/Drop Size)",
            " Delivery Charge ($/Gal) ",
        ),
        description="Delivery mileage × drop-size → fee per gallon.",
    ),
    ActivityFileSpec(
        filename="Pallet_Fee.csv",
        expected_columns=("Pallet", "Mixed Pallet Fee ($/Gal)"),
        description="Pallet type → mixed-pallet fee per gallon.",
    ),
    ActivityFileSpec(
        filename="Plant_Class_Plant Fees.csv",
        expected_columns=(
            "Plant",
            "Market Index Name",
            " Class I Location & Plant Fees ($/Gal)",
        ),
        description="Plant → Class I location & plant fees per gallon.",
    ),
    ActivityFileSpec(
        filename="Product_Class_Plant.csv",
        expected_columns=(
            "Item",
            "Item Description",
            "Item Category",
            "Market Index Name",
            "Plant",
        ),
        description="Product master: item → class / market / plant.",
    ),
    ActivityFileSpec(
        filename="Product_Milk Base Cost.csv",
        expected_columns=(
            "Item",
            "Item Description",
            " Base Milk Cost per Gallon ",
            "Month",
            "Source",
        ),
        description=(
            "Per-item base milk cost, by month. Auto-updated on the "
            "1st of each month from base_milk_cost_monthly_tracker."
        ),
    ),
    ActivityFileSpec(
        filename="Product_Processing_Pkg_Ing.csv",
        expected_columns=(
            "Item",
            "Item Description",
            "Total Processing ($/Gal)",
            "Packaging ($/Gal)",
            "Ingredients ($/Gal)",
        ),
        description="Per-item processing / packaging / ingredient costs.",
    ),
    ActivityFileSpec(
        filename="Product_UOM.csv",
        expected_columns=(
            "Item",
            "Item Description",
            "CA per EA", "CA per ST", "CA per PL", "CA per BC", "CA per BG",
            "Eaches per Case", "Gallons per Each", "Gallons per Case",
        ),
        description="Per-item unit-of-measure conversions.",
    ),
    ActivityFileSpec(
        filename="Sell-to_Volume Bracket_Fee.csv",
        expected_columns=("Sell-to Volume Bracket", "Sell-to Volume Fee ($/Gal)"),
        description="Sell-to volume bracket → fee per gallon.",
    ),
)

# Index lookup by filename — every public function takes filenames as
# strings (matching the actual filename in OneLake) so the page UI can
# enumerate ``EXPECTED_FILES`` and pass spec.filename verbatim.
_BY_NAME: dict[str, ActivityFileSpec] = {s.filename: s for s in EXPECTED_FILES}


# ── Internal helpers ─────────────────────────────────────────────────────────

def _spec(name: str) -> ActivityFileSpec:
    """Return the spec for ``name`` or raise with a precise list of valid names."""
    spec = _BY_NAME.get(name)
    if spec is None:
        raise ActivityModelStoreError(
            f"Unknown Activity_Model file: {name!r}. "
            f"Expected one of: {sorted(_BY_NAME.keys())}."
        )
    return spec


def _stripped(name: object) -> str:
    """Return ``str(name).strip()`` — the canonical lookup key for tolerant validation."""
    return str(name).strip()


def _normalise_to_canonical(spec: ActivityFileSpec, raw_bytes: bytes) -> bytes:
    """Validate + re-emit ``raw_bytes`` with canonical column names and order.

    Tolerant on input:
      * Trailing/leading whitespace differences (Excel round-trips strip them).
      * Column reordering.

    Strict on the column SET — extra columns or missing columns still
    raise :class:`ActivityModelSchemaError`. Two upload columns that
    strip-collide to the same canonical name (e.g. both ``"Item"`` and
    ``" Item "`` present) are also rejected, since the rename would be
    ambiguous.

    Returns the re-serialised bytes; on a no-rewrite-needed input the
    return value is byte-equal to ``raw_bytes``.
    """
    try:
        df = pd.read_csv(io.BytesIO(raw_bytes))
    except Exception as exc:  # noqa: BLE001
        raise ActivityModelSchemaError(
            f"{spec.filename}: file is not a valid CSV ({exc})."
        ) from exc

    actual = [str(c) for c in df.columns]
    expected = list(spec.expected_columns)

    # Build a stripped-name → canonical map. If any duplicate stripped
    # names appear in the upload, refuse rather than silently picking one.
    stripped_to_canonical = {_stripped(c): c for c in expected}
    actual_stripped_counts: dict[str, int] = {}
    for c in actual:
        s = _stripped(c)
        actual_stripped_counts[s] = actual_stripped_counts.get(s, 0) + 1
    duplicate_strips = [s for s, n in actual_stripped_counts.items() if n > 1]
    if duplicate_strips:
        raise ActivityModelSchemaError(
            f"{spec.filename}: ambiguous columns — multiple columns strip to "
            f"{duplicate_strips!r}. Resolve duplicates before uploading."
        )

    actual_stripped = {_stripped(c) for c in actual}
    expected_stripped = set(stripped_to_canonical.keys())
    missing = sorted(expected_stripped - actual_stripped)
    extra   = sorted(actual_stripped - expected_stripped)
    if missing or extra:
        msg_parts = [f"{spec.filename}: column-set mismatch."]
        if missing:
            msg_parts.append(f"Missing columns (whitespace-insensitive): {missing!r}.")
        if extra:
            msg_parts.append(f"Unexpected columns (whitespace-insensitive): {extra!r}.")
        msg_parts.append(f"Expected (canonical): {expected!r}.")
        msg_parts.append(f"Got: {actual!r}.")
        raise ActivityModelSchemaError(" ".join(msg_parts))

    rename_map = {c: stripped_to_canonical[_stripped(c)] for c in actual}
    needs_rewrite = (
        actual != expected            # order or whitespace differs
        or any(rename_map[c] != c for c in actual)  # any individual rename
    )
    if not needs_rewrite:
        return raw_bytes

    out_df = df.rename(columns=rename_map)[expected]
    buf = io.BytesIO()
    out_df.to_csv(buf, index=False)
    return buf.getvalue()


def _validate_schema(spec: ActivityFileSpec, raw_bytes: bytes) -> None:
    """Tolerant validation — wraps :func:`_normalise_to_canonical` and discards the result.

    Used in non-write paths (bootstrap pre-flight) where we just want
    to fail loudly on a malformed CSV without rewriting it.
    """
    _normalise_to_canonical(spec, raw_bytes)


# ── Public API: bootstrap ────────────────────────────────────────────────────

def bootstrap_from_local_if_empty(local_dir: Path) -> dict[str, bool]:
    """Upload every local CSV under ``local_dir`` that's not already in Fabric.

    Idempotent and safe to call from a render path. Returns
    ``{filename: was_uploaded}`` so the caller can render a one-time
    "first-load bootstrap" caption.

    NOT transactional: each file is uploaded independently. If a network
    drop kills the bootstrap halfway through, calling this function again
    on the next render finishes the remaining files.
    """
    written: dict[str, bool] = {}
    for spec in EXPECTED_FILES:
        local = local_dir / spec.filename
        if not local.exists():
            written[spec.filename] = False
            continue
        with open(local, "rb") as fh:
            payload = fh.read()
        # Validate AND canonicalise the seed — a local CSV that's been
        # round-tripped through Excel and lost its trailing spaces
        # should still bootstrap cleanly with canonical headers.
        payload = _normalise_to_canonical(spec, payload)
        try:
            wrote = _io.bootstrap_bytes_if_absent(
                _SECRETS_SECTION, spec.blob_path, payload,
            )
        except _io.LakehouseIOError as exc:
            raise ActivityModelStoreError(
                f"Failed bootstrapping {spec.filename}: {exc}"
            ) from exc
        written[spec.filename] = wrote

    if any(written.values()):
        # At least one bootstrap upload happened — invalidate the
        # ETag-listing cache so the parquet-rebuild gate on the next
        # render sees the freshly-seeded files.
        _invalidate_etag_cache()
    return written


# ── Public API: read ─────────────────────────────────────────────────────────

@st.cache_data(ttl=_ETAG_CACHE_TTL_SECONDS, show_spinner=False)
def _fabric_etags_cached() -> dict[str, Optional[str]]:
    """Single-round-trip ``{filename: etag-or-None}`` for every expected file.

    Implementation: ``list_files`` enumerates the entire
    ``Activity_Model/`` folder in ONE request and returns ETag + size +
    last-modified per entry. This replaces the legacy 9-file
    ``download_file()`` fan-out (which downloaded every blob's body
    just to read its ETag — ~1 s of redundant network I/O on every
    page render against a corporate-network connection).

    Files registered in :data:`EXPECTED_FILES` but missing from
    OneLake are reported as ``None`` so the parquet-rebuild gate sees
    them as "not yet bootstrapped" and doesn't short-circuit.
    """
    try:
        listing = _io.list_files(_SECRETS_SECTION, _FOLDER)
    except _io.LakehouseIOError as exc:
        raise ActivityModelStoreError(
            f"Failed listing OneLake folder Files/{_FOLDER}: {exc}"
        ) from exc

    by_name = {f.name: f for f in listing}
    return {spec.filename: (by_name[spec.filename].etag if spec.filename in by_name else None)
            for spec in EXPECTED_FILES}


def fabric_etags() -> dict[str, Optional[str]]:
    """Return ``{filename: etag-or-None}`` for every expected file.

    Used as the cache key for the parquet build so the processor only
    re-runs when something in OneLake changed.

    Backed by a 60-second Streamlit cache (see ``_fabric_etags_cached``);
    upload paths in this module call :func:`_invalidate_etag_cache`
    after a successful write so the next render sees the new ETag
    immediately rather than waiting for the TTL.
    """
    return _fabric_etags_cached()


def _invalidate_etag_cache() -> None:
    """Drop the cached ``{filename: etag}`` mapping after a write."""
    _fabric_etags_cached.clear()


def read_raw_bytes(name: str) -> bytes:
    """Return the exact stored bytes for ``name``.

    Used by ``st.download_button`` so the user gets the byte-for-byte
    copy that's currently in Fabric, NOT a re-serialised pandas
    round-trip.
    """
    spec = _spec(name)
    try:
        raw, _etag = _io.read_bytes(_SECRETS_SECTION, spec.blob_path)
    except _io.LakehouseIOError as exc:
        raise ActivityModelStoreError(str(exc)) from exc
    if raw is None:
        raise ActivityModelStoreError(
            f"{spec.filename} is not in OneLake yet. "
            "Reload the page once to trigger first-time bootstrap."
        )
    return raw


def materialise_to_dir(target_dir: Path) -> dict[str, Path]:
    """Write every Activity_Model CSV to ``target_dir`` (creating it as needed).

    Returns ``{filename: target_path}``. Used by the New Price Quote
    page to feed the existing ``processing/new_pricing_processor.py``
    subprocess, which reads from a dir of CSVs. Keeping this contract
    intact means the processor itself doesn't need to be rewritten.
    """
    target_dir.mkdir(parents=True, exist_ok=True)
    out: dict[str, Path] = {}
    for spec in EXPECTED_FILES:
        try:
            raw, _etag = _io.read_bytes(_SECRETS_SECTION, spec.blob_path)
        except _io.LakehouseIOError as exc:
            raise ActivityModelStoreError(
                f"Failed downloading {spec.filename}: {exc}"
            ) from exc
        if raw is None:
            raise ActivityModelStoreError(
                f"{spec.filename} is missing from OneLake. "
                "Reload the page once to trigger first-time bootstrap, "
                "or upload it via the 'Upload to replace' panel below."
            )
        target = target_dir / spec.filename
        with open(target, "wb") as fh:
            fh.write(raw)
        out[spec.filename] = target
    return out


# ── Public API: write ────────────────────────────────────────────────────────

def write_csv_bytes(name: str, raw_bytes: bytes) -> str:
    """Replace ``name`` in OneLake with ``raw_bytes``. Returns the new ETag.

    Validates the column set tolerantly (whitespace + order) and
    rewrites to the canonical column names before writing, so the
    stored copy is always processor-compatible regardless of how the
    user's editor mangled the headers.

    Uses the freshly-fetched ETag for an ``If-Match`` write. On
    ``ResourceModifiedError`` we don't auto-retry — the user pressed
    "Push to Fabric", so a fresh click is the right merge resolution.
    """
    spec = _spec(name)
    canonical_bytes = _normalise_to_canonical(spec, raw_bytes)

    try:
        _, etag = _io.read_bytes(_SECRETS_SECTION, spec.blob_path)
    except _io.LakehouseIOError as exc:
        raise ActivityModelStoreError(str(exc)) from exc

    try:
        new_etag = _io.write_bytes(
            _SECRETS_SECTION, spec.blob_path, canonical_bytes, etag=etag,
        )
    except _io.LakehouseIOError as exc:
        raise ActivityModelStoreError(str(exc)) from exc

    # Drop the ETag-listing cache so the very next call to
    # ``fabric_etags()`` sees this freshly-uploaded blob.
    _invalidate_etag_cache()
    return new_etag


# ── Public API: identity ─────────────────────────────────────────────────────

def get_store_label() -> str:
    """Return a short human-readable label of where the data lives.

    Used by the New Price Quote panel caption.
    """
    return _io.LakehouseRef(
        secrets_section=_SECRETS_SECTION,
        blob_path=_FOLDER,
    ).display


# Default seed directory used to bootstrap a fresh OneLake folder — same
# path the legacy upload UX resolved to.
DEFAULT_SEED_DIR: Path = (
    Path(__file__).resolve().parent.parent / "data" / "HTST Activity Quote"
)


__all__ = [
    "ActivityModelStoreError",
    "ActivityModelSchemaError",
    "ActivityFileSpec",
    "EXPECTED_FILES",
    "DEFAULT_SEED_DIR",
    "bootstrap_from_local_if_empty",
    "fabric_etags",
    "read_raw_bytes",
    "materialise_to_dir",
    "write_csv_bytes",
    "get_store_label",
]
