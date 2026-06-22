"""
ORDS REST client — the ONLY path to the data (the Oracle DB sits behind ORDS;
there is no DB DSN available to clients).

  READ  -> GET  {base}/{resource}/?q=<FbDS JSON>&limit&offset   (filtered, paged)
  WRITE -> POST / PUT / DELETE {base}/{resource}/[{id}]

Service auth is declared None at ordsprod.darigold.com (network-restricted), so
no credentials are sent unless [ords].auth = "basic" is set in secrets.

READ filtering uses ORDS AutoREST "filter-by-data" (FbDS) syntax via the ``q``
query parameter. If /priceadjs/ turns out to be a hand-written resource module
instead of an AutoREST endpoint, swap ``_build_q`` for that module's named query
parameters — nothing else needs to change.
"""
import datetime as _dt
import json
import math

import numpy as np
import pandas as pd
import requests
import streamlit as st
from concurrent.futures import ThreadPoolExecutor, as_completed

from .schemas import PRICEADJS_DEFAULTS, PRICEADJS_FIELDS, PRICEADJS_FILTERS


def _session():
    s = requests.Session()
    cfg = st.secrets.get("ords", {})
    if cfg.get("auth") == "basic":
        s.auth = (cfg["username"], cfg["password"])
    return s


def BASE():
    return st.secrets["ords"]["base_url"].rstrip("/")


# ======================================================================
# READ
# ======================================================================

# Output column order (mirrors the spec's Excel export). ``id`` is kept so the
# editable grid / ORDS diff has its primary key.
_READ_COLS = [
    "id", "pricelistname", "pricinguom", "baseprice",
    "chargestartdate", "chargeenddate", "itemname",
    "customername", "customernumber", "shiptositename", "customersitenumber",
    "adjustmenttype", "adjustmentamount", "adjustmentbasis", "precedence",
    "market", "marketindex", "age", "spec", "grade",
    "adjustmentstartdate", "adjustmentenddate", "status", "status_msg",
    "excludefromcpprice", "batchno",
    "creation_date", "created_by", "last_update_date", "last_updated_by",
    "external_system_ref_id",
]


# The dates in this table are Pacific wall-clock stored as UTC (e.g. a window
# starting "Sep 1" is 2025-09-01T07:00:00Z = midnight PDT). The user picks
# calendar dates thinking in Pacific, so we convert the picked date's Pacific
# start/end-of-day to the matching UTC instant before filtering — otherwise the
# UTC offset silently drops the boundary rows.
_PACIFIC_TZ = "America/Los_Angeles"


def _ords_date(d, *, end_of_day: bool) -> dict:
    """ORDS FbDS date literal for a picked calendar date, Pacific -> UTC."""
    t = _dt.time(23, 59, 59) if end_of_day else _dt.time(0, 0, 0)
    try:
        from zoneinfo import ZoneInfo
        local = _dt.datetime.combine(d, t, tzinfo=ZoneInfo(_PACIFIC_TZ))
        utc = local.astimezone(_dt.timezone.utc)
        stamp = utc.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    except Exception:  # noqa: BLE001 - tz db missing: fall back to UTC-naive
        stamp = f"{d}T{t.strftime('%H:%M:%S')}.000Z"
    return {"$date": stamp}


def _split_multi(v) -> list[str]:
    """Normalize a filter value to a list of non-empty terms (comma-delimited)."""
    if isinstance(v, (list, tuple)):
        return [str(x).strip() for x in v if str(x).strip()]
    return [s.strip() for s in str(v).split(",") if s.strip()]


def _build_q(filters: dict) -> dict:
    """Build the ORDS FbDS ``q`` object: fixed DEFAULT constraints + P filters.

    Conditions are collected into a top-level ``$and`` so a column can carry an
    OR-group (e.g. several item names) without colliding with other columns.
    """
    conds: list[dict] = []
    # Fixed "D" constraints (always applied).
    for col, val in PRICEADJS_DEFAULTS.items():
        conds.append({col: {"$eq": val}})
    # User "P" filters.
    for col, cmp_ in PRICEADJS_FILTERS:
        v = filters.get(col)
        if v in (None, "", []):
            continue
        if cmp_ == "Contains":
            # ORDS $like is case-sensitive; uppercase the term to match the
            # upcased name/site values these columns store.
            conds.append({col: {"$like": f"%{str(v).upper()}%"}})
        elif cmp_ == "ContainsAny":
            likes = [{col: {"$like": f"%{x.upper()}%"}} for x in _split_multi(v)]
            if not likes:
                continue
            conds.append(likes[0] if len(likes) == 1 else {"$or": likes})
        elif cmp_ == "Equals":
            conds.append({col: {"$eq": v}})
        elif cmp_ == "In":
            vals = _split_multi(v)
            if vals:
                conds.append({col: {"$in": vals}})
        elif cmp_ == "DateOnOrAfter":
            conds.append({col: {"$gte": _ords_date(v, end_of_day=False)}})
        elif cmp_ == "DateOnOrBefore":
            conds.append({col: {"$lte": _ords_date(v, end_of_day=True)}})

    q: dict = {"$and": conds} if conds else {}
    q["$orderby"] = {"customername": "ASC", "itemname": "ASC"}
    return q


def _get_page(resource: str, q: dict, limit: int, offset: int) -> dict:
    params = {"q": json.dumps(q), "limit": limit, "offset": offset}
    r = _session().get(f"{BASE()}/{resource}/", params=params, timeout=60)
    r.raise_for_status()
    return r.json()


def read_rows(resource: str, q: dict, max_rows: int = 5000,
              page_size: int = 500) -> list[dict]:
    """Page through an ORDS collection until max_rows or hasMore is false."""
    items: list[dict] = []
    offset = 0
    while len(items) < max_rows:
        page = _get_page(resource, q, min(page_size, max_rows - len(items)), offset)
        batch = page.get("items", [])
        items.extend(batch)
        if not batch or not page.get("hasMore"):
            break
        offset += len(batch)
    return items[:max_rows]


def _shape(items: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(items)
    if df.empty:
        return df
    df.columns = [str(c).lower() for c in df.columns]
    df = df.drop(columns=["links"], errors="ignore")  # ORDS per-row hypermedia
    # Coerce declared datetime columns (ORDS returns ISO strings) so the grid
    # and the ORDS write-serializer treat them as real timestamps.
    for col, meta in PRICEADJS_FIELDS.items():
        if meta.get("type") == "datetime" and col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce")
    ordered = [c for c in _READ_COLS if c in df.columns]
    rest = [c for c in df.columns if c not in ordered]
    return df[ordered + rest]


@st.cache_data(ttl=120, show_spinner="Reading from Oracle (via ORDS)…")
def read_price_adjustments(filters: dict, limit: int = 5000) -> pd.DataFrame:
    """Read market-price adjustments via ORDS, constrained to PRICEADJS_DEFAULTS."""
    return _shape(read_rows("priceadjs", _build_q(filters), max_rows=limit))


@st.cache_data(ttl=300, show_spinner=False)
def distinct_markets(sample: int = 5000) -> list[str]:
    """Distinct Market values for the filter dropdown.

    ORDS AutoREST has no DISTINCT, so we sample the default-constrained
    collection and de-dupe client-side. Cached; falls back to [] (free text) on
    any failure.
    """
    try:
        q = {c: {"$eq": v} for c, v in PRICEADJS_DEFAULTS.items()}
        q["$orderby"] = {"market": "ASC"}
        items = read_rows("priceadjs", q, max_rows=sample)
        return sorted({str(it.get("market")) for it in items
                       if it.get("market") not in (None, "")})
    except Exception:  # noqa: BLE001 - dropdown degrades to free text
        return []


# ======================================================================
# WRITE — JSON serialization
# ======================================================================
# Payload values come straight out of a pandas DataFrame, so they are often
# numpy scalars (np.int64/np.float64) or pandas Timestamps — none of which the
# stdlib json encoder can serialize. ORDS also wants ISO8601 strings for dates.
# Every payload is passed through ``_serialize`` before it hits ``json=``.

def _jsonable(v):
    """Convert a single cell value to a JSON/ORDS-friendly Python primitive."""
    if v is None:
        return None
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(v, (pd.Timestamp, _dt.datetime, _dt.date)):
        ts = pd.Timestamp(v)
        if ts.tzinfo is None:
            return ts.strftime("%Y-%m-%dT%H:%M:%SZ")
        return ts.isoformat()
    if isinstance(v, np.integer):
        return int(v)
    if isinstance(v, (np.floating, float)):
        f = float(v)
        return None if math.isnan(f) else f
    if isinstance(v, np.bool_):
        return bool(v)
    return v


def _serialize(payload: dict) -> dict:
    return {k: _jsonable(v) for k, v in payload.items()}


# ======================================================================
# WRITE — POST / PUT / DELETE
# ======================================================================

def update_row(resource: str, row_id: str, payload: dict) -> dict:
    r = _session().put(f"{BASE()}/{resource}/{row_id}",
                       json=_serialize(payload), timeout=30)
    return {"op": "update", "id": row_id, "ok": r.ok,
            "status": r.status_code, "body": r.text[:500]}


def create_row(resource: str, payload: dict) -> dict:
    r = _session().post(f"{BASE()}/{resource}/",
                        json=_serialize(payload), timeout=30)
    return {"op": "create", "id": None, "ok": r.ok,
            "status": r.status_code, "body": r.text[:500]}


def delete_row(resource: str, row_id: str) -> dict:
    r = _session().delete(f"{BASE()}/{resource}/{row_id}", timeout=30)
    return {"op": "delete", "id": row_id, "ok": r.ok,
            "status": r.status_code, "body": r.text[:500]}


def push_changes(resource: str, changes: list[dict], max_workers: int = 4,
                 progress_cb=None) -> list[dict]:
    """
    changes: list of {"op": "create"|"update"|"delete", "id": str|None, "payload": dict}
    Mirrors VBAFE ParallelUploadRequestCount=4. If you hit optimistic-lock
    errors, drop max_workers to 1.

    progress_cb: optional ``callable(done: int, total: int)`` invoked from the
    calling thread each time a request completes — lets the caller drive a
    progress bar. (Called in completion order, not submission order.)
    """
    results = []
    total = len(changes)
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = []
        for c in changes:
            if c["op"] == "update":
                futures.append(ex.submit(update_row, resource, c["id"], c["payload"]))
            elif c["op"] == "create":
                futures.append(ex.submit(create_row, resource, c["payload"]))
            elif c["op"] == "delete":
                futures.append(ex.submit(delete_row, resource, c["id"]))
        for f in as_completed(futures):
            results.append(f.result())
            if progress_cb is not None:
                progress_cb(len(results), total)
    return results


def ping() -> tuple[bool, str]:
    """Lightweight reachability check (GET the base URL)."""
    try:
        r = _session().get(f"{BASE()}/", timeout=15)
        return r.ok, f"HTTP {r.status_code}"
    except Exception as e:  # noqa: BLE001
        return False, str(e)
