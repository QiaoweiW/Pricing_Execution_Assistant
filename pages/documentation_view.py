"""
Documentation page view — the app's landing page and its single sign-in point.

Two jobs, in this order:

1. **Microsoft Fabric sign-in.**  This is the ONLY place in the app that
   renders the device-code sign-in flow.  Every other view detects the
   session and points here, so the panel stays at the top of this page and
   this page stays first in the sidebar.
2. **The manual.**  How to drive each view (click by click), the exact
   arithmetic behind every number the app publishes (so anyone can rebuild
   it in Excel), and a clickable index of the lakehouse folders each view
   reads from.

Why the content is data, not prose
----------------------------------
The page bodies below are built from module-level tuples
(:data:`_PAGE_GUIDES`, :data:`_FORMULAS`, :data:`_LAKEHOUSE_PATHS`) rather
than one long ``st.markdown`` blob.  Adding a page, a formula or a folder is
a one-entry edit, the render functions never change, and nothing can drift
out of alignment between the three sections.  It also keeps every Fabric URL
in exactly one place — see :data:`_LAKEHOUSE_BASE`.
"""
from __future__ import annotations

from typing import NamedTuple
from urllib.parse import quote

import streamlit as st

from utils import fabric_signin_widget
from utils.ui_helpers import apply_custom_css


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
        name="Demand Planner Analytics",
        one_liner="The demand plan: what we said we would sell, what we "
                  "actually sold, and where the two disagree.",
        steps=(
            "**IBP Cadence and Supporting files** is a list of links — the "
            "checklist, the change journal, the Power BI reports. Click and "
            "go.",
            "**Business Health** is the executive glance: three levers, "
            "green or red. Open **Reconciliation & data sources** inside it "
            "to see exactly which files fed a number.",
            "**RO Comparison** opens already expanded because it is the "
            "section you came for. Pick a cycle, read the bridge.",
            "**Demand Summary (APS)** is where you upload a base plan, "
            "review it by corporate group, and push a patch.",
            "**Velocity Analysis** is the embedded Fabric report. Click "
            "*Load Velocity Analysis report* once and it stays loaded.",
        ),
        needs_fabric=True,
        gotcha="Sections load only when you ask them to. That is deliberate — "
               "it is why the page opens fast. A section you never open costs "
               "you nothing.",
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
            "Read the dashboard: each customer gets an annualized volume, a "
            "drop tier, a mileage tier and a pallet mix.",
            "The **Total activity fee ($/gal)** column is the answer: what "
            "this customer should pay for the way they order.",
        ),
        needs_fabric=True,
        gotcha="FOB customers (Pricing Method 0) always get $0 delivery "
               "charge. That is not a bug — they collect their own freight.",
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
            "Set the filters (market, dates) and read the data. It comes "
            "straight from Oracle through ORDS.",
            "Press **Download CSV**. A timestamped copy is also filed in the "
            "lakehouse automatically, so there is always an audit trail.",
            "Edit the CSV. To send a row back, set its `status` to **U** to "
            "update or **N** to insert. Leave it alone to skip it.",
            "Upload the edited CSV, read the preview, then push. Every row "
            "comes back with a result and a reason if it failed.",
            "Optionally open **Compare against a fixed VBCS file** to diff "
            "this read against a file in the lakehouse.",
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


# ── Section 2: the arithmetic, in a form you can retype into Excel ───────────


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
        title="Forecast accuracy — bias, WMAPE, FVA, impact",
        page="Demand Planner Analytics → Business Health / bias tables",
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
              "asterisk — those months are not strictly comparable.",
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


# ── Section 3: where the data lives ──────────────────────────────────────────


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
    _LakehousePath("RO Tracking / Demand Plan", "RO Tracking/Demand Plan",
                   "Demand Planner Analytics — management plan, PDH "
                   "classification, comparison summaries"),
    _LakehousePath("RO Tracking / Demand Plan / qry_pdh.csv",
                   "RO Tracking/Demand Plan/qry_pdh.csv",
                   "Demand Planner Analytics — the PDH classification that "
                   "drives every category rollup"),
    _LakehousePath("RO Tracking / APS", "RO Tracking/APS",
                   "Demand Planner Analytics — APS base-plan uploads & "
                   "history"),
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


# ── Entry point ──────────────────────────────────────────────────────────────


def render() -> None:
    """Render the Documentation page.

    Flow
    ----
    1. Microsoft Fabric sign-in (top of page — the app's only sign-in UI)
    2. Start here — the three things a new user must know
    3. What each page does, and how to drive it (one expander per page)
    4. Every formula, so you can rebuild it in Excel
    5. Where the data lives — clickable lakehouse index

    Everything below the sign-in panel is static markdown built from
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

    st.markdown("""
### 🚦 Start here

1. **Sign in above.** Once per session. Every other page then just works.
2. **Pick a page on the left.** The sidebar is ordered the way the work
   flows: this page first, the daily tools in the middle, the two
   specialist tools at the bottom.
3. **Open a page's own instructions.** Every page has an Instructions block
   or a "how this is computed" box at the top. This page tells you which.

*If a page says it has no data, come back here and check you are still
signed in — that is the cause nine times out of ten.*
""")

    st.markdown("---")
    _render_page_guides()

    st.markdown("---")
    _render_formulas()

    st.markdown("---")
    _render_data_sources()

    st.markdown("---")
    st.info(
        "💡 **Tip**: CSV uploads must be **UTF-8 encoded**. If a file is "
        "rejected for no obvious reason, re-save it from Excel as "
        "*CSV UTF-8 (Comma delimited)*."
    )
