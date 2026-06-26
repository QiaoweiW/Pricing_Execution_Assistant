"""
Pricing Execution Automation (VBCS Generator) page view.

This module provides the Streamlit UI for the VBCS (Value-Based Cost Structure) generator.
It handles four main functions:
1. Fixed Pricing - Generate VBCS files for fixed and quarterly pricing items
2. KS Pricing - Generate VBCS files for Kirkland Signature items
3. Variable Pricing - Generate VBCS files for variable pricing items with Excel automation
4. Combine VBCS - Combine multiple VBCS files into a single file

Key Features:
- File upload and validation
- Processing script execution via subprocess
- Output file caching in session state (5-minute TTL)
- Excel automation error handling and display
- Download functionality for generated VBCS files
- A single SharePoint shortcut to the HTST & ESL PL Pricing Model
  Tracker workbook above the tool picker (full Excel-for-the-Web edit
  in a new tab — see :func:`_render_pricing_model_tracker_link` for
  the rationale on dropping the inline iframe embed).

Author: Pricing Execution Agent Team
Last Updated: 2026-05-08
"""
import streamlit as st
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
from pathlib import Path
from collections import Counter
import traceback
from datetime import datetime, timedelta
from typing import Optional

from data_sources import bid_asset_store as _bid_store
from data_sources import task_manager_store as _task_store
from data_sources import vbcs_refrehable_store as _vbcs_store
from data_sources.bid_asset_store import BidAssetStoreError
from data_sources.task_manager_store import TaskManagerStoreError
from data_sources.vbcs_refrehable_store import VbcsRefrehableStoreError
from utils import notification_helpers as _notify
from utils.notification_helpers import NotificationError
from utils.ui_helpers import apply_custom_css, create_metric_box, safe_error_message
from utils.data_helpers import load_existing_data
from utils.processing_helpers import run_processing_script


# ── SharePoint shortcut to the Pricing Model Tracker workbook ────────────────
#
# We previously embedded the workbook inline via the Office-for-the-Web
# ``?action=embedview`` viewer, but that mode is *read-only* (SharePoint
# blocks in-place editing inside iframes for security), and operators
# overwhelmingly preferred a clean "open in a new tab to edit" flow over
# a tall inline preview that doubled the page's scroll height.  We
# therefore replace the iframe with a single primary link button — full
# interactivity, zero embed pitfalls, much shorter page.
_PRICING_MODEL_TRACKER_SHAREPOINT_URL: str = (
    "https://darigold1com.sharepoint.com/sites/CPPricing2/"
    "Shared%20Documents/General/"
    "Monthly%20and%20Quarterly%20Price%20Updates/"
    "02%20Standard%20Pricing%20Models/Fresh_reference/"
    "HTST%20&%20ESL%20PL%20Pricing%20Model_Tracker_v2.xlsx"
    "?web=1"
)


def _render_pricing_model_tracker_link() -> None:
    """Render the SharePoint shortcut button above the Select Tool divider.

    Layout — a single primary :class:`st.link_button` that opens the
    workbook in a new tab via Excel for the Web with full edit /
    comment / share access.  Sized at the default width (not stretched)
    so it reads as a discrete shortcut rather than a hero CTA.

    Why a link button (not an iframe)
    ---------------------------------
    SharePoint Online blocks in-place editing for iframed Office
    workbooks via its frame-ancestor headers.  Operators reliably ask
    "how do I edit this?" → "click the button below to open in a new
    tab" — so we removed the read-only inline embed entirely and
    promoted the edit-in-SharePoint button to be the only entry point.
    Same destination URL, less visual noise, and the workbook opens
    with full interactivity in the user's existing Microsoft session.
    """
    st.link_button(
        "✏️ Open & Edit in SharePoint (Excel for the Web)",
        _PRICING_MODEL_TRACKER_SHAREPOINT_URL,
        type="primary",
        use_container_width=False,
        help=(
            "Opens the **HTST & ESL PL Pricing Model Tracker** in a new "
            "tab via Excel for the Web for full edit, comment, and share "
            "access. Sign in with your Darigold Microsoft account if "
            "prompted."
        ),
    )


def _store_vbcs_in_cache(output_dataframes):
    """
    Store VBCS output files in session state cache with timestamps.
    
    Args:
        output_dataframes: Dictionary mapping filename to DataFrame
    """
    if 'vbcs_cache' not in st.session_state:
        st.session_state.vbcs_cache = {}
    if 'vbcs_cache_timestamps' not in st.session_state:
        st.session_state.vbcs_cache_timestamps = {}
    
    current_time = datetime.now()
    
    # Store each output file in cache with timestamp
    for filename, df in output_dataframes.items():
        st.session_state.vbcs_cache[filename] = df
        st.session_state.vbcs_cache_timestamps[filename] = current_time


def _cleanup_vbcs_cache():
    """
    Clean up VBCS cache entries older than 5 minutes.
    
    This ensures that output files don't persist indefinitely in session state.
    Files are automatically removed after 5 minutes or when new files are generated.
    """
    if 'vbcs_cache' in st.session_state and 'vbcs_cache_timestamps' in st.session_state:
        current_time = datetime.now()
        cutoff_time = current_time - timedelta(minutes=5)
        files_to_remove = [
            filename for filename, timestamp in st.session_state.vbcs_cache_timestamps.items()
            if timestamp < cutoff_time
        ]
        for filename in files_to_remove:
            if filename in st.session_state.vbcs_cache:
                del st.session_state.vbcs_cache[filename]
            if filename in st.session_state.vbcs_cache_timestamps:
                del st.session_state.vbcs_cache_timestamps[filename]


def _publish_outputs_to_lakehouse(outputs: dict[str, pd.DataFrame]) -> None:
    """Mirror every generated VBCS CSV to OneLake ``VBCS_refrehable/``.

    Called from each of the four VBCS tools (Fixed, KS, Variable,
    Combine) immediately after a successful generation.  The user's
    operating contract is "always overwrite the old copy", so we never
    version-stamp filenames — the latest publish wins.

    Lakehouse failure is treated as non-fatal: the local download
    experience is preserved (the session-state cache is independent),
    but we surface an amber warning so the operator knows the
    downstream Price Book lookup might be reading stale bytes.
    """
    if not outputs:
        return

    publishable = {name: df for name, df in outputs.items() if df is not None and not df.empty}
    if not publishable:
        return

    try:
        result = _vbcs_store.publish_many(publishable)
    except VbcsRefrehableStoreError as exc:
        st.warning(
            f"⚠️ Could not publish VBCS files to lakehouse "
            f"({_vbcs_store.get_folder_label()}): {exc}.  The local "
            "download(s) below still reflect the freshly-generated "
            "data — but the Distribute Price Book lookup may be stale "
            "until the next successful publish."
        )
        return

    written = [name for name, ok in result.items() if ok]
    if written:
        st.caption(
            f"📤 Published {len(written)} VBCS file(s) to "
            f"{_vbcs_store.get_folder_label()}: "
            + ", ".join(f"`{n}`" for n in written)
        )


def _load_latest_bid_df_for_rules():
    """Load the most recent bid asset file for Start-Soon rule evaluation."""
    files = _bid_store.list_bid_files()
    if not files:
        return None
    df, _etag = _bid_store.read_bid_file(files[0].full_path)
    return df


def _run_task_automation_once() -> tuple[int, int]:
    """Run auto-task rule reconciliation + due-soon email reminders.

    Returns
    -------
    tuple[int, int]
        * **rule_delta** — net change from the Start-Soon rolled-up rule:
          ``+1`` if the auto-task was just created, ``-1`` if it was just
          removed (because no Start Soon rows remain), ``0`` if no change.
        * **reminder_emails_sent** — number of distinct assignee summary
          emails sent for tasks due in exactly 5 days.
    """
    rule_delta = 0
    reminders_sent = 0

    bid_df = _load_latest_bid_df_for_rules()
    if bid_df is not None:
        rule_delta = _task_store.sync_start_soon_tasks_from_bid_df(bid_df)

    due_df = _task_store.tasks_due_in_days(days=5)
    if not due_df.empty:
        sent = _notify.send_due_soon_summary(due_df)
        sent_task_ids = [tid for ids in sent.values() for tid in ids]
        _task_store.mark_reminder_sent(sent_task_ids)
        reminders_sent = len(sent)

    return rule_delta, reminders_sent


def _render_task_card(task_row, lane_index: int, lane_count: int) -> None:
    """Render one task card with quick move, edit, and delete actions."""
    task_id = str(task_row["task_id"])
    title = str(task_row.get("title", "")).strip() or "(untitled task)"
    description = str(task_row.get("description", "")).strip()
    assignee = str(task_row.get("assignee_email", "")).strip() or "Unassigned"
    due_date = str(task_row.get("due_date", "")).strip() or "No due date"
    status = str(task_row.get("status", "")).strip()

    with st.container(border=True):
        st.markdown(f"**{title}**")
        st.caption(f"Assignee: {assignee} | Due: {due_date} | Status: {status}")
        if description:
            st.write(description)

        move_left_col, move_right_col, delete_col = st.columns(3)
        if lane_index > 0:
            if move_left_col.button("⬅️ Move", key=f"task_move_left_{task_id}"):
                _task_store.move_task(task_id, _task_store.ALL_STATUSES[lane_index - 1])
                st.rerun()
        if lane_index < lane_count - 1:
            if move_right_col.button("Move ➡️", key=f"task_move_right_{task_id}"):
                _task_store.move_task(task_id, _task_store.ALL_STATUSES[lane_index + 1])
                st.rerun()
        if delete_col.button("🗑️ Delete", key=f"task_delete_{task_id}"):
            _task_store.soft_delete_task(task_id)
            st.rerun()

        with st.expander("Edit task", expanded=False):
            new_title = st.text_input("Title", value=title, key=f"task_title_{task_id}")
            new_desc = st.text_area("Description", value=description, key=f"task_desc_{task_id}")
            parsed_due = pd.to_datetime(task_row.get("due_date"), errors="coerce")
            default_due = (parsed_due.date() if pd.notna(parsed_due) else datetime.now().date())
            new_due = st.date_input("Due date", value=default_due, key=f"task_due_{task_id}")
            new_email = st.text_input("Assignee email", value=str(task_row.get("assignee_email", "")), key=f"task_email_{task_id}")
            new_status = st.selectbox(
                "Status",
                options=list(_task_store.ALL_STATUSES),
                index=list(_task_store.ALL_STATUSES).index(status) if status in _task_store.ALL_STATUSES else 0,
                key=f"task_status_{task_id}",
            )
            if st.button("Save changes", key=f"task_save_{task_id}", type="primary"):
                _task_store.upsert_task(
                    {
                        "task_id": task_id,
                        "title": new_title,
                        "description": new_desc,
                        "assignee_email": new_email,
                        "due_date": new_due.isoformat(),
                        "status": new_status,
                        "source_rule": task_row.get("source_rule"),
                        "source_key": task_row.get("source_key"),
                    }
                )
                st.rerun()


def _render_task_manager_section() -> None:
    """Render the kanban task manager at the bottom of the page."""
    st.markdown("---")
    st.markdown("## Task Manager")
    st.caption(
        "Create and track pricing execution tasks. Completed tasks are automatically "
        "removed after 60 days."
    )

    # Best-effort automatic rule + reminder evaluation while the app is running.
    # This keeps behavior automatic without requiring a separate background worker.
    auto_key = "_task_automation_ran_this_session"
    if not st.session_state.get(auto_key):
        try:
            _run_task_automation_once()
        except (TaskManagerStoreError, BidAssetStoreError, NotificationError):
            # Non-fatal: explicit button below lets operators retry on demand.
            pass
        st.session_state[auto_key] = True

    auto_col, create_col = st.columns([1, 3])
    with auto_col:
        if st.button("Run Rule Sync + Reminders", key="task_auto_run", type="primary"):
            try:
                delta, reminders = _run_task_automation_once()
            except (TaskManagerStoreError, BidAssetStoreError, NotificationError) as exc:
                st.error(f"Automation failed: {exc}")
            else:
                # `delta` is the *net* lifecycle change from the rolled-up rule:
                #   +1 → auto-task created, -1 → auto-task auto-removed (no
                #   more Start Soon rows), 0 → no change (idempotent).
                if delta > 0:
                    rule_msg = "Start-Soon auto-task created."
                elif delta < 0:
                    rule_msg = "Start-Soon auto-task removed (no Start Soon rows remaining)."
                else:
                    rule_msg = "Start-Soon rule already in sync."
                st.success(
                    f"Automation complete. {rule_msg} Sent {reminders} reminder email(s)."
                )
                st.rerun()
    with create_col:
        with st.expander("Start New Task", expanded=False):
            new_title = st.text_input("Task title", key="task_new_title")
            new_desc = st.text_area("Task description", key="task_new_desc")
            new_due = st.date_input("Due date", value=datetime.now().date(), key="task_new_due")
            new_assignee = st.text_input("Assignee email", key="task_new_assignee")
            new_status = st.selectbox("Status", options=list(_task_store.ALL_STATUSES), key="task_new_status")
            if st.button("Create Task", key="task_create_btn", type="primary"):
                if not new_title.strip():
                    st.warning("Task title is required.")
                else:
                    _task_store.upsert_task(
                        {
                            "title": new_title,
                            "description": new_desc,
                            "assignee_email": new_assignee,
                            "due_date": new_due.isoformat(),
                            "status": new_status,
                        }
                    )
                    st.rerun()

    try:
        tasks = _task_store.list_tasks()
    except TaskManagerStoreError as exc:
        st.error(f"Could not load task manager data: {exc}")
        return

    if tasks.empty:
        st.info("No active tasks. Create one from 'Start New Task'.")
        return

    # ── Filters ───────────────────────────────────────────────────────────────
    # Two filters narrow which tasks appear in the kanban below:
    #   • Assignee  — multiselect, including an "(Unassigned)" sentinel for
    #     tasks whose ``assignee_email`` is blank.
    #   • Due within — selectbox; "All open tasks" means no due-date narrowing
    #     and Done tasks always remain visible regardless of due date so they
    #     don't disappear from history.
    filtered_tasks = _apply_task_filters(tasks)
    if filtered_tasks.empty:
        st.info("No tasks match the current filters. Adjust them above to see tasks.")
        return

    # ── Kanban lanes ──────────────────────────────────────────────────────────
    lane_cols = st.columns(3)
    for lane_index, lane_status in enumerate(_task_store.ALL_STATUSES):
        with lane_cols[lane_index]:
            lane_df = filtered_tasks[filtered_tasks["status"] == lane_status].copy()
            st.markdown(f"### {lane_status} ({len(lane_df)})")
            lane_df = lane_df.sort_values(
                by=["due_date", "updated_at"],
                ascending=[True, False],
                na_position="last",
            )
            if lane_df.empty:
                st.caption("No tasks.")
                continue
            for _, row in lane_df.iterrows():
                _render_task_card(row, lane_index, len(_task_store.ALL_STATUSES))


# ── Task Manager filters ──────────────────────────────────────────────────────

# Sentinel used inside the Assignee multiselect for rows with no email so the
# user can include/exclude unassigned tasks just like any other assignee group.
_UNASSIGNED_SENTINEL: str = "(Unassigned)"

# Due-within selectbox options. Mapping value → cutoff in days; ``None`` means
# "do not apply a due-date filter" (i.e. show every task regardless of due).
_DUE_FILTER_OPTIONS: tuple[tuple[str, Optional[int]], ...] = (
    ("All open tasks", None),
    ("Due within 1 day", 1),
    ("Due within 5 days", 5),
    ("Due within 10 days", 10),
)


def _apply_task_filters(tasks: pd.DataFrame) -> pd.DataFrame:
    """Render the Assignee + Due-within filter row and return the narrowed frame.

    Filters are kept entirely UI-local — they never mutate the underlying task
    store. The function preserves the original index/order so the downstream
    sort in ``_render_task_manager_section`` continues to apply.
    """
    # Build the assignee option pool. We map blank emails to a sentinel so the
    # user can explicitly include or exclude unassigned tasks; otherwise the
    # multiselect would silently swallow them.
    emails = tasks["assignee_email"].fillna("").astype(str).str.strip()
    assignee_options = sorted({email if email else _UNASSIGNED_SENTINEL for email in emails})

    flt_assignee, flt_due, _spacer = st.columns([3, 2, 3])
    with flt_assignee:
        # Defensive: drop persisted assignees that no longer exist (e.g. after
        # a task was reassigned or deleted) so the widget never raises.
        key_assignee = "task_filter_assignee"
        if key_assignee in st.session_state:
            preserved = [v for v in st.session_state[key_assignee] if v in assignee_options]
            if preserved != st.session_state[key_assignee]:
                st.session_state[key_assignee] = preserved
        selected_assignees = st.multiselect(
            "Assignee",
            options=assignee_options,
            default=assignee_options,
            key=key_assignee,
            help="Filter tasks by assignee email. '(Unassigned)' covers tasks with no email.",
        )

    with flt_due:
        due_label = st.selectbox(
            "Due within",
            options=[label for label, _ in _DUE_FILTER_OPTIONS],
            index=0,
            key="task_filter_due_within",
            help=(
                "Limits the kanban to open tasks (To Do / In Progress) whose due date "
                "falls within the chosen window. Done tasks are always shown so history "
                "is never hidden."
            ),
        )

    # ── Apply Assignee filter ────────────────────────────────────────────────
    assignee_norm = emails.where(emails != "", _UNASSIGNED_SENTINEL)
    assignee_mask = assignee_norm.isin(selected_assignees)

    # ── Apply Due-within filter (open tasks only; Done is always preserved) ──
    due_cutoff_days: Optional[int] = dict(_DUE_FILTER_OPTIONS)[due_label]
    if due_cutoff_days is None:
        due_mask = pd.Series(True, index=tasks.index)
    else:
        today = datetime.now().date()
        cutoff = today + timedelta(days=due_cutoff_days)
        due_dates = pd.to_datetime(tasks["due_date"], errors="coerce").dt.date
        open_lane_mask = tasks["status"].isin(
            [_task_store.STATUS_TODO, _task_store.STATUS_IN_PROGRESS]
        )
        in_window = (due_dates >= today) & (due_dates <= cutoff)
        # Hide open tasks that fall outside the window; keep all Done tasks.
        due_mask = (~open_lane_mask) | in_window

    return tasks[assignee_mask & due_mask].copy()


def render():
    """
    Render the main Pricing Execution Automation page.
    
    This function:
    - Sets up the page layout and styling
    - Manages session state for tool selection and data caching
    - Handles cache cleanup (removes entries older than 5 minutes)
    - Routes to appropriate tool functions based on user selection
    """
    apply_custom_css()
    
    st.markdown('<h1 class="main-header">Oracle Data Preparation Tool (VBCS Generator)</h1>', unsafe_allow_html=True)
    
    st.markdown("""
    ### Welcome

    This application helps you generate HTST & ESL Private Label VBCS files for Oracle upload. 
    The tool provides four main functions: **Fixed Pricing**, **KS Pricing**, **Variable Pricing**, and **Combine VBCS**.

    **Note**: What's not covered by this tool - custom model such as Bulk Milk (totes & tankers) and KS Organic milk.

    **How to navigate**: Click on any of the buttons below to switch between different tools. 
    Each tool will guide you through the process of uploading required files, running VBCS generation, and downloading the results (in CSV format).
    """)

    # Security notice as a separate, prominent element
    st.warning("🔒 **Security Notice:** For security reasons, all upload and download files will be automatically removed after each processing run.")

    # Load existing data from session state cache (not from persistent disk)
    # Clean up old cache entries first (older than 5 minutes)
    _cleanup_vbcs_cache()
    
    # Load from session state cache
    data_files = st.session_state.get('vbcs_cache', {})
    output_dir = Path("data")  # Keep for compatibility, but files are in cache

    # Pricing Model Tracker shortcut — sits ABOVE the tool-picker
    # divider so the operator can jump out to SharePoint to edit the
    # workbook in Excel for the Web without scrolling past every tool
    # below.  A single link button (not an inline embed) keeps the
    # page short and avoids SharePoint's iframe edit restrictions —
    # see :func:`_render_pricing_model_tracker_link`.
    _render_pricing_model_tracker_link()

    # Tool selection at the top
    st.markdown("---")
    st.markdown('<h2 style="font-size: 1.8rem; color: #1f77b4; margin-bottom: 1rem;">Select Tool</h2>', unsafe_allow_html=True)

    # Create columns for tool selection with better visuals
    col1, col2, col3, col4 = st.columns(4)

    with col1:
        if st.button("**Fixed Pricing**\n\nClick to generate VBCS files for Fixed and Quarterly pricing items", width='stretch', type="primary", key="fixed_btn"):
            st.session_state.selected_tool = "Fixed Pricing"

    with col2:
        if st.button("**KS Pricing**\n\nClick to generate VBCS files for Kirkland Signature items", width='stretch', type="primary", key="ks_btn"):
            st.session_state.selected_tool = "KS Pricing"

    with col3:
        if st.button("**Variable Pricing**\n\nClick to generate VBCS files for variable pricing items", width='stretch', type="primary", key="var_btn"):
            st.session_state.selected_tool = "Variable Pricing"

    with col4:
        if st.button("**Combine VBCS**\n\nClick to combine all VBCS files into one", width='stretch', type="primary", key="combine_btn"):
            st.session_state.selected_tool = "Combine VBCS"

    # Initialize session state if not exists
    if 'selected_tool' not in st.session_state:
        st.session_state.selected_tool = "Fixed Pricing"

    st.markdown("---")

    # Main content area
    if st.session_state.selected_tool == "Fixed Pricing":
        run_fixed_pricing(data_files)
    elif st.session_state.selected_tool == "KS Pricing":
        run_ks_pricing(data_files)
    elif st.session_state.selected_tool == "Variable Pricing":
        run_variable_pricing(data_files)
    elif st.session_state.selected_tool == "Combine VBCS":
        run_combine_vbcs(data_files)

    # Bottom section: shared execution task manager
    _render_task_manager_section()


def run_fixed_pricing(data_files):
    """Run Fixed Pricing VBCS Generation"""
    
    # Monthly update type selection
    st.subheader("Select Month Type")
    update_type = st.radio(
        "Select the current month type:",
        ["Quarterly Update Month", "Non-Quarterly Update Month"],
        help="Choose whether this is a quarterly update month or not. This affects which rows are included in the output."
    )
    
    # Show reminder message for quarterly updates
    if update_type == "Quarterly Update Month":
        # Reminder message - no emoji, plain text
        st.markdown("**Reminder**: As a reminder, the VBCS generation for quarterly pricing is managed in Excel. Please locate the correct file (e.g., \"KS Organic Price Build\") and navigate to the \"VBCS\" tab there.")
    
    # How to Use section
    st.subheader("How to Use")
    st.markdown("""
    This tool generates VBCS files for fixed pricing items based on market index and quarterly pricing.
    
    **What it does:**
    - Filters items with 'Fixed' or 'Quarterly' market index names
    - Excludes items starting with 'DG'
    - Applies effective dates from assumptions file
    - Generates VBCS format output for Oracle upload
    
    **Reminder - all csv files should be UTF-8 format**
    """)
    
    # Upload & Run section
    st.subheader("Upload & Run")
    
    col1, col2 = st.columns(2)
    
    with col1:
        price_build_file = st.file_uploader(
            "Upload Old_Price_Build.csv",
            type=['csv'],
            help="Price Build Report file"
        )
    
    with col2:
        assumptions_file = st.file_uploader(
            "Upload Effective_Date_Assumptions.csv",
            type=['csv'],
            help="Effective Date Assumptions file"
        )
    
    if st.button("Run Fixed Pricing Generation", type="primary"):
        if price_build_file is not None and assumptions_file is not None:
            with st.spinner("Processing Fixed Pricing data..."):
                # Prepare uploaded files
                uploaded_files = {
                    "Old_Price_Build.csv": price_build_file.getvalue(),
                    "Effective_Date_Assumptions.csv": assumptions_file.getvalue()
                }
                
                # Create output directory
                output_dir = Path("data")
                output_dir.mkdir(exist_ok=True)
                
                # Run the processing script
                success, message, output_dataframes = run_processing_script("Fixed_Pricing_VBCS", uploaded_files, output_dir)
                # Store output in cache if available (for consistency, though Fixed Pricing may not use cache)
                if success and output_dataframes:
                    if 'vbcs_cache' not in st.session_state:
                        st.session_state.vbcs_cache = {}
                    if 'vbcs_cache_timestamps' not in st.session_state:
                        st.session_state.vbcs_cache_timestamps = {}
                    from datetime import datetime
                    current_time = datetime.now()
                    for filename, df in output_dataframes.items():
                        st.session_state.vbcs_cache[filename] = df
                        st.session_state.vbcs_cache_timestamps[filename] = current_time
                    # Mirror every generated CSV to OneLake so the
                    # Distribute Price Book lookup always reads the
                    # latest published rates.
                    _publish_outputs_to_lakehouse(output_dataframes)
                
                if success:
                    st.success(f"Success: {message}")
                    st.rerun()  # Refresh the page to show new data
                else:
                    # Safely display error message (handle Unicode encoding issues)
                    safe_msg = safe_error_message(message)
                    st.error(f"Error: {safe_msg}")
        else:
            st.warning("Please upload both required files before running the generation.")
    
    # Download Output section
    st.subheader("Download Output")
    
    if "fixed_vbcs.csv" in data_files:
        df = data_files["fixed_vbcs.csv"]
        
        # Apply filtering based on update type
        if update_type == "Quarterly Update Month":
            # Remove rows where Market column contains "Quarterly"
            if 'Market' in df.columns:
                original_count = len(df)
                df_filtered = df[~df['Market'].str.contains('Quarterly', na=False)]
                filtered_count = len(df_filtered)
                st.info(f"📊 Quarterly Update Month: Removed {original_count - filtered_count} rows containing 'Quarterly' in Market column. {filtered_count} rows remaining.")
                df = df_filtered
            else:
                st.warning("'Market' column not found in data. No filtering applied.")
        else:
            # Non-Quarterly Update Month - keep all data as is
            st.info("Non-Quarterly Update Month: All data included (no filtering applied).")
        
        # Download button
        csv = df.to_csv(index=False)
        st.download_button(
            label="Download Fixed Pricing VBCS",
            data=csv,
            file_name="fixed_vbcs.csv",
            mime="text/csv"
        )
    else:
        st.info("No data available for download. Please run the generation first.")


def run_ks_pricing(data_files):
    """Run KS Pricing VBCS Generation"""
    
    # How to Use section
    st.subheader("How to Use")
    st.markdown("""
    This tool generates VBCS files for KS (Kirkland Signature) items with Costco-specific pricing.
    
    **What it does:**
    - Filters KS items from Price Build Report
    - Matches with Costco region-specific pricing
    - Applies CLASS market index filtering
    - Generates VBCS format for both EA and CA UOMs
    
    **Reminder - all csv files should be UTF-8 format**
    """)
    
    # Upload & Run section
    st.subheader("Upload & Run")
    
    col1, col2, col3, col4 = st.columns(4)
    
    with col1:
        costco_prices_file = st.file_uploader(
            "Upload Costco_HTST_Pricing.csv",
            type=['csv'],
            help="Costco pricing data file"
        )
    
    with col2:
        price_build_file = st.file_uploader(
            "Upload Old_Price_Build.csv",
            type=['csv'],
            help="Price Build Report file"
        )
    
    with col3:
        regions_file = st.file_uploader(
            "Upload Costco_HTST_Region_Lookup.csv",
            type=['csv'],
            help="Costco regions lookup file"
        )
    
    with col4:
        assumptions_file = st.file_uploader(
            "Upload Effective_Date_Assumptions.csv",
            type=['csv'],
            help="Effective Date Assumptions file"
        )
    
    if st.button("Run KS Pricing Generation", type="primary"):
        if costco_prices_file is not None and price_build_file is not None and regions_file is not None and assumptions_file is not None:
            with st.spinner("Processing KS Pricing data..."):
                # Prepare uploaded files
                uploaded_files = {
                    "Costco_HTST_Pricing.csv": costco_prices_file.getvalue(),
                    "Old_Price_Build.csv": price_build_file.getvalue(),
                    "Costco_HTST_Region_Lookup.csv": regions_file.getvalue(),
                    "Effective_Date_Assumptions.csv": assumptions_file.getvalue()
                }
                
                # Create output directory
                output_dir = Path("data")
                output_dir.mkdir(exist_ok=True)
                
                # Run the processing script
                success, message, output_dataframes = run_processing_script("KS_Pricing_VBCS", uploaded_files, output_dir)
                # Store output in cache if available (for consistency, though KS Pricing may not use cache)
                if success and output_dataframes:
                    if 'vbcs_cache' not in st.session_state:
                        st.session_state.vbcs_cache = {}
                    if 'vbcs_cache_timestamps' not in st.session_state:
                        st.session_state.vbcs_cache_timestamps = {}
                    from datetime import datetime
                    current_time = datetime.now()
                    for filename, df in output_dataframes.items():
                        st.session_state.vbcs_cache[filename] = df
                        st.session_state.vbcs_cache_timestamps[filename] = current_time
                    # Mirror every generated CSV to OneLake (see
                    # _publish_outputs_to_lakehouse for the rationale).
                    _publish_outputs_to_lakehouse(output_dataframes)
                
                if success:
                    st.success(f"Success: {message}")
                    st.rerun()  # Refresh the page to show new data
                else:
                    # Safely display error message (handle Unicode encoding issues)
                    safe_msg = safe_error_message(message)
                    st.error(f"Error: {safe_msg}")
        else:
            st.warning("Please upload all 4 required files before running the generation.")
    
    # Download Output section
    st.subheader("Download Output")
    
    if "ks_htst_vbcs.csv" in data_files:
        df = data_files["ks_htst_vbcs.csv"]
        
        # Download button
        csv = df.to_csv(index=False)
        st.download_button(
            label="Download KS Pricing VBCS",
            data=csv,
            file_name="ks_htst_vbcs.csv",
            mime="text/csv"
        )
    else:
        st.info("No data available for download. Please run the generation first.")


def run_variable_pricing(data_files):
    """
    Run Variable Pricing VBCS Generation.
    
    This function:
    - Handles file uploads for Variable Pricing processing
    - Executes the Variable_Pricing_VBCS processing script
    - Stores output files in session state cache (5-minute TTL)
    - Displays Excel automation status and errors
    - Provides download functionality for generated VBCS files
    
    Args:
        data_files: Dictionary of existing data files (for compatibility, not used for Variable Pricing)
    """
    
    # How to Use section
    st.subheader("How to Use")
    st.markdown("""
    This tool turns your monthly execution data into Oracle-ready VBCS price files for
    variable (market-based) pricing items. Upload the 5 files below, click **Run**, and the
    tool produces **three** downloadable CSV files — one for **URM/TOPCO**, one for **Winco**,
    and one for everyone else (**Batch**).

    **Step 1 — Build the prices (happens automatically when you click Run):**
    1. Your execution prices are matched to each item's units of measure (EA, CA, ST, PL, BC),
       so every item gets a price for each pack size.
    2. Each price is rounded using that item's rounding rule.
    3. An effective start/end date is attached based on the *Effective Date Assumptions* file.
    4. A market index name (e.g., the milk market it follows) is attached from the *Milk Market Index* file.
    5. Everything is reshaped into the exact column layout Oracle expects for a VBCS upload.

    **Step 2 — How the URM and Winco lists are generated (the "cross-dock" logic):**

    URM and Winco are special: instead of pricing every store individually, we price **one
    representative store** and then copy that price list out to all of their other stores.
    The tool reads the *Customer Extract Report* to know which stores exist.
    - **Winco:** the prices set for the store **`WINCO 002 KENNEWICK DSD`** are copied to *every*
      other Winco DSD store found in the Customer Extract Report.
    - **URM/TOPCO:** the prices set for the store **`TOWN PUMP`** are copied to *every* other
      URM/TOPCO store, **except** the Spokane warehouse sites (`URM WHSE SPOKANE` /
      `URM WHSE SPOKANE HTST`) and a small list of customers that don't belong on this list.

    The result is split by customer name into the three files: any customer containing "URM" or
    "TOPCO" → **URM file**, any containing "WINCO" → **Winco file**, and everything else → **Batch file**.
    Duplicate rows are removed automatically. The files appear under **Download Output** as soon as
    Step 1–2 finish — you can download them right away.

    **Step 3 — Emailing the URM & Winco price sheets (optional, Windows only):**

    After the CSVs are generated, you can click **"Receive URM & Winco DSD sheets for Customer
    Distribution"** under Download Output. For each of URM and Winco, the tool opens the matching
    macro-enabled Excel template, drops the new prices into it, refreshes the workbook, and lets
    the workbook email itself out:
    - **URM template** runs: `Step1_UpdateData` → paste prices → `Step2_SaveNewMonthasValues` → `Step3_SendPreparedEmail`
    - **Winco template** runs: `Step1_RollForwardData` → paste prices → `Step2_ExportCleanVersion` → `Step3_EmailPriceList`

    This email step needs **Windows + Microsoft Excel + Outlook (open and signed in)**, and the
    `pywin32` library (the tool installs it for you if it's missing). If it can't run — for example
    on a non-Windows server — the CSV files are still generated and downloadable; only the
    automatic email is skipped.

    **Reminder - all csv files should be UTF-8 format**
    """)
    
    # Upload & Run section
    st.subheader("Upload & Run")
    
    col1, col2, col3, col4, col5 = st.columns(5)
    
    with col1:
        execution_file = st.file_uploader(
            "Upload Execution_final.csv",
            type=['csv'],
            help="Execution data file"
        )
    
    with col2:
        uom_file = st.file_uploader(
            "Upload HTST Pricing_UOMS_v1.csv",
            type=['csv'],
            help="UOM data file"
        )
    
    with col3:
        market_file = st.file_uploader(
            "Upload Milk_Market_Index.csv",
            type=['csv'],
            help="Market index file"
        )
    
    with col4:
        dates_file = st.file_uploader(
            "Upload Effective_Date_Assumptions.csv",
            type=['csv'],
            help="Effective dates file"
        )
    
    with col5:
        customer_report_file = st.file_uploader(
            "Upload Customer_Extract_Report.csv",
            type=['csv'],
            help="Customer report file for cross-dock logic"
        )
    
    if st.button("Run Variable Pricing Generation", type="primary"):
        if execution_file is not None and uom_file is not None and dates_file is not None and market_file is not None and customer_report_file is not None:
            with st.spinner("Processing Variable Pricing data..."):
                # Prepare uploaded files
                uploaded_files = {
                    "Execution_final.csv": execution_file.getvalue(),
                    "HTST Pricing_UOMS_v1.csv": uom_file.getvalue(),
                    "Effective_Date_Assumptions.csv": dates_file.getvalue(),
                    "Milk_Market_Index.csv": market_file.getvalue(),
                    "Customer_Extract_Report.csv": customer_report_file.getvalue()
                }
                
                # Create output directory
                output_dir = Path("data")
                output_dir.mkdir(exist_ok=True)
                
                # Run the processing script (generates CSV files first, then runs Excel automation automatically)
                success, message, output_dataframes = run_processing_script("Variable_Pricing_VBCS", uploaded_files, output_dir)
                
                if success:
                    # Store output files in session state immediately after CSV generation
                    # CSV files are generated first in the script, so they're available right away
                    if output_dataframes:
                        _store_vbcs_in_cache(output_dataframes)
                        _cleanup_vbcs_cache()
                        # Mirror every generated CSV to OneLake's
                        # VBCS_refrehable folder so the Distribute Price
                        # Book lookup always reads the latest published
                        # rates (see _publish_outputs_to_lakehouse).
                        _publish_outputs_to_lakehouse(output_dataframes)
                    
                    # Check for Excel automation errors in the message (from stdout/stderr)
                    has_excel_error = ("ERROR: Failed to process URM custom sheet" in message or 
                                      "ERROR: Failed to process Winco custom sheet" in message or 
                                      "Excel automation error" in message or 
                                      "Failed to process" in message or
                                      "⚠️ Excel Automation Warnings" in message)
                    
                    # Check if macros completed but email might not have been sent
                    has_email_warning = ("NOTE: If you did not receive an email" in message or
                                        "INFO: Email should have been sent" in message)
                    
                    if has_excel_error:
                        st.success("✅ VBCS files generated successfully and available for download!")
                        st.warning("⚠️ Excel automation encountered issues. VBCS files are available for download, but email automation did not complete.")
                        with st.expander("🔍 Excel Automation Debug Information", expanded=True):
                            # Extract error details from message
                            error_lines = [line for line in message.split('\n') if 'ERROR' in line.upper() or 'Excel' in line or 'macro' in line.lower() or 'Failed' in line]
                            if error_lines:
                                st.text("Error Details:")
                                for line in error_lines:
                                    st.text(line)
                            else:
                                st.text("Full output:")
                                st.text(message)
                            st.markdown("**Common issues and solutions:**")
                            st.markdown("""
                            1. **Excel template file not found**: Verify the file exists at the expected location
                            2. **Macros disabled**: Enable macros in Excel security settings
                            3. **Macros not present**: 
                               - URM: Ensure macros Step1_UpdateData, Step2_SaveNewMonthasValues, Step3_SendPreparedEmail exist
                               - Winco: Ensure macros Step1_RollForwardData, Step2_ExportCleanVersion, Step3_EmailPriceList exist
                            4. **pywin32 not installed**: The script will attempt to auto-install, but you may need to install manually: `pip install pywin32`
                            5. **Excel file open**: Close the Excel file if it's open in another application
                            6. **Email not configured**: Check that email settings are configured in the Excel macros
                            7. **Outlook not running**: Ensure Outlook is installed, configured, and running
                            8. **Email in spam/junk**: Check your spam/junk folder for the email
                            """)
                    elif has_email_warning:
                        st.success("✅ VBCS files generated successfully and available for download!")
                        st.info("📧 Excel automation completed. If you did not receive an email, please check:")
                        st.markdown("""
                        - **Outlook is running**: The email macros require Outlook to be open and configured
                        - **Check Sent Items**: Verify the email was sent by checking Outlook's Sent Items folder
                        - **Email address**: Ensure your email address is in the macro's recipient list
                        - **Spam folder**: Check your spam/junk folder
                        - **Email settings**: Verify email settings in the Excel macros are correct
                        """)
                        # Show the success message with email note
                        st.success(f"Success: {message.split('NOTE:')[0].strip()}")
                    else:
                        st.success("✅ VBCS files generated successfully and available for download!")
                        st.success(f"Success: {message}")
                    
                    st.rerun()  # Refresh the page to show new data
                else:
                    # Safely display error message (handle Unicode encoding issues)
                    safe_msg = safe_error_message(message)
                    st.error(f"Error: {safe_msg}")
                    # Show detailed error information in the browser
                    with st.expander("🔍 Debug Information", expanded=True):
                        st.text(f"Error Details: {message}")
                        st.text("This error usually indicates an encoding issue with one of your CSV files.")
                        st.text("Please check that all your CSV files are saved with proper encoding (UTF-8 recommended).")
                        st.text("The Customer_Extract_Report.csv file was detected as having special characters.")
        else:
            st.warning("Please upload all 5 required files before running the generation.")
    
    # Download Output section
    st.subheader("Download Output")
    
    # Check for variable pricing files in session state cache
    variable_files = {
        "URM/TOPCO": "urm_vbcs.csv",
        "Winco": "winco_vbcs.csv",
        "Batch": "batch_vbcs.csv"
    }
    
    # Load from session state cache (not from disk)
    cache_data_files = st.session_state.get('vbcs_cache', {})
    available_files = {name: file_name for name, file_name in variable_files.items() if file_name in cache_data_files}
    
    if available_files:
        # Display download buttons for each file from cache
        for name, file_name in available_files.items():
            try:
                df = cache_data_files[file_name]
                csv = df.to_csv(index=False)
                st.download_button(
                    label=f"Download {name} VBCS",
                    data=csv,
                    file_name=f"{name.lower()}_vbcs.csv",
                    mime="text/csv"
                )
            except Exception as e:
                safe_msg = safe_error_message(e)
                st.error(f"Error loading {name} data: {safe_msg}")
        
        # New button for Excel automation (only show if URM or Winco files exist)
        st.markdown("---")
        st.markdown("### 📧 Receive Emails about URM & Winco DSD Sheets")
        
        has_urm_or_winco = "urm_vbcs.csv" in cache_data_files or "winco_vbcs.csv" in cache_data_files
        
        if has_urm_or_winco:
            if st.button("Receive URM & Winco DSD sheets for Customer Distribution", type="primary"):
                with st.spinner("Running Excel automation and sending email notifications..."):
                    # Create output directory
                    output_dir = Path("data")
                    output_dir.mkdir(exist_ok=True)
                    
                    # Run Excel automation script
                    # Use empty uploaded_files since we only need to run Excel automation
                    success, message, _ = run_processing_script("Variable_Pricing_VBCS", {}, output_dir, excel_automation=True)
                    
                    if success:
                        # Check for Excel automation errors
                        has_excel_error = ("ERROR: Failed to process URM custom sheet" in message or 
                                          "ERROR: Failed to process Winco custom sheet" in message or 
                                          "Excel automation error" in message or 
                                          "Failed to process" in message)
                        
                        has_email_warning = ("NOTE: If you did not receive an email" in message or
                                            "INFO: Email should have been sent" in message)
                        
                        if has_excel_error:
                            st.warning("⚠️ Excel automation encountered issues.")
                            with st.expander("🔍 Excel Automation Debug Information", expanded=True):
                                error_lines = [line for line in message.split('\n') if 'ERROR' in line.upper() or 'Excel' in line or 'macro' in line.lower() or 'Failed' in line]
                                if error_lines:
                                    st.text("Error Details:")
                                    for line in error_lines:
                                        st.text(line)
                                else:
                                    st.text("Full output:")
                                    st.text(message)
                                st.markdown("**Common issues and solutions:**")
                                st.markdown("""
                                1. **Excel template file not found**: Verify the file exists at the expected location
                                2. **Macros disabled**: Enable macros in Excel security settings
                                3. **Macros not present**: 
                                   - URM: Ensure macros Step1_UpdateData, Step2_SaveNewMonthasValues, Step3_SendPreparedEmail exist
                                   - Winco: Ensure macros Step1_RollForwardData, Step2_ExportCleanVersion, Step3_EmailPriceList exist
                                4. **pywin32 not installed**: The script will attempt to auto-install, but you may need to install manually: `pip install pywin32`
                                5. **Excel file open**: Close the Excel file if it's open in another application
                                6. **Email not configured**: Check that email settings are configured in the Excel macros
                                7. **Outlook not running**: Ensure Outlook is installed, configured, and running
                                8. **Email in spam/junk**: Check your spam/junk folder for the email
                                """)
                        elif has_email_warning:
                            st.success("✅ Excel automation completed!")
                            st.info("📧 If you did not receive an email, please check:")
                            st.markdown("""
                            - **Outlook is running**: The email macros require Outlook to be open and configured
                            - **Check Sent Items**: Verify the email was sent by checking Outlook's Sent Items folder
                            - **Email address**: Ensure your email address is in the macro's recipient list
                            - **Spam folder**: Check your spam/junk folder
                            - **Email settings**: Verify email settings in the Excel macros are correct
                            """)
                        else:
                            st.success("✅ Excel automation completed and emails sent successfully!")
                            st.info("📧 Please check your inbox for the URM and Winco DSD sheets.")
                        
                        st.info("""
                        **Next Steps:**
                        - Review the custom sheets with a focus on comparing $unit price changes against the mover file.
                        - If no further changes are needed, proceed with sending these files out to customers.
                        - Customer contact information can be found here: [Customer Contact Information](https://darigold1com.sharepoint.com/:t:/r/sites/CPPricing2/Shared%20Documents/General/Monthly%20and%20Quarterly%20Price%20Updates/03%20Custom%20Pricing%20Models/Customer%20Contact_URM%20%26%20Winco.txt?csf=1&web=1&e=Bel9Gr).
                        - If revisions are required, please first update execution_final to ensure correct VBCS formats are generated.
                        """)
                    else:
                        safe_msg = safe_error_message(message)
                        st.error(f"Error running Excel automation: {safe_msg}")
                        with st.expander("🔍 Debug Information", expanded=True):
                            st.text(f"Error Details: {message}")
        else:
            st.info("URM or Winco VBCS files are required to run Excel automation. Please ensure CSV files are generated first.")
    else:
        st.info("No data available for download. Please run the generation first.")


def run_combine_vbcs(data_files):
    """Run VBCS File Combination"""
    
    # How to Use section
    st.subheader("How to Use")
    st.markdown("""
    This tool combines all generated VBCS files into a single comprehensive file to enable efficient validation of upload success. The VBCS files that can be uploaded include: 1) fixed_vbcs, 2) ks_htst_vbcs, 3) batch_vbcs, 4) urm_topco_vbcs, 5) winco_vbcs, 6) bulk_vbcs, 7) walmart_vbcs, 8) us_foods_vbcs, 9) ks_organic_vbcs. All csv files should be UTF-8 format.
    """)
    
    # Upload & Run section
    st.subheader("Upload & Run")
    
    # Multiple file uploader
    uploaded_files = st.file_uploader(
        "Upload VBCS files to combine",
        type=['csv'],
        accept_multiple_files=True,
        help="Upload one or more VBCS CSV files. Supported files: fixed_vbcs, ks_htst_vbcs, batch_vbcs, urm_topco_vbcs, winco_vbcs, bulk_vbcs, walmart_vbcs, us_foods_vbcs, ks_organic_vbcs"
    )
    
    if st.button("Run Combine VBCS Generation", type="primary"):
        if uploaded_files and len(uploaded_files) > 0:
            with st.spinner("Combining VBCS files..."):
                try:
                    # Create output directory
                    output_dir = Path("data")
                    output_dir.mkdir(exist_ok=True)
                    
                    # Clean up existing combined file before creating new one
                    combined_file_path = output_dir / "combined_all_vbcs.csv"
                    if combined_file_path.exists():
                        combined_file_path.unlink()
                    
                    # Process all uploaded files
                    combined_dfs = []
                    for file_obj in uploaded_files:
                        try:
                            # Read the uploaded file
                            df = pd.read_csv(file_obj)
                            
                            # Keep only the first 21 columns
                            original_column_count = len(df.columns)
                            if original_column_count > 21:
                                df = df.iloc[:, :21]
                                st.info(f"{file_obj.name}: Kept first 21 columns out of {original_column_count} total columns")
                            
                            df['Source_File'] = file_obj.name  # Add source file column
                            combined_dfs.append(df)
                            st.success(f"Loaded {file_obj.name}: {len(df)} records")
                        except Exception as e:
                            safe_msg = safe_error_message(e)
                            st.error(f"Error loading {file_obj.name}: {safe_msg}")
                            continue  # Continue with other files instead of returning
                    
                    if combined_dfs:
                        # Concatenate all dataframes
                        combined_df = pd.concat(combined_dfs, ignore_index=True)
                        
                        # Remove duplicates
                        combined_df = combined_df.drop_duplicates()
                        
                        st.info(f"Combined data: {len(combined_df)} records with {len(combined_df.columns)} columns (first 21 columns from each file)")
                        
                        # Save to data directory
                        output_path = output_dir / "combined_all_vbcs.csv"
                        combined_df.to_csv(output_path, index=False)

                        # Mirror the combined CSV to OneLake so the
                        # Distribute Price Book lookup can see it.
                        _publish_outputs_to_lakehouse({"combined_all_vbcs.csv": combined_df})

                        # Verify the file was saved
                        if output_path.exists():
                            file_size = output_path.stat().st_size
                            st.success(f"Success: Successfully combined {len(combined_dfs)} files into {len(combined_df)} records!")
                            st.info(f"Combined file saved: {output_path} ({file_size} bytes)")
                            
                            # Force reload the data to include the new combined file
                            st.info("Reloading data to include combined file...")
                            data_files, output_dir = load_existing_data()
                            
                            # Verify the combined file is now in data_files
                            if "combined_all_vbcs.csv" in data_files:
                                st.success("Combined file successfully loaded and available for download!")
                            else:
                                st.warning("Combined file saved but not loaded. Please refresh the page.")
                        else:
                            st.error(f"Error: Failed to save combined file to {output_path}")
                        
                        st.rerun()  # Refresh the page to show new data
                    else:
                        st.error("Error: No VBCS files could be processed successfully.")
                        
                except Exception as e:
                    safe_msg = safe_error_message(e)
                    st.error(f"Error: {safe_msg}")
        else:
            st.warning("Please upload at least one VBCS file before running the combination.")
    
    # Download Output section
    st.subheader("Download Output")
    
    # Debug: Show what files are available
    if data_files:
        st.info(f"Available data files: {list(data_files.keys())}")
    else:
        st.info("No data files loaded")
    
    if "combined_all_vbcs.csv" in data_files:
        df = data_files["combined_all_vbcs.csv"]
        
        # Download button
        csv = df.to_csv(index=False)
        st.download_button(
            label="Download Combined VBCS",
            data=csv,
            file_name="combined_all_vbcs.csv",
            mime="text/csv"
        )
    else:
        st.info("No combined data available for download. Please run the combination first.")

