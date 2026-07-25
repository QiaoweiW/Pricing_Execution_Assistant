"""
OneLake-backed reader for the HTST Shipment Monitor **lookup** tables.

The HTST Activity Monitor enriches the shipment Delta table with four lookups
and prices activity with four fee/bracket tables.  These used to be uploaded by
hand every session; this module reads them straight from the Fabric lakehouse so
the dashboard runs end-to-end once Microsoft Fabric is connected — no uploads.

Layout in the lakehouse (``fabric_htst`` → the B2C Actuals lakehouse)
--------------------------------------------------------------------
``Files/Activity_Model/Shipment Report/``  (dated snapshots — newest wins)
    Shipment_Plant_Tracker_<date>.csv        Shipping Warehouse → Plant
    Ship_Route_Mileage_Tracker_<date>.csv    (Sourcing Plant, SHIPTONAME) → Mileage
    Demantra_<date>.csv                       Item master: pallet config + Format
    Delivered vs FOB_Tracker_<date>.csv       (Item Desc, Party Site) → Pricing Method

``Files/Activity_Model/``  (stable fee/bracket tables)
    Sell-to_Volume Bracket_Fee.csv
    Custom Label_Volume Bracket_Fee.csv
    Pallet_Fee.csv
    Delivery_Miles Tier_Drop Size Tier_Fee.csv

The four enrichment lookups are REQUIRED (a failure raises so the page can fall
back to manual upload); the four fee tables are OPTIONAL (a failure leaves that
frame ``None`` and the page substitutes its hard-coded fallback fees).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pandas as pd
import streamlit as st

from data_sources import fabric_lakehouse_io as _io

_SECRETS_SECTION: str = "fabric_htst"
_SHIPMENT_REPORT_FOLDER: str = "Activity_Model/Shipment Report"
_FEE_FOLDER: str = "Activity_Model"
_CACHE_TTL_SECONDS: int = 60 * 60

# Fabric deep-link to the folder operators refresh (shown in the page's SOP).
FABRIC_SHIPMENT_REPORT_URL: str = (
    "https://app.fabric.microsoft.com/groups/"
    "bb11c51d-03c8-4f1b-938c-e20657a8f31d/lakehouses/"
    "a01f513d-eee7-41eb-8c15-670bc40e7fc8?experience=fabric-developer"
    "&selectedPath=Files%2FActivity_Model%2FShipment%20Report"
)


class HTSTLookupError(RuntimeError):
    """Raised when a REQUIRED enrichment lookup can't be read from Fabric."""


@dataclass(frozen=True)
class _LookupSpec:
    key: str          # dict key used by the page
    folder: str       # lakehouse folder under Files/
    match: str        # dated → filename prefix; stable → exact filename
    label: str        # human label (SOP + errors)
    dated: bool       # True → newest file whose name starts with `match`
    required: bool     # True → raise on failure; False → None on failure


# Enrichment lookups — dated snapshots, newest by name wins.
_ENRICHMENT_SPECS: tuple[_LookupSpec, ...] = (
    _LookupSpec("plant_tracker", _SHIPMENT_REPORT_FOLDER, "Shipment_Plant_Tracker_",
                "Shipment Plant Tracker", True, True),
    _LookupSpec("mileage_tracker", _SHIPMENT_REPORT_FOLDER, "Ship_Route_Mileage_Tracker_",
                "Ship Route Mileage Tracker", True, True),
    _LookupSpec("demantra", _SHIPMENT_REPORT_FOLDER, "Demantra_",
                "Demantra Item Master", True, True),
    _LookupSpec("pricing_tracker", _SHIPMENT_REPORT_FOLDER, "Delivered vs FOB_Tracker_",
                "Delivered vs FOB Pricing Tracker", True, True),
)
# Fee / bracket tables — stable filenames, optional (hard-coded fallback exists).
_FEE_SPECS: tuple[_LookupSpec, ...] = (
    _LookupSpec("sell_to", _FEE_FOLDER, "Sell-to_Volume Bracket_Fee.csv",
                "Sell-to Volume Bracket Fee", False, False),
    _LookupSpec("custom_label", _FEE_FOLDER, "Custom Label_Volume Bracket_Fee.csv",
                "Custom Label Volume Bracket Fee", False, False),
    _LookupSpec("pallet_fee", _FEE_FOLDER, "Pallet_Fee.csv",
                "Pallet Fee", False, False),
    _LookupSpec("delivery", _FEE_FOLDER, "Delivery_Miles Tier_Drop Size Tier_Fee.csv",
                "Delivery Charge Table", False, False),
)
_ALL_SPECS: tuple[_LookupSpec, ...] = _ENRICHMENT_SPECS + _FEE_SPECS


@dataclass(frozen=True)
class LookupFileMeta:
    """One resolved source file — surfaced in the SOP panel for staleness checks."""
    key: str
    label: str
    folder: str
    name: str
    last_modified: Optional[str]


@dataclass(frozen=True)
class HTSTLookupBundle:
    """All lookups read from Fabric.

    ``frames`` maps each spec key to its DataFrame (columns stripped); a missing
    OPTIONAL fee table is absent from the dict.  ``files`` carries per-file
    metadata for the SOP panel.
    """
    frames: dict[str, pd.DataFrame]
    files: tuple[LookupFileMeta, ...]

    def get(self, key: str) -> Optional[pd.DataFrame]:
        return self.frames.get(key)


def _resolve_file(spec: _LookupSpec) -> Optional[_io.LakehouseFile]:
    """Return the source LakehouseFile for *spec* (newest dated / exact), or None."""
    files = _io.list_files(_SECRETS_SECTION, spec.folder, suffix=".csv")
    if spec.dated:
        cands = [f for f in files if f.name.startswith(spec.match)]
        if not cands:
            return None
        return max(cands, key=lambda f: (f.name, f.last_modified or ""))
    return next((f for f in files if f.name == spec.match), None)


@st.cache_data(ttl=_CACHE_TTL_SECONDS, show_spinner=False)
def _cached_fetch() -> tuple[dict[str, pd.DataFrame], list[LookupFileMeta]]:
    """Read every lookup once; native return type keeps Streamlit's fast cache path."""
    frames: dict[str, pd.DataFrame] = {}
    files: list[LookupFileMeta] = []
    for spec in _ALL_SPECS:
        try:
            src = _resolve_file(spec)
            if src is None:
                raise HTSTLookupError(
                    f"No '{spec.match}{'*' if spec.dated else ''}' file under "
                    f"'Files/{spec.folder}'.")
            df, _etag = _io.read_csv(_SECRETS_SECTION, src.full_path)
            if df is None:
                raise HTSTLookupError(f"File not found in OneLake: Files/{src.full_path}")
            df.columns = df.columns.str.strip()
            frames[spec.key] = df
            files.append(LookupFileMeta(
                spec.key, spec.label, spec.folder, src.name, src.last_modified))
        except (_io.LakehouseIOError, HTSTLookupError) as exc:
            if spec.required:
                raise HTSTLookupError(f"{spec.label}: {exc}") from exc
            # Optional fee table — leave absent; the page falls back to defaults.
    return frames, files


def fetch_htst_lookups(*, force_refresh: bool = False) -> HTSTLookupBundle:
    """Return all HTST lookup tables from Fabric (see module docstring).

    Raises :class:`HTSTLookupError` if any REQUIRED enrichment lookup is missing
    or unreadable, so the page can fall back to manual upload.  ``force_refresh``
    clears this connector's cache slot before reading.
    """
    if force_refresh:
        _cached_fetch.clear()
    frames, files = _cached_fetch()
    return HTSTLookupBundle(frames=frames, files=tuple(files))
