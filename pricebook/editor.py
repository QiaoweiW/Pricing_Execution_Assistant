"""
Generic, schema-driven table editor.

One ``render_editor(cfg, fetch_fn)`` call powers every pricebook page:
  * builds the "Read Data in Oracle" filter form from cfg["filters"]
  * READs via the supplied fetch_fn (ORDS REST)
  * shows the result read-only, downloadable as CSV / Excel
  * accepts an edited CSV back and turns it into ORDS writes, routed by the
    Status operation code (U -> update, N -> insert); reports per-row status

Note: SelectboxColumn options are unioned with the values actually present, so
a legacy value outside the canonical LOV doesn't make the grid raise.
"""
import json

import pandas as pd
import streamlit as st

from .batch import make_batch_number
from .ords_client import push_changes
from .schemas import LOVS, STATUS_LABELS

# Columns rendered with 4-decimal precision when they are numeric.
_PRICE_COLS = {"baseprice", "adjustmentamount"}


def _selectbox_options(lov_key: str, series: pd.Series) -> list[str]:
    """Canonical LOV first, then any extra values present in the data."""
    opts = list(LOVS.get(lov_key, []))
    seen = set(opts)
    for v in series.dropna().astype(str).unique():
        if v not in seen:
            opts.append(v)
            seen.add(v)
    return opts


def _build_column_config(df: pd.DataFrame, fields: dict) -> dict:
    """Derive st.column_config from the schema for the columns present."""
    cfg = {}
    for col in df.columns:
        meta = fields.get(col)
        # Anything not in the schema (join columns like itemdescription, audit
        # columns like creation_date/created_by) is display-only.
        editable = bool(meta) and not meta.get("readonly", False)
        ftype = (meta or {}).get("type", "string")
        lov = (meta or {}).get("lov")

        if lov:
            cfg[col] = st.column_config.SelectboxColumn(
                options=_selectbox_options(lov, df[col]), disabled=not editable)
        elif ftype == "number":
            fmt = "%.4f" if col in _PRICE_COLS else None
            cfg[col] = st.column_config.NumberColumn(format=fmt, disabled=not editable)
        elif ftype == "datetime":
            cfg[col] = st.column_config.DatetimeColumn(disabled=not editable)
        else:
            cfg[col] = st.column_config.TextColumn(disabled=not editable)
    return cfg


# Friendly filter labels (col -> label). Falls back to a title-cased column name.
_FILTER_LABELS = {
    "itemname":            "Item name (contains — comma for multiple)",
    "customername":        "Customer name (contains — comma for multiple)",
    "shiptositename":      "Ship-to site (contains — comma for multiple)",
    "customersitenumber":  "Customer site # (contains — comma for multiple)",
    "batchno":             "Batch # (contains)",
    "market":              "Market",
    "status":              "Status",
    "adjustmentstartdate": "Adjustment start on/after",
    "adjustmentenddate":   "Adjustment end on/before",
}


def _resolve_options(provider) -> list[str]:
    """filter_options entry may be a list or a callable returning a list."""
    try:
        opts = provider() if callable(provider) else provider
        return [str(o) for o in (opts or [])]
    except Exception:  # noqa: BLE001 - fall back to free text on any failure
        return []


def _filter_widgets(key: str, filters: list, fields: dict,
                    filter_options: dict | None) -> dict:
    """Render the filter inputs (inside the Read expander) and return {col: value}."""
    filter_options = filter_options or {}
    out = {}
    # Lay the inputs out 3 per row to stay compact.
    for i in range(0, len(filters), 3):
        row = filters[i:i + 3]
        cols = st.columns(len(row))
        for (col, cmp_), slot in zip(row, cols):
            label = _FILTER_LABELS.get(col, col.replace("_", " ").title())
            wkey = f"{key}_flt_{col}"
            with slot:
                if cmp_ in ("DateOnOrAfter", "DateOnOrBefore"):
                    out[col] = st.date_input(label, value=None, key=wkey)
                elif cmp_ == "In":
                    # Multi-select dropdown from the column's LOV (e.g. status).
                    lov = (fields.get(col) or {}).get("lov")
                    opts = LOVS.get(lov, [])
                    fmt = ((lambda x: f"{x} – {STATUS_LABELS.get(x, '')}")
                           if col == "status" else str)
                    out[col] = st.multiselect(label, opts, format_func=fmt, key=wkey)
                elif cmp_ == "Equals" and col in filter_options:
                    opts = _resolve_options(filter_options[col])
                    if opts:
                        out[col] = st.selectbox(label, [""] + opts, key=wkey)
                    else:
                        # No options available (e.g. ORDS unreachable) -> free text.
                        out[col] = st.text_input(label, key=wkey)
                else:
                    out[col] = st.text_input(label, key=wkey)
    return {k: v for k, v in out.items() if v not in (None, "", [])}


def _to_excel_bytes(df: pd.DataFrame, sheet_name: str = "Data") -> bytes:
    import io
    buf = io.BytesIO()
    # Sheet names are capped at 31 chars by Excel.
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        df.to_excel(xw, index=False, sheet_name=sheet_name[:31])
    return buf.getvalue()


def _diagnose(res: dict) -> str:
    """Turn an ORDS response into a human-readable per-row status message."""
    body = res.get("body", "") or ""
    parsed = None
    try:
        parsed = json.loads(body)
    except Exception:  # noqa: BLE001 - body may not be JSON
        parsed = None

    if res["ok"]:
        # ORDS often echoes the row back; surface its workflow status if present.
        if isinstance(parsed, dict) and parsed.get("status"):
            code = str(parsed["status"])
            return STATUS_LABELS.get(code, f"Succeeded ({code})")
        return "Succeeded"

    # Error: pull the most useful message ORDS gives us.
    if isinstance(parsed, dict):
        for k in ("message", "o:errorDetails", "detail", "title", "error", "cause"):
            if parsed.get(k):
                return f"HTTP {res['status']}: {parsed[k]}"
    snippet = body.strip().replace("\n", " ")[:200]
    return f"HTTP {res['status']}" + (f": {snippet}" if snippet else "")


# Status codes that aren't yet a final outcome — informational only, for the
# read-side guide ("not yet settled to S/E").
_PENDING_STATUSES = {"U", "N", "IN", "IU"}

# The Status column IS the operation code (per the _VBAFE_Services LocalDataSource).
# Only these two codes trigger a write; everything else is left untouched:
#   U -> update an existing row  -> PUT  /priceadjs/{id}   (id preserved)
#   N -> insert a new adjustment -> POST /priceadjs/
# S (already processed), E (prior error — fix then flip to U), and IN/IU
# (in-flight) are intentionally NOT sent.
_STATUS_TO_OP = {"U": "update", "N": "create"}

# Why a row was skipped, keyed by its (upper-cased) Status code. Used to tell the
# user exactly why an uploaded row wasn't sent.
_SKIP_REASONS = {
    "S": "already processed (S) — set Status to U to change it",
    "E": "prior error (E) — fix the issue, then set Status to U",
    "IN": "in-flight (IN) — leave until it settles to S/E",
    "IU": "in-flight (IU) — leave until it settles to S/E",
    "": "no Status — set U to update or N to insert",
}

# Only status_msg is withheld from the payload — it's the ERP's error-output
# column. The primary key (id) is excluded automatically because it's readonly
# (it travels in the PUT URL, not the body). Status IS sent: it's the op code.
_WRITE_EXCLUDE = {"status_msg"}


def _cell_blank(v) -> bool:
    if v is None:
        return True
    if isinstance(v, (list, dict)):
        return len(v) == 0
    try:
        if pd.isna(v):
            return True
    except (TypeError, ValueError):
        pass
    return str(v).strip() == ""


def _changes_from_upload(upload_df: pd.DataFrame, fields: dict,
                         batch_no: str) -> tuple[list[dict], list[tuple]]:
    """Turn an uploaded CSV into an ORDS change list, driven by the Status code.

    The ``status`` column is the operation code (see :data:`_STATUS_TO_OP`):
      * ``U`` -> PUT /priceadjs/{id}  (update the existing row, id preserved)
      * ``N`` -> POST /priceadjs/      (insert a new adjustment)
      * anything else (S / E / IN / IU / blank) -> not sent.

    The **full** editable payload present in the CSV is sent every time (the
    service sets ``SendOnlyChangedDataForUpdatesEnabled = false``), so we do not
    diff against the prior read. ``status_msg`` is withheld and ``id`` is dropped
    from the body (it's readonly and travels in the URL). Deletes are not
    supported (``DeleteEnabled = false``).

    Returns ``(changes, skipped)`` where ``skipped`` lists ``(id, reason)`` for
    every row that was not sent, so the UI can explain exactly why.
    """
    upload_df = upload_df.rename(columns={c: str(c).strip().lower()
                                          for c in upload_df.columns})
    write_cols = [c for c, m in fields.items()
                  if not m.get("readonly") and c not in _WRITE_EXCLUDE
                  and c in upload_df.columns]

    changes: list[dict] = []
    skipped: list[tuple] = []

    for _, row in upload_df.iterrows():
        rid = "" if _cell_blank(row.get("id")) else str(row["id"]).strip()
        status = "" if _cell_blank(row.get("status")) else str(row["status"]).strip().upper()
        op = _STATUS_TO_OP.get(status)

        if op is None:  # S / E / IN / IU / blank / unknown -> leave it alone
            reason = _SKIP_REASONS.get(status, f"Status '{status}' is not actionable")
            skipped.append((rid or "(new)", reason))
            continue

        if op == "update" and not rid:
            skipped.append(("(new)", "Status U needs an id — use N to insert instead"))
            continue

        # Full payload: every editable column present in the CSV (Status included,
        # since it's the op code). Dates are coerced so they serialize as ISO.
        payload = {}
        for c in write_cols:
            v = row.get(c)
            if _cell_blank(v):
                continue
            if (fields.get(c) or {}).get("type") == "datetime":
                v = pd.to_datetime(v, errors="coerce")
            payload[c] = v
        payload["batchno"] = batch_no  # fresh batch per upload (VBAFE DefaultBatchNumber)

        if op == "update":
            changes.append({"op": "update", "id": rid, "payload": payload})
        else:  # create (N)
            changes.append({"op": "create", "id": None, "payload": payload})

    return changes, skipped


def _defaults_caption(defaults: dict | None) -> str:
    if not defaults:
        return ""
    pairs = ", ".join(f"{k} = {v}" for k, v in defaults.items())
    return f"Always filtered to: {pairs}"


def _render_status_guide(df: pd.DataFrame) -> None:
    """Explain that Status is the operation code and how to change a price."""
    pending = 0
    if "status" in df.columns:
        pending = int(df["status"].astype(str).isin(_PENDING_STATUSES).sum())
    if pending:
        st.info(f"ℹ️ {pending} of these rows aren't a final outcome yet "
                "(Status N/U/IN/IU). See **What the Status column means** below.")
    with st.expander("ℹ️ What the Status column means (it's the operation code)", expanded=False):
        st.markdown(
            "`Status` is both the row's state **and** the instruction you give ORDS on upload:\n\n"
            "- **S — Success:** already processed and live in Oracle. Uploaded as-is it does "
            "**nothing**. To change it, edit the fields and **flip Status from S → U**.\n"
            "- **U — Update:** re-send this existing row by its `id`. ORDS issues "
            "`PUT /priceadjs/{id}` against the same record — **no new row, no new date range**. "
            "You can change the amount *or the dates* on the same row.\n"
            "- **N — New:** insert a brand-new adjustment. Use this only for a genuinely "
            "separate row (different item / customer / market, or a non-overlapping new period).\n"
            "- **E — Error:** ORDS/the interface rejected it — read `status_msg`, fix the cause, "
            "then set Status to **U** and re-upload.\n"
            "- **IN / IU — in-flight:** the interface is mid-processing. ⏳ Leave these alone "
            "until they settle to **S** or **E**, then re-read.\n\n"
            "**To change a price on an existing adjustment:** download → edit the field → flip "
            "**S → U** → upload. Do **not** insert a new row for the same item/market/dates — the "
            "ERP rejects it as *“Matrix Rule Already exists with overlapping dates.”*"
        )


def _render_upload_instructions() -> None:
    with st.expander("📋 How to upload changes (and caveats)", expanded=False):
        st.markdown(
            "**Steps**\n"
            "1. Set filters and **Read from Oracle**, then **⬇️ Download CSV** — that file is "
            "your editing template (every column, including `id` and `status`).\n"
            "2. Open it in Excel and, for each row you want to send:\n"
            "   - **Change an existing row:** edit the field(s) — amount and/or dates — keep its "
            "`id`, and **set `status` to `U`** (it comes down as `S`).\n"
            "   - **Add a brand-new row:** append a line, leave `id` blank, set **`status` = `N`**.\n"
            "   - **Leave a row as-is:** keep `status` = `S` (or anything other than U/N) and it "
            "won't be sent.\n"
            "3. Save as **CSV (UTF-8)** and upload it below.\n"
            "4. Review the detected changes (and any skipped rows), then **Push**.\n\n"
            "**Caveats**\n"
            "- **`status` is the operation code.** Only `U` (update) and `N` (insert) rows are "
            "sent; `S` / `E` / `IN` / `IU` / blank are skipped. If nothing happens, you probably "
            "left rows as `S`.\n"
            "- A `U` update is a `PUT` by `id` — it edits the **same** record (amount or dates). "
            "Don't insert a new row for the same item/market/dates, or the ERP rejects it as "
            "*overlapping*.\n"
            "- The **full row** is sent for every U/N (the service doesn't do changed-only "
            "updates), so **keep all the template's columns** and never change an existing `id`.\n"
            "- **No deletes** — the service disallows them; omitting a row does nothing.\n"
            "- `status_msg` is never uploaded (it's the ERP's error-output column).\n"
            "- Dates may be `YYYY-MM-DD` or full ISO timestamps; they're normalized before sending.\n"
            "- A green push result means **ORDS accepted** the call; the ERP's final outcome "
            "(S/E) appears in `status` only after you **re-read**."
        )


def render_editor(cfg: dict, fetch_fn, filter_options: dict | None = None) -> None:
    """Render a full Read -> grid -> push workflow for one table.

    filter_options: optional {col: list | callable} supplying dropdown choices
    for ``Equals`` filters (e.g. the Market dropdown).
    """
    key = cfg["resource"]
    fields = cfg["fields"]
    title = cfg["title"]

    st.subheader(f"{title} — ERP Editor")
    st.caption(f"Read & write via ORDS REST `/{cfg['resource']}` (table {cfg['table']})")

    df_key = f"{key}_df"

    # --- Foldable READ section ------------------------------------------
    with st.expander("📂 Read Data in Oracle", expanded=(df_key not in st.session_state)):
        caption = _defaults_caption(cfg.get("defaults"))
        if caption:
            st.caption(caption)
        filters = _filter_widgets(key, cfg["filters"], fields, filter_options)
        c1, c2 = st.columns([1, 3])
        with c1:
            row_limit = st.number_input("Row limit", 100, 50_000, 5000, step=500,
                                        key=f"{key}_limit")
        with c2:
            st.write("")  # vertical spacer to align the button with the input
            go = st.button("🔄 Read from Oracle", type="primary", key=f"{key}_fetch")

    if not (go or df_key in st.session_state):
        st.info("Open **Read Data in Oracle**, set your filters, and click **Read from Oracle**.")
        return

    if go:
        try:
            fetched = fetch_fn(filters, int(row_limit))
        except Exception as e:  # noqa: BLE001 - show the DB error in the UI
            st.error(f"Query failed: {e}")
            return
        st.session_state[df_key] = fetched
        # Drop any stale results from a previous push.
        st.session_state.pop(f"{key}_results", None)

    df = st.session_state[df_key]
    st.caption(f"{len(df):,} rows loaded from Oracle")

    if df.empty:
        st.warning("No rows matched those filters.")
        return

    # --- Download the pulled data ---------------------------------------
    d1, d2, _ = st.columns([1, 1, 4])
    with d1:
        st.download_button(
            "⬇️ Download CSV", data=df.to_csv(index=False).encode("utf-8"),
            file_name=f"{key}.csv", mime="text/csv", key=f"{key}_dl_csv")
    with d2:
        try:
            st.download_button(
                "⬇️ Download Excel", data=_to_excel_bytes(df, title),
                file_name=f"{key}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                key=f"{key}_dl_xlsx")
        except Exception as e:  # noqa: BLE001 - openpyxl missing etc.; CSV still works
            st.caption(f"Excel export unavailable: {e}")

    # Read-only view of what's currently in Oracle.
    st.dataframe(df, use_container_width=True, height=420,
                 column_config=_build_column_config(df, fields))

    _render_status_guide(df)

    # --- Upload changes (CSV) -------------------------------------------
    st.markdown("##### Upload changes (CSV)")
    _render_upload_instructions()

    uploaded = st.file_uploader("Upload your edited CSV", type=["csv"], key=f"{key}_upload")
    if uploaded is not None:
        try:
            # Keep id as text so large ids don't become floats (7521022.0).
            upload_df = pd.read_csv(uploaded, dtype={"id": str})
        except Exception as e:  # noqa: BLE001
            st.error(f"Could not read the CSV: {e}")
            return

        batch_no = make_batch_number(st.secrets.get("ords", {}).get("batch_initials", "WAN"))
        changes, skipped = _changes_from_upload(upload_df, fields, batch_no)

        # Always surface skipped rows — most "nothing happened" confusion is rows
        # left at Status S (i.e. not flipped to U/N).
        if skipped:
            with st.expander(f"↪️ {len(skipped)} row(s) will NOT be sent — why", expanded=not changes):
                st.dataframe(
                    pd.DataFrame(skipped, columns=["id", "reason"]),
                    use_container_width=True, hide_index=True)

        if not changes:
            st.info("No rows to send. Set `status` to **U** (update) or **N** (insert) "
                    "on the rows you want to push — see the skipped-rows list above.")
            return

        n_u = sum(c["op"] == "update" for c in changes)
        n_c = sum(c["op"] == "create" for c in changes)
        st.write(f"Detected **{len(changes)}** change(s): "
                 f"{n_u} update (U), {n_c} insert (N) · batch `{batch_no}`")

        with st.expander("Preview the changes to be sent", expanded=True):
            st.dataframe(pd.DataFrame([
                {"op": c["op"], "id": c["id"],
                 "fields": ", ".join(k for k in c["payload"] if k != "batchno")}
                for c in changes
            ]), use_container_width=True, hide_index=True)

        col_a, col_b = st.columns([1, 3])
        with col_a:
            push = st.button("⬆️ Push uploaded changes to Oracle (via ORDS)",
                             type="primary", key=f"{key}_push")
        with col_b:
            workers = st.slider("Parallel requests", 1, 8, 4, key=f"{key}_workers",
                                help="Mirrors VBAFE ParallelUploadRequestCount=4. "
                                     "Drop to 1 if you hit optimistic-lock errors.")
        if push:
            # Live progress bar: advances as each ORDS call completes.
            total = len(changes)
            bar = st.progress(0.0, text=f"Pushing 0/{total}…")

            def _on_progress(done: int, count: int) -> None:
                bar.progress(done / count, text=f"Pushing {done}/{count}…")

            results = push_changes(cfg["resource"], changes,
                                   max_workers=int(workers), progress_cb=_on_progress)
            bar.empty()
            st.session_state[f"{key}_results"] = results

    # --- Per-row results -------------------------------------------------
    results = st.session_state.get(f"{key}_results")
    if results:
        rows = [{
            "Operation": r["op"],
            "ID": r.get("id"),
            "HTTP": r["status"],
            "Result": "Succeeded" if r["ok"] else "Error",
            "Diagnose": _diagnose(r),
        } for r in results]
        res_df = pd.DataFrame(rows)

        ok = int(res_df["Result"].eq("Succeeded").sum())
        bad = len(res_df) - ok
        (st.success if bad == 0 else st.warning)(f"{ok}/{len(res_df)} succeeded")

        st.dataframe(
            res_df, use_container_width=True, hide_index=True,
            column_config={
                "Result": st.column_config.TextColumn(width="small"),
                "Diagnose": st.column_config.TextColumn(width="large"),
            },
        )
        if bad:
            st.caption("Re-fetch from Oracle to confirm the current persisted status of each row.")
