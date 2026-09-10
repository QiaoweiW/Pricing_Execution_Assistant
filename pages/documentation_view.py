"""
Documentation page view — the app's landing page and its single sign-in point.

Three jobs, in this order:

1. **Microsoft Fabric sign-in.**  This is the ONLY place in the app that
   renders the device-code sign-in flow.  Every other view detects the
   session and points here, so the panel stays at the top of this page and
   this page stays first in the sidebar.
2. **The manual.**  How to drive each view (click by click), the exact
   arithmetic behind every number the app publishes (so anyone can rebuild
   it in Excel), and a clickable index of the lakehouse folders and files
   each view reads from.
3. **A Word copy of all of it.**  One button hands the whole manual over as
   a ``.docx`` so it can be sent to someone who has no access to the app.

Why the content is data, not prose
----------------------------------
The page bodies below are built from module-level tuples
(:data:`_PAGE_GUIDES`, :data:`_FORMULAS`, :data:`_LAKEHOUSE_PATHS`,
:data:`_LAKEHOUSE_FILES`) rather than one long ``st.markdown`` blob.  Adding a
page, a formula or a folder is a one-entry edit, the render functions never
change, and nothing can drift out of alignment between the sections.  It also
keeps every Fabric URL in exactly one place — see :data:`_LAKEHOUSE_BASE`.

Crucially, it is also what makes the Word export honest: :func:`_build_manual_docx`
walks the *same* tuples the page renders, so the download can never describe a
different app from the one on screen.
"""
from __future__ import annotations

from datetime import date
from typing import NamedTuple
from urllib.parse import quote

import streamlit as st

from utils import fabric_signin_widget
from utils.ui_helpers import apply_custom_css
from utils import docx_export


# ── Fabric deep-links ─────────────────────────────────────────────────────────
#
# Workspace + lakehouse GUIDs of the B2C pricing lakehouse — the one the app
# reads Activity Model, Finance, PDH, IBP, movers and VBCS from.  Same pair
# that ``.streamlit/secrets.toml -> [fabric_htst]`` points at; kept as literals
# here because this page must render its links whether or not secrets loaded.
_LAKEHOUSE_BASE: str = (
    "https://app.fabric.microsoft.com/groups/"
    "bb11c51d-03c8-4f1b-938c-e20657a8f31d/lakehouses/"
    "a01f513d-eee7-41eb-8c15-670bc40e7fc8?experience=fabric-developer"
)


def _lakehouse_url(path: str = "") -> str:
    """Return a deep-link into ``Files/<path>`` of the pricing lakehouse.

    Passing no *path* returns the lakehouse root.  The path is URL-quoted
    (``safe=""`` so ``/`` becomes ``%2F`` too) because Fabric expects
    ``selectedPath`` as a single opaque, fully-escaped value — an unescaped
    slash or space silently lands the user on the lakehouse root instead of
    the folder they clicked.
    """
    if not path:
        return _LAKEHOUSE_BASE
    return f"{_LAKEHOUSE_BASE}&selectedPath={quote(f'Files/{path}', safe='')}"


# Reports embedded elsewhere in the app, surfaced here so every Fabric
# destination the app can reach is reachable from this one page.
_VELOCITY_REPORT_URL: str = (
    "https://app.fabric.microsoft.com/groups/"
    "41da47a8-8733-40a0-9764-826d9d7df90d/reports/"
    "80cefdf7-9fe4-4f10-8231-6c7a66595a87/"
    "270796e12490916b5002?experience=fabric-developer"
)
_FINANCE_PNL_REPORT_URL: str = (
    "https://app.powerbi.com/groups/me/reports/"
    "ff2d4ea3-d3e4-4a14-945d-998bb7a7f03d/ef0f92c30868546c301b"
    "?ctid=c9a55ced-3b88-408c-ab99-8db8b9b90286&experience=power-bi"
)


# ── Section 1: how to drive each page ────────────────────────────────────────


class _PageGuide(NamedTuple):
    """One sidebar page, explained from a standing start."""
    name: str            # sidebar label, verbatim
    one_liner: str       # what it is for, in one sentence
    steps: tuple         # numbered click-by-click instructions
    needs_fabric: bool   # True → sign in on this page first
    gotcha: str = ""     # the single thing people get wrong


# Order matches the sidebar exactly: the landing page, then the daily-use
# views alphabetically, then the two pinned to the bottom by
# ``streamlit_app.NAV_PINNED_LAST``.  Reorder the sidebar and reorder this.
_PAGE_GUIDES: tuple = (
    _PageGuide(
        name="Documentation",
        one_liner="This page. Sign in to Microsoft Fabric, then read how "
                  "everything else works.",
        steps=(
            "Look at the box at the top. If it says you are connected, you "
            "are done — do not click anything.",
            "If it asks you to sign in, click the sign-in button, then follow "
            "the link it shows and type the code it gives you.",
            "Come back to this tab. The box turns green on its own.",
            "Now click any page in the left sidebar. You will not be asked to "
            "sign in again.",
        ),
        needs_fabric=False,
        gotcha="You only sign in once per session. If a page says it has no "
               "data, come back here and check the box is still green.",
    ),
    _PageGuide(
        name="Demand Planner Analytics",
        one_liner="The demand plan: what we said we would sell, what we "
                  "actually sold, and where the two disagree.",
        steps=(
            "**IBP Cadence and Supporting files** is just a list of links — "
            "the checklist, the change journal, the Power BI reports. Click "
            "and go.",
            "**Business Health** is the executive glance: the plan measured "
            "against customer orders, green or red. Open the reconciliation "
            "box inside it to see exactly which files fed a number.",
            "**RO Comparison** is a four-step flow. Upload the Distribution "
            "Tracker in Step 1; the report builds itself in Step 2; Step 3 "
            "shows what moved and which items; Step 4 is where you go when "
            "something needs fixing.",
            "**Demand Summary (APS / Oracle)** is the same four steps for the "
            "Oracle plan. Each cycle needs two uploads — the APS bulk export "
            "and the RO_Seed — and Step 2 tells you if one is still missing.",
            "**Demand Summary (IBP)** is the same four steps again for the "
            "IBP base plan. Forecast accuracy — bias, WMAPE, the driver "
            "tables — lives inside its Step 3.",
            "**Velocity Analysis** is an embedded Fabric report. Click "
            "*Load Velocity Analysis report* once and it stays loaded.",
        ),
        needs_fabric=True,
        gotcha="Sections load only when you ask them to — that is why the "
               "page opens fast, and a section you never open costs you "
               "nothing. Inside a section, always start at Step 1: the later "
               "steps read what Step 1 produced.",
    ),
    _PageGuide(
        name="Market Barometer",
        one_liner="Watch the outside world: what resin, diesel, milk, "
                  "packaging and wages are doing to our costs.",
        steps=(
            "Open **Monthly Milk, Resin & Freight Movers** and upload this "
            "month's files when you are running the monthly cycle. The four "
            "Mover Downloads appear at the bottom of that section.",
            "Open **Annual COLA Movers** to add or edit a COLA program row, "
            "then press Refresh to save it back to the lakehouse.",
            "Open **Walmart Fresh Tracker** for the Walmart HTST fuel and "
            "resin review.",
            "Scroll to **Market Indices** for the charts. Drag the two date "
            "boxes to pick your window. Pick an end date in the future and a "
            "24-month forecast appears automatically.",
        ),
        needs_fabric=True,
        gotcha="If the indices look stale, the FRED/EIA API keys expired — "
               "the page tells you and shows an upload box for new keys. "
               "Data refreshes itself every 15 days when the keys are valid.",
    ),
    _PageGuide(
        name="New Price Quote",
        one_liner="Type an item number, get its price for a given plant, "
                  "volume bracket, pallet, mileage and drop size.",
        steps=(
            "Type one or more item numbers (or descriptions) in the search "
            "box. Separate several with a semicolon `;`.",
            "Set the filters: plant, volume bracket, pallet, mileage, drop "
            "size.",
            "Read the table. Download it as CSV if you need to send it on.",
            "To change the underlying rate files, use **Download files from "
            "Fabric** and **Upload to replace files in Fabric** at the top — "
            "that is the supported way to edit them.",
        ),
        needs_fabric=True,
        gotcha="The page rebuilds its database only when the source files in "
               "Fabric actually change. If you replaced a file and see the "
               "old numbers, check the upload strip reported success.",
    ),
    _PageGuide(
        name="Pricing Execution Automation",
        one_liner="Turn approved price changes into the VBCS files Oracle "
                  "will accept.",
        steps=(
            "Pick your tool: Fixed Pricing, KS Pricing, Variable Pricing, or "
            "Combine VBCS.",
            "Upload the input file it asks for. The page validates it before "
            "running anything.",
            "Press the generate button and wait — Variable Pricing drives "
            "Excel and is the slow one.",
            "Download the generated VBCS file, then upload it to Oracle.",
        ),
        needs_fabric=True,
        gotcha="Outputs are cached for 5 minutes. If you re-run with a new "
               "input and get the old file, wait or change the input name.",
    ),
    _PageGuide(
        name="Shipment Monitor & HTST Requote",
        one_liner="How each customer actually orders — drop size, distance, "
                  "pallets — and therefore what they should be charged.",
        steps=(
            "Let the page load its lookups from the lakehouse. If it cannot "
            "reach them it shows a manual upload panel instead.",
            "Open **How the metrics & fees are computed** to see the fee "
            "arithmetic before you trust a number.",
            "Read **Customer-Site Details (L3M)**: each customer gets an "
            "annualized volume, a drop tier, a mileage tier and a pallet mix.",
            "The **Total activity fee ($/gal)** column is the answer: what "
            "this customer should pay for the way they order.",
            "Need the underlying lines? **Enriched Shipment Report "
            "(row-level)** downloads them.",
        ),
        needs_fabric=True,
        gotcha="FOB customers (Pricing Method 0) always get $0 delivery "
               "charge. That is not a bug — they collect their own freight.",
    ),
    _PageGuide(
        name="Bid Assistant",
        one_liner="Price a customer bid end to end: volume, delivery, "
                  "pallets, custom label, and the resulting P&L.",
        steps=(
            "Pick the bid scenario file at the top. The page loads it from "
            "the lakehouse.",
            "Fill in the **Reference SKU UOMs** — the lbs/Each boxes. Every "
            "cost formula below depends on them.",
            "Work down the item rows. Costs default from the reference SKUs; "
            "type over anything you want to override.",
            "Use **BOM Search** to look up the bill of material behind any "
            "cost you do not recognise.",
            "Read the program-level table for the rolled-up answer, then "
            "open **Finance P&L** at the bottom to sanity-check against "
            "finance's own report.",
        ),
        needs_fabric=True,
        gotcha="Leave a lbs/Each box blank and it inherits Milk Ref lbs/Each. "
               "That is usually right — but check it, because it silently "
               "changes ingredient, packaging and conversion cost.",
    ),
    _PageGuide(
        name="Oracle Pricing Data Download",
        one_liner="Read the live price adjustments out of Oracle, edit them "
                  "in a spreadsheet, and push the changes back.",
        steps=(
            "Set the filters (market, dates) and press **Read from Oracle**. "
            "The data comes straight from Oracle through ORDS.",
            "Press **Download CSV**. A timestamped copy is also filed in the "
            "lakehouse automatically, so there is always an audit trail.",
            "Edit the CSV. To send a row back, set its `status` to **U** to "
            "update or **N** to insert. Leave it alone to skip it.",
            "Upload the edited CSV, read the preview, then push. Every row "
            "comes back with a result and a reason if it failed.",
            "Optionally open **Compare** to diff this read against a fixed "
            "VBCS file in the lakehouse.",
        ),
        needs_fabric=True,
        gotcha="Rows left at status **S** are skipped on purpose. If nothing "
               "happened, that is almost always why — the page lists every "
               "skipped row and the reason.",
    ),
)


def _render_page_guides() -> None:
    """One expander per sidebar page, in sidebar order."""
    st.markdown("### 🧭 What each page does, and how to drive it")
    st.caption(
        "One box per page in the sidebar, in the same order. Open the one you "
        "are about to use."
    )
    for g in _PAGE_GUIDES:
        with st.expander(f"**{g.name}** — {g.one_liner}", expanded=False):
            if g.needs_fabric:
                st.caption(
                    "🔐 Needs Microsoft Fabric — sign in at the top of this "
                    "page first."
                )
            st.markdown(
                "\n".join(f"{i}. {s}" for i, s in enumerate(g.steps, 1))
            )
            if g.gotcha:
                st.info(f"**Watch out:** {g.gotcha}")


# ── Section 2: the shape the three upload modules share ──────────────────────
#
# RO Comparison, Demand Summary (APS) and Demand Summary (IBP) were aligned on
# one four-step layout precisely so learning one teaches all three.  Saying
# that once, here, beats repeating it inside three separate page guides.

_FOUR_STEP_PRIMER: tuple = (
    ("Step 1 — Upload",
     "The one action that matters, so it comes first and is already open. "
     "Drop in the file; the app checks it and tells you what to fix before it "
     "runs anything."),
    ("Step 2 — Check what you got",
     "What landed, and the published file to download. Look here first "
     "whenever you are not sure an upload worked."),
    ("Step 3 — Compare",
     "The analysis: what changed against the previous cycle or plan, and the "
     "drivers behind each move."),
    ("Step 4 — Undo",
     "Re-upload, withdraw or delete. Deliberately last and out of the "
     "everyday path, so nobody meets the destructive tool before the "
     "uploader."),
)

#: (module, step 1, step 2, step 3, step 4) — the labels as they read on screen.
_MODULE_STEPS: tuple = (
    ("RO Comparison", "Upload the Distribution Tracker", "RO Output",
     "Drivers & drill-in", "Re-upload / change how RO is read"),
    ("Demand Summary (APS / Oracle)", "Upload this cycle's plan",
     "Check what landed", "Compare this cycle with the last one",
     "Delete a cycle"),
    ("Demand Summary (IBP)", "Upload a new Base Plan",
     "Download the plan files", "Compare this plan with the last one",
     "Withdraw a Base Plan upload"),
)

_FOUR_STEP_WARNING: str = (
    "Always start at Step 1. Steps 2 to 4 read what Step 1 produced, so a "
    "stale-looking answer further down almost always means Step 1 has not "
    "been re-run."
)


def _render_four_step_primer() -> None:
    """Explain the shared four-step shape once, instead of three times."""
    st.markdown("### 🪜 The three upload modules all work the same way")
    st.caption(
        "**RO Comparison**, **Demand Summary (APS / Oracle)** and **Demand "
        "Summary (IBP)** — all inside Demand Planner Analytics — are laid out "
        "identically on purpose. Learn one and you can drive the other two."
    )
    for label, what in _FOUR_STEP_PRIMER:
        st.markdown(f"- **{label}** — {what}")
    st.markdown(
        "\n| Module | Step 1 | Step 2 | Step 3 | Step 4 |\n"
        "|---|---|---|---|---|\n"
        + "\n".join(
            f"| **{m[0]}** | {m[1]} | {m[2]} | {m[3]} | {m[4]} |"
            for m in _MODULE_STEPS
        )
    )
    st.info(f"**Watch out:** {_FOUR_STEP_WARNING}")


# ── Section 3: the arithmetic, in a form you can retype into Excel ───────────


class _Formula(NamedTuple):
    """One calculation family, written so it can be rebuilt in a spreadsheet."""
    title: str      # what it computes
    page: str       # which view publishes it
    body: str       # the arithmetic, as a fenced block
    inputs: str     # where each input comes from
    notes: str = ""  # rounding / edge-case rules that change the answer


_FORMULAS: tuple = (
    _Formula(
        title="Milk cost & the Milk Mover",
        page="Market Barometer → Monthly Milk, Resin & Freight Movers",
        body="""Start Month Milk Cost =
    ( Start Skim Rate         * Skim Usage
    + Start Butterfat Rate    * Butterfat Usage
    + Start Protein Rate      * Protein Usage
    + Start Other Solids Rate * Other Solids Usage )
    * ( 1 + Milk Scrape% )

End Month Milk Cost = same formula, End-month rates

Milk Mover $/Gal = End Month Milk Cost - Start Month Milk Cost

Monthly Milk Mover = Monthly Gallons * Milk Mover $/Gal""",
        inputs="""Rates — `Milk_Mover_Tracker`, one set per (Category, Class),
for the two months you picked in the slicer.
Usages — `Milk_cost_tracker/Milk_Usage_Stable.csv`, per item.
Milk Scrape% — the last row's `Milk` cell of `Scrape_Tracker`.
Monthly Gallons — `site_item_volume`.""",
        notes="HTST and ESL items carry 0 for Protein Usage and Other Solids "
              "Usage, so the formula collapses to the older Skim + Butterfat "
              "shape for them. A missing rate or usage counts as **zero**, "
              "not as an error — so a blank never blanks the whole row. "
              "Culture rows always take butterfat from ESL Class II.",
    ),
    _Formula(
        title="Resin cost & the Resin Mover",
        page="Market Barometer → Monthly Milk, Resin & Freight Movers",
        body="""Resin Cost ($/Gal) = $/lbs * Usage (Lbs/Ea) * ( 1 + Scrape% ) / Gal per Ea

Resin Mover ($/Gal) = New Resin Cost ($/Gal) - Old Resin Cost ($/Gal)

    where  new_month = the latest Month present for that side
           old_month = new_month minus exactly one calendar month

Monthly Resin Mover = Monthly Gallons * Resin Mover $/Gal""",
        inputs="""$/lbs — the Movers Non-Milk Tracker (NMT) row:
`Rest HTST Resin Cost ($/lbs)` for Rest, `TOPCO HTST Resin Cost ($/lbs)`
for TOPCO.
Usage (Lbs/Ea) and Gal per Ea — the resin calculator file, per Product ID.
Scrape% — the same Scrape_Tracker as the milk formula.""",
        notes="Result is rounded to 4 decimals. `Gal per Ea = 0` or missing "
              "returns blank rather than dividing by zero. The old-month "
              "subtraction is strict calendar arithmetic — if last month's "
              "row is absent the page warns instead of quietly borrowing an "
              "older month.",
    ),
    _Formula(
        title="Freight Mover",
        page="Market Barometer → Monthly Milk, Resin & Freight Movers",
        body="""Freight Mover $/Gal = tracker last row, Tag-matched freight column

Monthly Freight Mover = Monthly Gallons * Pricing Method * Freight Mover $/Gal""",
        inputs="""Freight column — picked by customer Tag: Rest HTST, TOPCO
HTST, Walmart HTST, Costco HTST PNW or Costco KS Quarterly PDX.
Pricing Method — 0 for FOB, 1 for delivered.""",
        notes="Pricing Method acts as the on/off switch: an FOB customer "
              "multiplies out to $0 freight because they collect their own.",
    ),
    _Formula(
        title="Example-price impact",
        page="Market Barometer → Monthly Milk, Resin & Freight Movers",
        body="""Price Increase% = ( Resin Mover $/EA + Freight Mover $/EA ) / Price $/EA * 100""",
        inputs="""Resin Mover $/EA — from `rest_htst_resin_mover_fg`, matched
on item description.
Freight Mover $/EA — the last row's Rest HTST Freight Mover ($/Gal).
Price $/EA — your example-prices file.""",
    ),
    _Formula(
        title="Activity fees — what a customer should pay",
        page="Shipment Monitor & HTST Requote",
        body="""Pallet%          = Ordered LBS / ( Total Each per Pallet * Unit Net Weight )
                   -> Full if Pallet% >= 80%, else Mixed

Annualized volume = Ordered Secondary QTY / window-days * 365
                   (sell-to = all products; custom-label = non-Darigold only)

Drop size        = SUM( Ordered LBS ) / COUNT( unique orders )   -> Drop tier
Travel distance  = route mileage, Sourcing Plant -> Ship-To      -> Mileage tier

Delivery charge ($/gal)      = table[ (Mileage tier, Drop tier) ]
                               forced to $0 when FOB (Pricing Method 0)
Sell-to fee ($/gal)          = bracket of the annualized sell-to volume
Custom-label fee ($/gal)     = bracket of the annualized custom-label volume
Mixed pallet fee ($/gal)     = charged on Mixed rows

Total activity fee ($/gal)   = Sell-to + Custom-label + Delivery + Mixed-pallet""",
        inputs="""All lookups live in `Files/Activity_Model` in the lakehouse:
the fee brackets, the delivery-charge table, the pallet and UOM files.
Order and shipment lines come from `Activity_Model/Shipment Report`.""",
        notes="The fee tiers are step functions, not interpolations — a volume "
              "one pound over a bracket boundary pays the whole next bracket. "
              "Backtest by looking up the bracket, never by scaling.",
    ),
    _Formula(
        title="RO — how a tracker row becomes an Opportunity or a Risk",
        page="Demand Planner Analytics → RO Comparison (Steps 1 and 4)",
        body="""An OPPORTUNITY row is kept only when it clears EVERY gate:

    Reflected in APS  = no            (i.e. not already in the base plan)
AND Pipeline Status  NOT IN ( Declined, Closed )
AND probability      >  0.00

A RISK row is kept on its own terms, and BYPASSES the two gates above
(a committed loss still matters even once it is marked Declined):

    probability      >= 0.50
AND volume            <  0            (a negative, i.e. a loss)
AND Reflected in APS  = no

Anything clearing neither test is dropped from RO_Seed.""",
        inputs="""One uploaded `Distribution_Tracker.csv` (Step 1). The gates
above are the shipped defaults; **Step 4** exposes them as controls, and a
change there applies the next time you upload in Step 1.""",
        notes="These thresholds are business rules, not physics — check Step 4 "
              "for the values actually in force before you reconcile against a "
              "number someone else produced. Changing the Opportunity gate "
              "rewrites `RO_Seed.csv`, so it only takes effect on the next "
              "upload; changing the Risk threshold re-reads the existing "
              "output and updates the table immediately.",
    ),
    _Formula(
        title="Forecast accuracy — bias, WMAPE, FVA, impact",
        page="Demand Planner Analytics → Demand Summary (IBP) → Step 3",
        body="""Forecast = the lag-1 cycle Base Plan for that month (R&O excluded)
Actual   = IBP Orders, ordered lbs

Bias %       = ( Forecast - Actual ) / Actual
6-Mo Avg Bias = AVERAGE( the six monthly Bias % )

WMAPE        = SUM( ABS( Actual - Forecast ) ) / SUM( ABS( Actual ) )

FVA vs Seasonal-Naive =
    WMAPE( same month last year's orders ) - WMAPE( forecast )
    (in percentage points; positive = the plan beats repeating last year)

Impact (materiality) = segment absolute pound-error / total B2C volume
                     = WMAPE * the segment's share of volume""",
        inputs="""Forecast — the planning cycle whose horizon STARTS that
month (the freshest one-month-ahead view), from the tracker cycles.
Actual — IBP Orders tables in the lakehouse.""",
        notes="Negative bias = **under-forecast** (customers ordered more than "
              "planned). WMAPE is volume-weighted on purpose so one tiny SKU "
              "cannot dominate. A month with no cycle at exactly lag-1 is "
              "backfilled from the nearest earlier cycle and marked with an "
              "asterisk — those months are not strictly comparable. Two driver "
              "tables sit below the summary: **6-Month** ranks what drove the "
              "whole window, **Latest Month** ranks only the most recent one.",
    ),
    _Formula(
        title="APS corporate groups — how each row gets its group",
        page="Demand Planner Analytics → Demand Summary (APS / Oracle)",
        body="""BASE PLAN rows (from the APS bulk export), in order:
    1. bridge    plan_to_code -> dp_dimplantosites -> dp_dimcustomernames
    2. native    the export's own corporate_group_code
    3. (Unmapped)  neither resolved

R&O rows (from the RO_Seed), in order:
    1. exact     the Customer name matches the customer-names dimension
    2. fuzzy     a close name match in that dimension
    3. customer  <- the Customer's OWN name, filled in automatically
    4. (Unmapped)  only when the row has no Customer at all

Every row records which rule fired, in the `Corp Source` column.""",
        inputs="""`RO Tracking/APS/qry_mgmt_plan_full_aps_history.csv`, plus
the `dp_dimplantosites` and `dp_dimcustomernames` dimensions.""",
        notes="Rule 3 replaced a manual review-and-patch step: a planner used "
              "to download the unresolved customers, type a group into each "
              "and upload the sheet back, and what they typed was the "
              "customer's own name almost every time. **Fuzzy matches are "
              "deliberately left alone** — a fuzzy row already carries the "
              "spelling the base plan uses (`SMART AND FINAL` where the seed "
              "says `Smart & Final`), and overwriting it would split one "
              "corporate group into two. Rows marked `override` are historical, "
              "from the retired manual tool, and are never touched.",
    ),
    _Formula(
        title="RFP / bid item costs",
        page="Bid Assistant",
        body="""Per-anchor cost = SUM( Ext Cost.1 ) * Qty.1
                  / Reference SKU lbs per Each
                  * Target SKU lbs per Each

    reading the units left to right:
      SUM( Ext Cost.1 )  = $ per lb of the sub-recipe (e.g. bulk cream)
      / ref lbs per EA   = $ per lb of the Reference SKU
      * target lbs per EA = $ per EA of the Target SKU

Conversion cost = SUM( Ext Cost.1 ) / Conversion Ref SKU lbs per Each
                  * Target SKU lbs per Each

Total GP  = SUM( GP $/EA * units )          <- dollar-weighted, NOT a sum of per-EA
GP%, PCM% = volume-weighted on pounds
FOB Price = SUM( price * units ) / SUM( units )   <- realized average price""",
        inputs="""Ext Cost.1 / Qty.1 — the BOM rows for the reference SKU.
lbs per Each — the four Reference SKU UOM boxes you type on the page.""",
        notes="Blank Ingredient / Packaging / Conversion lbs-per-Each inherit "
              "**Milk Ref lbs/Each**. Roll-ups are weighted, never averaged: "
              "averaging per-EA gross profit across items of different size "
              "hides the portfolio's real profit, which is why Total GP sums "
              "dollars.",
    ),
    _Formula(
        title="Market indices & the 24-month forecast",
        page="Market Barometer → Market Indices",
        body="""Each series is plotted as published by FRED / EIA.

Forecast = Holt-Winters exponential smoothing   (the central line)
           + SARIMA                             (the uncertainty band)

Horizon  = 24 months beyond the last actual month""",
        inputs="""FRED and EIA series, pulled with the API keys stored on the
page. Every series in the summary table links back to its own FRED or EIA
source page.""",
        notes="The forecast is regenerated only when the underlying index CSV "
              "changes, so moving the date sliders never re-fits a model. If "
              "Holt-Winters fails to converge for a series the page falls back "
              "to a linear trend for that series only.",
    ),
)


def _render_formulas() -> None:
    """One expander per formula family, Excel-ready."""
    st.markdown("### 🧮 Every formula, so you can rebuild it in Excel")
    st.caption(
        "Each box gives the arithmetic, where every input comes from, and the "
        "rounding and edge-case rules that change the answer. Retype these "
        "into a sheet and you should land on the same number the app shows."
    )
    for f in _FORMULAS:
        with st.expander(f"**{f.title}**", expanded=False):
            st.caption(f"Published by: {f.page}")
            st.code(f.body, language="text")
            st.markdown(f"**Inputs**\n\n{f.inputs}")
            if f.notes:
                st.warning(f"**Rules that change the answer:** {f.notes}")


# ── Section 4: where the data lives ──────────────────────────────────────────


class _LakehousePath(NamedTuple):
    """One clickable lakehouse destination."""
    label: str    # human name
    path: str     # path under Files/ ("" = lakehouse root)
    used_by: str  # which view reads or writes it


_LAKEHOUSE_PATHS: tuple = (
    _LakehousePath("Pricing lakehouse (root)", "",
                   "Everything below lives here"),
    _LakehousePath("Activity_Model", "Activity_Model",
                   "New Price Quote · Shipment Monitor — fee brackets, "
                   "delivery-charge table, pallet & UOM files"),
    _LakehousePath("Activity_Model / Shipment Report",
                   "Activity_Model/Shipment Report",
                   "Shipment Monitor — the order & shipment lines operators "
                   "refresh monthly"),
    _LakehousePath("Finance", "Finance",
                   "Demand Planner Analytics — SKU-level net sales & gross "
                   "profit actuals"),
    _LakehousePath("RO Tracking", "RO Tracking",
                   "Demand Planner Analytics → RO Comparison — the seed, the "
                   "item master and the trackers (see the file table below)"),
    _LakehousePath("RO Tracking / RO_Reporting", "RO Tracking/RO_Reporting",
                   "Demand Planner Analytics → RO Comparison — the two "
                   "published outputs"),
    _LakehousePath("RO Tracking / Demand Plan", "RO Tracking/Demand Plan",
                   "Demand Planner Analytics → Demand Summary (IBP) — "
                   "management plan, PDH classification, comparison summaries"),
    _LakehousePath("RO Tracking / APS", "RO Tracking/APS",
                   "Demand Planner Analytics → Demand Summary (APS) — the "
                   "cycle history and the latest built cycle"),
    _LakehousePath("Milk_cost_tracker", "Milk_cost_tracker",
                   "Market Barometer — milk usage & base-cost trackers"),
    _LakehousePath("Monthly_Pricing_Execution", "Monthly_Pricing_Execution",
                   "Market Barometer — mover downloads, COLA program "
                   "tracker, refreshable VBCS"),
    _LakehousePath("Program_Bid_Management / New_Bids",
                   "Program_Bid_Management/New_Bids",
                   "Bid Assistant — bid scenario files"),
    _LakehousePath("BOM", "BOM",
                   "Bid Assistant — bills of material behind the cost "
                   "formulas"),
    _LakehousePath("VBCS", "VBCS",
                   "Oracle Pricing Data Download — the fixed VBCS files to "
                   "compare against"),
    _LakehousePath("VBCS / Extract_Snapshot", "VBCS/Extract_Snapshot",
                   "Oracle Pricing Data Download — the automatic audit trail "
                   "of every CSV downloaded"),
)


class _LakehouseFile(NamedTuple):
    """One individual file, and what happens if you change it."""
    path: str        # full path under Files/
    written_by: str  # what puts it there
    if_changed: str  # what moves in the app when this file changes


# The files a planner actually touches or asks about, for the two folders that
# drive Demand Planner Analytics.  Deliberately not exhaustive: an index of
# every blob would go stale in a week and help nobody.
_LAKEHOUSE_FILES: tuple = (
    _LakehouseFile(
        "RO Tracking/Append_New_History/Distribution_Tracker.csv",
        "You, in RO Comparison Step 1",
        "The input to everything else in RO. Re-uploading rebuilds the seed, "
        "the comparison and the summary."),
    _LakehouseFile(
        "RO Tracking/RO_Item_Master.csv",
        "You, in RO Comparison Step 1 or 4",
        "Maps item codes to portfolio and format. Fix it when Step 1 reports "
        "an item it cannot classify."),
    _LakehouseFile(
        "RO Tracking/RO_Seed.csv",
        "The app, from your tracker upload",
        "The filtered opportunity + risk lines. Rebuilt on every upload — "
        "never edit it by hand."),
    _LakehouseFile(
        "RO Tracking/RO_Reporting/RO_Comparison_Output.csv",
        "The app",
        "The row-level comparison behind Step 2 and the Step 3 drivers."),
    _LakehouseFile(
        "RO Tracking/RO_Reporting/RO_Summary_Report.csv",
        "The app",
        "The rolled-up report you download from Step 2."),
    _LakehouseFile(
        "RO Tracking/Demand Plan/qry_pdh.csv",
        "The IBP pipeline",
        "The PDH classification driving every category roll-up. A wrong "
        "portfolio here moves numbers on every Demand Planner page."),
    _LakehouseFile(
        "RO Tracking/Demand Plan/qry_mgmt_plan_history_tracker.csv",
        "Demand Summary (IBP) Step 1",
        "Every IBP cycle ever uploaded — the prior-cycle baseline for both "
        "comparison modules."),
    _LakehouseFile(
        "RO Tracking/APS/qry_mgmt_plan_full_aps_history.csv",
        "Demand Summary (APS) Step 1",
        "Every APS cycle ever uploaded. This is what the Step 2 table and "
        "the Step 3 cycle picker read."),
    _LakehouseFile(
        "RO Tracking/APS/qry_mgmt_plan_full_aps.csv",
        "Demand Summary (APS) Step 1",
        "Just the most recently built cycle, both legs — the file offered for "
        "download in Step 2."),
)


def _render_data_sources() -> None:
    """Clickable index of every Fabric destination the app touches."""
    st.markdown("### 🗄️ Where the data lives (all links open in Fabric)")
    st.caption(
        "Every folder the app reads or writes, in the B2C pricing lakehouse. "
        "Click any row to land on that exact folder — useful when you want to "
        "check a source file yourself, or trace a number back to its input."
    )
    rows = "\n".join(
        f"| [{p.label}]({_lakehouse_url(p.path)}) | {p.used_by} |"
        for p in _LAKEHOUSE_PATHS
    )
    st.markdown(
        "| Lakehouse folder | Read / written by |\n"
        "|---|---|\n" + rows
    )

    st.markdown("#### The individual files behind Demand Planner Analytics")
    st.caption(
        "Who writes each file, and what moves in the app if it changes. "
        "Anything marked *the app* is generated — edit the input instead."
    )
    file_rows = "\n".join(
        f"| `{f.path.rsplit('/', 1)[-1]}` | {f.written_by} | {f.if_changed} |"
        for f in _LAKEHOUSE_FILES
    )
    st.markdown(
        "| File | Written by | What changes if it changes |\n"
        "|---|---|---|\n" + file_rows
    )

    st.markdown("#### Embedded reports")
    st.markdown(
        f"- [Velocity Analysis (Fabric)]({_VELOCITY_REPORT_URL}) — embedded at "
        f"the bottom of **Demand Planner Analytics**\n"
        f"- [Finance P&L (Power BI)]({_FINANCE_PNL_REPORT_URL}) — embedded at "
        f"the bottom of **Bid Assistant**"
    )
    st.caption(
        "Embedded frames use Entra-ID auto-auth. If one renders blank, your "
        "browser session is not authenticated to the tenant — use the link "
        "above (or the report's own *Open in new tab* button) instead."
    )


# ── Section 5: the same manual, as a Word file ───────────────────────────────

_DOC_TITLE: str = "Darigold Pricing Intelligence — User Manual"
_DOC_FILENAME: str = "Darigold_Pricing_Intelligence_Manual.docx"

_START_HERE: tuple = (
    "**Sign in to Microsoft Fabric.** Once per session, on the Documentation "
    "page. Every other page then just works.",
    "**Pick a page on the left.** The sidebar is ordered the way the work "
    "flows: Documentation first, the daily tools in the middle, the two "
    "specialist tools at the bottom.",
    "**Open the page's own instructions.** Every page has an Instructions "
    "block or a \"how this is computed\" box at the top.",
)

_UTF8_TIP: str = (
    "CSV uploads must be UTF-8 encoded. If a file is rejected for no obvious "
    "reason, re-save it from Excel as CSV UTF-8 (Comma delimited)."
)


@st.cache_data(show_spinner=False)
def _build_manual_docx() -> bytes:
    """Render the whole manual to a Word file.

    Walks the same tuples the page renders, so the download cannot drift away
    from what is on screen — that is the entire reason the content is data.

    Cached because it is pure: the manual is static, so the document is built
    once per session rather than on every rerun of the landing page.
    """
    today = date.today().strftime("%d %B %Y")
    b = docx_export.DocBuilder(
        _DOC_TITLE,
        "How to drive every page, the arithmetic behind every published "
        "number, and where all the data lives.",
    )
    b.footer_note(f"Generated from the live app on {today}")
    b.rule()

    b.heading("Start here", 1)
    b.numbered([s.replace("**", "") for s in _START_HERE])
    b.para(
        "If a page says it has no data, check you are still signed in to "
        "Microsoft Fabric — that is the cause nine times out of ten.",
        italic=True, grey=True,
    )

    b.heading("What each page does, and how to drive it", 1)
    b.para("One entry per page in the sidebar, in the same order.", grey=True)
    for g in _PAGE_GUIDES:
        b.heading(g.name, 2)
        b.para(g.one_liner, italic=True)
        if g.needs_fabric:
            b.para("Needs Microsoft Fabric — sign in first.", grey=True)
        b.numbered([s.replace("**", "").replace("`", "") for s in g.steps])
        if g.gotcha:
            b.callout("Watch out:", g.gotcha)

    b.page_break()
    b.heading("The three upload modules all work the same way", 1)
    b.para(
        "RO Comparison, Demand Summary (APS / Oracle) and Demand Summary "
        "(IBP) — all inside Demand Planner Analytics — are laid out "
        "identically on purpose. Learn one and you can drive the other two."
    )
    b.bullets([f"{label} — {what}" for label, what in _FOUR_STEP_PRIMER])
    b.table(
        ["Module", "Step 1", "Step 2", "Step 3", "Step 4"],
        [list(m) for m in _MODULE_STEPS],
    )
    b.callout("Watch out:", _FOUR_STEP_WARNING)

    b.page_break()
    b.heading("Every formula, so you can rebuild it in Excel", 1)
    b.para(
        "Each entry gives the arithmetic, where every input comes from, and "
        "the rounding and edge-case rules that change the answer.", grey=True,
    )
    for f in _FORMULAS:
        b.heading(f.title, 2)
        b.para(f"Published by: {f.page}", grey=True)
        b.code(f.body)
        b.para("Inputs", bold=True)
        b.para(f.inputs.replace("`", ""))
        if f.notes:
            b.callout("Rules that change the answer:", f.notes.replace("**", ""))

    b.page_break()
    b.heading("Where the data lives", 1)
    b.para(
        "Every folder the app reads or writes in the B2C pricing lakehouse. "
        "The folder names are clickable and open in Fabric.", grey=True,
    )
    b.table(
        ["Lakehouse folder", "Read / written by"],
        [[p.label, p.used_by] for p in _LAKEHOUSE_PATHS],
        links=[_lakehouse_url(p.path) for p in _LAKEHOUSE_PATHS],
    )

    b.heading("The individual files behind Demand Planner Analytics", 2)
    b.para(
        "Who writes each file, and what moves in the app if it changes. "
        "Anything marked \"the app\" is generated — edit the input instead.",
        grey=True,
    )
    b.table(
        ["File", "Written by", "What changes if it changes"],
        [[f.path, f.written_by, f.if_changed] for f in _LAKEHOUSE_FILES],
        links=[_lakehouse_url(f.path.rsplit("/", 1)[0]) for f in _LAKEHOUSE_FILES],
    )

    b.heading("Embedded reports", 2)
    b.link_bullets((
        ("Velocity Analysis (Fabric)", _VELOCITY_REPORT_URL,
         "embedded at the bottom of Demand Planner Analytics"),
        ("Finance P&L (Power BI)", _FINANCE_PNL_REPORT_URL,
         "embedded at the bottom of Bid Assistant"),
    ))
    b.para(
        "Embedded frames use Entra-ID auto-auth. If one renders blank, your "
        "browser session is not authenticated to the tenant — use the link "
        "instead.", grey=True,
    )

    b.rule()
    b.callout("Tip:", _UTF8_TIP)
    return b.to_bytes()


def _render_docx_download() -> None:
    """Offer the manual as a Word file, or explain why it is unavailable.

    Never raises: this is the landing page, and a missing optional dependency
    must not take down the app's only sign-in surface.
    """
    if not docx_export.AVAILABLE:
        st.caption(
            "📄 Word download unavailable — the `python-docx` package is not "
            "installed in this environment. Everything on this page is still "
            "readable here."
        )
        return
    try:
        data = _build_manual_docx()
    except Exception as exc:  # noqa: BLE001 — a broken export must not 500 the page
        st.caption(f"📄 Word download unavailable ({exc}).")
        return
    st.download_button(
        "📄 Download this manual (Word)",
        data=data,
        file_name=_DOC_FILENAME,
        mime=("application/vnd.openxmlformats-officedocument"
              ".wordprocessingml.document"),
        key="documentation_docx_download",
        type="primary",
        use_container_width=True,
        help="The whole page as a .docx — page guides, formulas and the "
             "lakehouse index, with every link still clickable. Share it with "
             "someone who does not have access to the app.",
    )


# ── Entry point ──────────────────────────────────────────────────────────────


def render() -> None:
    """Render the Documentation page.

    Flow
    ----
    1. Microsoft Fabric sign-in (top of page — the app's only sign-in UI)
    2. Start here — the three things a new user must know
    3. Download the whole manual as Word
    4. What each page does, and how to drive it (one expander per page)
    5. The four-step shape the three upload modules share
    6. Every formula, so you can rebuild it in Excel
    7. Where the data lives — clickable lakehouse folder + file index

    Everything below the sign-in panel is static content built from
    module-level tuples: no I/O, no Fabric reads, nothing to fail.  That is
    deliberate — the landing page must render even when Fabric is down,
    because it is where users come to find out why.
    """
    apply_custom_css()

    st.markdown(
        '<h1 class="main-header">Darigold Pricing Intelligence</h1>',
        unsafe_allow_html=True,
    )
    st.caption(
        "Sign in below, then use the sidebar. This page explains every other "
        "page, every formula behind the numbers, and where all the data lives."
    )

    # ── Microsoft Fabric Sign-in (prominent, top of page) ────────────────────
    #
    # First thing on the app's first page, on purpose: a successful sign-in
    # here silently unlocks every Fabric-backed view, and no other page
    # renders a sign-in prompt of its own.
    st.markdown("---")
    fabric_signin_widget.render_fabric_signin_section()
    st.markdown("---")

    st.markdown("### 🚦 Start here")
    st.markdown("\n".join(f"{i}. {s}" for i, s in enumerate(_START_HERE, 1)))
    st.caption(
        "*If a page says it has no data, come back here and check you are "
        "still signed in — that is the cause nine times out of ten.*"
    )

    st.markdown("---")
    _render_docx_download()

    st.markdown("---")
    _render_page_guides()

    st.markdown("---")
    _render_four_step_primer()

    st.markdown("---")
    _render_formulas()

    st.markdown("---")
    _render_data_sources()

    st.markdown("---")
    st.info(f"💡 **Tip**: {_UTF8_TIP}")
