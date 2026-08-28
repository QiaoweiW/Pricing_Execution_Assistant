"""Order-book fill-rate data source — ``dbo.Orders`` + ``dbo.Products`` +
``dbo.Customers`` (OneLake Delta).

Backs the Velocity Analysis section's **Weekly Ordered vs Shipped lbs — our
volume & fill rate** chart.  That chart used to read :mod:`shipments_velocity`
(``dbo.Shipments``); it now reads the Oracle order book instead, because
``dbo.Shipments`` only carries lines that shipped and therefore cannot show a
cut.  ``dbo.Orders`` keeps the original ordered quantity alongside the
cancelled and shipped quantities, so a line that was ordered and then killed
is visible as a service failure rather than absent from the denominator.

Everything else in the Velocity Analysis section still reads
``dbo.Shipments`` — this module is deliberately scoped to the one chart.

Shape of the source
-------------------
``dbo.Orders`` is a **single daily snapshot** (one ``Snapshot Date``, one row
per ``Fulfill Line ID``), so there is no snapshot fan-out to guard against.
It is fulfillment-line grain and carries no descriptive dimensions at all —
only Oracle surrogate ids — hence the two dimension joins:

* ``Inventory Item ID`` → ``dbo.Products``  — Portfolio Major / Minor, Supply
  Format, Business Unit, Item No, Item Description.
* ``Ship To Party Site ID`` → ``dbo.Customers`` (``Party Site ID``) —
  Organization Name (the customer) and Site Number (the ship-to).

Both joins are **deduplicated to exactly one row per key** before merging,
which matters more than it sounds:

* ``dbo.Products`` repeats each item once per inventory organisation — up to
  **54 rows for a single Inventory Item ID**.  We keep the
  ``Darigold Item Master Organization`` row (5,229 items, unique on the id),
  falling back to the first row for items absent from the master org.
* ``dbo.Customers`` repeats each party site per Site Purpose (Ship to / Bill
  to / Plan To).  We keep the ``Ship to`` row, falling back to any row.

A naive join would multiply pounds by the duplicate count.
:func:`join_dimensions` asserts the row count and total pounds are unchanged,
so a future dedupe regression fails loudly instead of quietly inflating the
chart.

Vocabulary alignment
--------------------
The section's filter dropdowns are populated from ``dbo.Shipments``, so the
values this module returns have to match those strings or a filtered chart
would come back empty.  Verified overlap against the live tables:

============== ================================================== ==========
Filter         Source column                                      Match
============== ================================================== ==========
Business Unit  Products ``Business Unit``                          100%
Product Format Products ``Supply Format``                          100%
Product Desc   Products ``Item Description``                       100%
Customer       Customers ``Organization Name``                     100%
Portfolio      Products ``Portfolio Major`` + :data:`_PORTFOLIO_ALIASES`  100%
============== ================================================== ==========

``Customer`` maps to ``Organization Name`` specifically: it matches all 432
Shipments customer values, where ``Account Name`` reaches 96% and
``Site Name`` only 60%.  Portfolio needs the alias map because Shipments
spells the two fluid portfolios "Extended Shelf Life" / "Fresh Milk" while
Products codes them "ESL" / "HTST" — confirmed as a clean one-to-one against
matched item descriptions (no cross-contamination).

Corporate Group is **not** taken from ``dbo.Customers`` even though that table
has the column: the section's dropdown is built from the app's
``dp_dimcustomernames`` lookup, which agrees per-site but carries a different
vocabulary (119 groups vs 802).  This module therefore returns ``ship_to``
(the party site number) and the page attaches Corporate Group with the same
helper it already uses for shipments — one source of truth, identical values.

Read strategy
-------------
Every scan is **column-projected with no SQL predicate**.  That is not a
style choice: DuckDB's delta-kernel raises ``Json error: Truncated record
whilst reading string`` when a string-equality predicate is pushed into the
scan of these tables (``WHERE "Site Purpose" = 'Ship to'`` reproduces it
every time), while unpushable predicates are fine.  Scoping therefore happens
in pandas, which is also how :mod:`shipments_velocity` works.

The line-level frame is then **pre-aggregated to weekly × dimension** before
it leaves this module — 1.55M order lines collapse by roughly an order of
magnitude, which keeps the cached object small enough to sit in session state
while preserving every filterable dimension.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Optional

import numpy as np
import pandas as pd
import streamlit as st

from data_sources.fabric_auth import (
    FabricAuthError,
    acquire_storage_token,
    bind_storage_token,
    duckdb_lock,
    get_duckdb_connection,
    prepare_duckdb_tls,
)
from data_sources.fabric_tls import tls_error_hint as _tls_hint
from data_sources.shipments_velocity import (
    COL_BUSINESS_UNIT,
    COL_CORP_GROUP,
    COL_CUSTOMER,
    COL_PORTFOLIO,
    COL_PRODUCT_DESC,
    COL_PRODUCT_FORMAT,
    COL_PRODUCT_MINOR,
    COL_SHIP_TO,
    WEEK_START,
)


# ── Source identity (same lakehouse as IBP / Shipments; dbo schema) ──────────
_WORKSPACE_GUID = "bb11c51d-03c8-4f1b-938c-e20657a8f31d"
_LAKEHOUSE_GUID = "a01f513d-eee7-41eb-8c15-670bc40e7fc8"
_SCHEMA = "dbo"
_TABLE_ORDERS = "Orders"
_TABLE_PRODUCTS = "Products"
_TABLE_CUSTOMERS = "Customers"

_CACHE_TTL_SECONDS = 15 * 60

# The Products row we treat as authoritative per item (see module docstring).
_MASTER_ORG = "Darigold Item Master Organization"
# The Customers row we treat as authoritative per party site.
_SHIP_TO_PURPOSE = "Ship to"

# ── Scope: which order lines count ───────────────────────────────────────────
# Sales-order lines only — excludes returns, credit-only, bill-only and cancel
# lines, which would otherwise net against real demand.
_LINE_TYPE_SALES = "ORA_BUY"
_CATEGORY_ORDER = "ORDER"
# "Net of cancelled sales orders": a line whose status is Canceled never
# became a shipment opportunity, so it leaves the frame entirely.  PARTIAL
# cuts on surviving lines DO remain, and are what the gross fill rate exposes.
_STATUS_CANCELED = "Canceled"
# A line is only allowed into the fill-rate denominator once it has finished
# its lifecycle.  Anything else is still in flight — counted in the volume
# bars, excluded from the fill rate, so recent weeks don't read as a collapse
# just because their lines have not shipped yet.
COMPLETED_LINE_STATUSES: frozenset[str] = frozenset({
    "Closed", "Shipped", "Billed",
})

# Shipments spells these two portfolios differently from Products.  Mapping
# runs Products → Shipments so the values match the section's dropdowns.
_PORTFOLIO_ALIASES: dict[str, str] = {
    "ESL": "Extended Shelf Life",
    "HTST": "Fresh Milk",
}

# ── Canonical measure columns of the frame this module returns ───────────────
# The dimension columns reuse shipments_velocity's canonical names so the
# section's existing filter selections apply unchanged.
COL_ORDERED_LBS: str = "ordered_lbs"            # net of cancellations
COL_ORIGINAL_ORDERED_LBS: str = "original_ordered_lbs"
COL_CANCELED_LBS: str = "canceled_lbs"
COL_SHIPPED_LBS: str = "shipped_lbs"
# Same measures restricted to completed lines — the fill-rate numerator and
# denominator, kept separate so the ratio is never mixed-basis.
COL_COMPLETED_ORIGINAL_LBS: str = "completed_original_lbs"
COL_COMPLETED_SHIPPED_LBS: str = "completed_shipped_lbs"
COL_COMPLETED_CANCELED_LBS: str = "completed_canceled_lbs"
COL_LINES: str = "order_lines"
COL_OPEN_LINES: str = "open_lines"
FILL_RATE_GROSS: str = "fill_rate_gross"        # shipped ÷ ORIGINAL ordered
FILL_RATE_NET: str = "fill_rate_net"            # shipped ÷ ordered (post-cut)
CUT_RATE: str = "cut_rate"                      # cancelled ÷ original ordered

# The pre-aggregation grain.  Deliberately excludes Item No: the chart and
# every filter it honours key off the description, so carrying the item as
# well would only split rows (and inflate the cached frame) for no gain.
_DIM_COLS: tuple[str, ...] = (
    COL_PORTFOLIO, COL_PRODUCT_MINOR, COL_PRODUCT_DESC, COL_PRODUCT_FORMAT,
    COL_BUSINESS_UNIT, COL_CUSTOMER, COL_SHIP_TO,
)
_MEASURE_COLS: tuple[str, ...] = (
    COL_ORDERED_LBS, COL_ORIGINAL_ORDERED_LBS, COL_CANCELED_LBS,
    COL_SHIPPED_LBS, COL_COMPLETED_ORIGINAL_LBS, COL_COMPLETED_SHIPPED_LBS,
    COL_COMPLETED_CANCELED_LBS, COL_LINES, COL_OPEN_LINES,
)

# ── Source column projections (narrow reads) ─────────────────────────────────
_ORDERS_COLS: tuple[str, ...] = (
    "Ordered Date", "Line Type Code", "Category Code", "Line Status",
    "Original Ordered Quantity Pounds", "Canceled Quantity Pounds",
    "Ordered Quantity Pounds", "Shipped Quantity Pounds",
    "Inventory Item ID", "Ship To Party Site ID",
)
_PRODUCTS_COLS: tuple[str, ...] = (
    "Inventory Item ID", "Organization Name", "Item No", "Item Description",
    "Portfolio Major", "Portfolio Minor", "Supply Format", "Business Unit",
)
_CUSTOMERS_COLS: tuple[str, ...] = (
    "Party Site ID", "Site Purpose", "Organization Name", "Site Number",
)


class OrdersFillRateError(RuntimeError):
    """Raised on any failure to read / shape the order-book fill-rate frame.

    Wraps the underlying exception so the page renders one clean banner
    instead of leaking DuckDB / delta-kernel stack traces.
    """


# ── Pure transforms ──────────────────────────────────────────────────────────

def dedupe_products(products: pd.DataFrame) -> pd.DataFrame:
    """One row per ``Inventory Item ID``, master-org row preferred.

    ``dbo.Products`` carries an item once per inventory organisation, so the
    join key is far from unique (up to 54 rows for one id).  The item-master
    organisation is the authoritative row; items missing from it fall back to
    their first row so nothing is silently dropped.
    """
    if products is None or products.empty:
        return pd.DataFrame(columns=list(_PRODUCTS_COLS))
    org = products["Organization Name"].astype(str).str.strip()
    master = products[org == _MASTER_ORG]
    rest = products[~products["Inventory Item ID"].isin(
        master["Inventory Item ID"])]
    out = pd.concat([master, rest.drop_duplicates(subset="Inventory Item ID")],
                    ignore_index=True)
    out = out.drop_duplicates(subset="Inventory Item ID")
    return out.drop(columns=["Organization Name"])


def dedupe_customers(customers: pd.DataFrame) -> pd.DataFrame:
    """One row per ``Party Site ID``, ``Ship to`` purpose preferred.

    A party site appears once per Site Purpose; the ship-to row is the one
    that describes where product goes.  Sites with no ship-to row fall back to
    any row rather than losing their customer name.
    """
    if customers is None or customers.empty:
        return pd.DataFrame(columns=list(_CUSTOMERS_COLS))
    purpose = customers["Site Purpose"].astype(str).str.strip()
    ship = customers[purpose == _SHIP_TO_PURPOSE]
    rest = customers[~customers["Party Site ID"].isin(ship["Party Site ID"])]
    out = pd.concat([ship, rest], ignore_index=True)
    out = out.drop_duplicates(subset="Party Site ID")
    return out.drop(columns=["Site Purpose"])


def scope_sales_orders(orders: pd.DataFrame) -> pd.DataFrame:
    """Keep sales-order lines, net of fully-cancelled ones.

    Sales orders are ``Line Type Code = ORA_BUY`` and
    ``Category Code = ORDER``; returns, credit-only and bill-only lines are
    dropped so they cannot net against real demand.  Lines whose status is
    ``Canceled`` are dropped too — a killed line was never a shipment
    opportunity.  Partial cuts on surviving lines stay, and are exactly what
    the gross fill rate is there to reveal.
    """
    if orders is None or orders.empty:
        return orders
    lt = orders["Line Type Code"].astype(str).str.strip().str.upper()
    cc = orders["Category Code"].astype(str).str.strip().str.upper()
    ls = orders["Line Status"].astype(str).str.strip()
    keep = (lt == _LINE_TYPE_SALES) & (cc == _CATEGORY_ORDER) & (ls != _STATUS_CANCELED)
    return orders.loc[keep]


def join_dimensions(orders: pd.DataFrame, products: pd.DataFrame,
                    customers: pd.DataFrame) -> pd.DataFrame:
    """Left-join the deduped product + customer dimensions onto order lines.

    Raises :class:`OrdersFillRateError` if either join changes the row count
    or the total ordered pounds — the signature of a dedupe regression
    silently multiplying volume.
    """
    if orders is None or orders.empty:
        return orders
    n_before = len(orders)
    lbs_before = float(
        pd.to_numeric(orders["Ordered Quantity Pounds"], errors="coerce")
        .fillna(0.0).sum())

    out = orders.merge(dedupe_products(products), on="Inventory Item ID",
                       how="left")
    out = out.merge(dedupe_customers(customers),
                    left_on="Ship To Party Site ID", right_on="Party Site ID",
                    how="left")

    lbs_after = float(
        pd.to_numeric(out["Ordered Quantity Pounds"], errors="coerce")
        .fillna(0.0).sum())
    if len(out) != n_before or abs(lbs_after - lbs_before) > 1.0:
        raise OrdersFillRateError(
            f"Dimension join changed the data: {n_before:,} lines / "
            f"{lbs_before:,.0f} lbs became {len(out):,} lines / "
            f"{lbs_after:,.0f} lbs.  dbo.Products or dbo.Customers has "
            f"duplicate join keys that the dedupe no longer collapses."
        )
    return out


def week_start(order_dt: pd.Series) -> pd.Series:
    """Monday-of-week for each order date, timezone-stripped and normalised.

    ``Ordered Date`` is timezone-aware in the source; the section's weekly
    grid is naive Monday-anchored dates, so we drop the offset before
    anchoring rather than letting a UTC conversion shift a Monday order into
    the previous week.
    """
    dt = pd.to_datetime(order_dt, errors="coerce", utc=True)
    naive = dt.dt.tz_localize(None)
    return (naive - pd.to_timedelta(naive.dt.weekday, unit="D")).dt.normalize()


def tidy_orders(orders: pd.DataFrame, products: pd.DataFrame,
                customers: pd.DataFrame) -> pd.DataFrame:
    """Scope, join, rename and pre-aggregate to weekly × dimension.

    Returns one row per (week, portfolio, minor, description, format,
    business unit, customer, ship-to, item) with the measure columns summed —
    every dimension the section can filter on is preserved, so the chart is
    reactive, but the frame is an order of magnitude smaller than line level.
    """
    scoped = scope_sales_orders(orders)
    if scoped is None or scoped.empty:
        return pd.DataFrame(columns=[WEEK_START, *_DIM_COLS, *_MEASURE_COLS])
    joined = join_dimensions(scoped, products, customers)

    def num(col: str) -> np.ndarray:
        return pd.to_numeric(joined.get(col), errors="coerce").fillna(0.0).to_numpy()

    def text(series: Optional[pd.Series]) -> np.ndarray:
        if series is None:
            return np.array([""] * len(joined), dtype=object)
        return series.astype(str).str.strip().replace(
            {"nan": "", "None": "", "<NA>": ""}).to_numpy()

    status = joined["Line Status"].astype(str).str.strip()
    completed = status.isin(COMPLETED_LINE_STATUSES).to_numpy()
    portfolio = pd.Series(text(joined.get("Portfolio Major"))).replace(
        _PORTFOLIO_ALIASES)

    work = pd.DataFrame({
        WEEK_START: week_start(joined["Ordered Date"]),
        COL_PORTFOLIO: portfolio.to_numpy(),
        COL_PRODUCT_MINOR: text(joined.get("Portfolio Minor")),
        COL_PRODUCT_DESC: text(joined.get("Item Description")),
        COL_PRODUCT_FORMAT: text(joined.get("Supply Format")),
        COL_BUSINESS_UNIT: text(joined.get("Business Unit")),
        COL_CUSTOMER: text(joined.get("Organization Name")),
        COL_SHIP_TO: text(joined.get("Site Number")),
        COL_ORIGINAL_ORDERED_LBS: num("Original Ordered Quantity Pounds"),
        COL_CANCELED_LBS: num("Canceled Quantity Pounds"),
        COL_ORDERED_LBS: num("Ordered Quantity Pounds"),
        COL_SHIPPED_LBS: num("Shipped Quantity Pounds"),
    })
    work[COL_COMPLETED_ORIGINAL_LBS] = np.where(
        completed, work[COL_ORIGINAL_ORDERED_LBS], 0.0)
    work[COL_COMPLETED_SHIPPED_LBS] = np.where(
        completed, work[COL_SHIPPED_LBS], 0.0)
    work[COL_COMPLETED_CANCELED_LBS] = np.where(
        completed, work[COL_CANCELED_LBS], 0.0)
    work[COL_LINES] = 1
    work[COL_OPEN_LINES] = (~completed).astype(int)
    work = work[work[WEEK_START].notna()]

    grouped = (work.groupby([WEEK_START, *_DIM_COLS], as_index=False, sort=False)
                   [list(_MEASURE_COLS)].sum())
    # The dimension columns are long, highly-repetitive strings; as plain
    # objects the cached frame runs several hundred MB.  Categoricals cut that
    # by roughly 4x and cost nothing — the filters compare via astype(str).
    for col in _DIM_COLS:
        grouped[col] = grouped[col].astype("category")
    return grouped


@dataclass(frozen=True)
class WeeklyFillRate:
    """Result of :func:`build_weekly_fill_rate`.

    ``weekly`` is one row per week (Monday-anchored, ascending) carrying the
    volume measures, the two fill rates and the open-line count.  The window
    totals are the filtered sums the KPI tiles show.
    """

    weekly: pd.DataFrame
    total_ordered: float = 0.0
    total_original: float = 0.0
    total_shipped: float = 0.0
    total_cut: float = 0.0
    completed_original: float = 0.0
    completed_shipped: float = 0.0
    completed_cut: float = 0.0
    open_lines: int = 0

    @property
    def fill_rate_gross(self) -> Optional[float]:
        """Shipped ÷ ORIGINAL ordered, completed lines only — cuts count."""
        return (self.completed_shipped / self.completed_original
                if self.completed_original else None)

    @property
    def fill_rate_net(self) -> Optional[float]:
        """Shipped ÷ ordered after cuts, completed lines only.

        Both terms are restricted to completed lines — subtracting *all*
        cancellations from a completed-only denominator would mix bases and
        overstate the ratio.
        """
        denom = self.completed_original - self.completed_cut
        return (self.completed_shipped / denom) if denom > 0 else None

    @property
    def cut_rate(self) -> Optional[float]:
        """Cancelled ÷ original ordered across the window."""
        return (self.total_cut / self.total_original
                if self.total_original else None)


def build_weekly_fill_rate(
    df: pd.DataFrame, *,
    portfolios:       Optional[list[str]] = None,
    product_minors:   Optional[list[str]] = None,
    product_descs:    Optional[list[str]] = None,
    customers:        Optional[list[str]] = None,
    business_units:   Optional[list[str]] = None,
    product_formats:  Optional[list[str]] = None,
    corporate_groups: Optional[list[str]] = None,
    date_range:       Optional[tuple[date, date]] = None,
) -> WeeklyFillRate:
    """Filter the tidy order frame and aggregate to a weekly fill-rate series.

    Filters mirror :func:`shipments_velocity.build_weekly_velocity` exactly —
    same argument names, same "empty list means all" semantics — so the
    section's existing selections pass straight through.  Each filter applies
    only when values are supplied AND the column is present.

    The fill-rate columns divide only the *completed* measures, so a week
    whose lines have not shipped yet shows its true volume in the bars
    without dragging the ratio down.
    """
    empty_cols = [WEEK_START, COL_ORDERED_LBS, COL_SHIPPED_LBS,
                  COL_ORIGINAL_ORDERED_LBS, COL_CANCELED_LBS,
                  FILL_RATE_GROSS, FILL_RATE_NET, CUT_RATE, COL_OPEN_LINES]
    if df is None or df.empty or WEEK_START not in df.columns:
        return WeeklyFillRate(weekly=pd.DataFrame(columns=empty_cols))

    work = df
    _filters = {
        COL_PORTFOLIO: portfolios, COL_PRODUCT_MINOR: product_minors,
        COL_PRODUCT_DESC: product_descs, COL_CUSTOMER: customers,
        COL_BUSINESS_UNIT: business_units, COL_PRODUCT_FORMAT: product_formats,
        COL_CORP_GROUP: corporate_groups,
    }
    mask = pd.Series(True, index=work.index)
    for col, values in _filters.items():
        if values and col in work.columns:
            mask &= work[col].astype(str).str.strip().isin(
                [str(v) for v in values])
    work = work.loc[mask]
    if work.empty:
        return WeeklyFillRate(weekly=pd.DataFrame(columns=empty_cols))

    present = [c for c in _MEASURE_COLS if c in work.columns]
    weekly = (work.groupby(WEEK_START, as_index=False)[present].sum()
                  .sort_values(WEEK_START).reset_index(drop=True))

    if date_range is not None:
        lo, hi = date_range
        weekly = weekly[weekly[WEEK_START].dt.date.between(lo, hi)] \
            .reset_index(drop=True)
    if weekly.empty:
        return WeeklyFillRate(weekly=pd.DataFrame(columns=empty_cols))

    done_orig = weekly[COL_COMPLETED_ORIGINAL_LBS]
    done_shp = weekly[COL_COMPLETED_SHIPPED_LBS]
    weekly[FILL_RATE_GROSS] = np.where(done_orig > 0, done_shp / done_orig,
                                       np.nan)
    net_denom = done_orig - weekly[COL_COMPLETED_CANCELED_LBS]
    weekly[FILL_RATE_NET] = np.where(net_denom > 0, done_shp / net_denom,
                                     np.nan)
    weekly[CUT_RATE] = np.where(weekly[COL_ORIGINAL_ORDERED_LBS] > 0,
                                weekly[COL_CANCELED_LBS]
                                / weekly[COL_ORIGINAL_ORDERED_LBS], np.nan)

    return WeeklyFillRate(
        weekly=weekly,
        total_ordered=float(weekly[COL_ORDERED_LBS].sum()),
        total_original=float(weekly[COL_ORIGINAL_ORDERED_LBS].sum()),
        total_shipped=float(weekly[COL_SHIPPED_LBS].sum()),
        total_cut=float(weekly[COL_CANCELED_LBS].sum()),
        completed_original=float(done_orig.sum()),
        completed_shipped=float(done_shp.sum()),
        completed_cut=float(weekly[COL_COMPLETED_CANCELED_LBS].sum()),
        open_lines=int(weekly[COL_OPEN_LINES].sum())
        if COL_OPEN_LINES in weekly.columns else 0,
    )


# ── I/O: token-bound DuckDB reads ────────────────────────────────────────────

def _table_uri(table: str) -> str:
    return (
        f"abfss://{_WORKSPACE_GUID}@onelake.dfs.fabric.microsoft.com/"
        f"{_LAKEHOUSE_GUID}/Tables/{_SCHEMA}/{table}"
    )


def _select(cols: tuple[str, ...]) -> str:
    return ", ".join(f'"{c}"' for c in cols)


@st.cache_data(ttl=_CACHE_TTL_SECONDS, show_spinner=False)
def _cached_orders_fill_fetch(_cache_token: str) -> pd.DataFrame:
    """Read the three tables, then shape them into the weekly × dim frame.

    Each read is column-projected with NO predicate — see the module
    docstring for why a pushed-down string filter breaks the delta kernel.
    """
    try:
        token = acquire_storage_token()
    except FabricAuthError as exc:
        raise OrdersFillRateError(str(exc)) from exc

    ssl_verify = prepare_duckdb_tls()
    try:
        con = get_duckdb_connection()
        with duckdb_lock():
            bind_storage_token(con, token, ssl_verify=ssl_verify)
            orders = con.execute(
                f"SELECT {_select(_ORDERS_COLS)} "
                f"FROM delta_scan('{_table_uri(_TABLE_ORDERS)}')").df()
            products = con.execute(
                f"SELECT {_select(_PRODUCTS_COLS)} "
                f"FROM delta_scan('{_table_uri(_TABLE_PRODUCTS)}')").df()
            customers = con.execute(
                f"SELECT {_select(_CUSTOMERS_COLS)} "
                f"FROM delta_scan('{_table_uri(_TABLE_CUSTOMERS)}')").df()
    except OrdersFillRateError:
        raise
    except FabricAuthError as exc:
        raise OrdersFillRateError(str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise OrdersFillRateError(
            f"Could not read dbo.Orders / dbo.Products / dbo.Customers via "
            f"DuckDB at {_table_uri(_TABLE_ORDERS)}.  Verify the lakehouse "
            f"identifiers and your Read access.  Underlying error: "
            f"{exc}{_tls_hint(exc)}"
        ) from exc

    return tidy_orders(orders, products, customers)


def fetch_orders_fill_df(*, force_refresh: bool = False) -> pd.DataFrame:
    """Return the weekly × dimension order-book frame for the volume chart.

    Columns: :data:`WEEK_START`, the canonical dimension names shared with
    :mod:`shipments_velocity`, and the measures in :data:`_MEASURE_COLS`.
    Corporate Group is NOT attached here — the page attaches it from its own
    ship-to lookup so the values match the section's dropdown.

    Raises :class:`OrdersFillRateError` on any read or shaping failure.
    """
    if force_refresh:
        _cached_orders_fill_fetch.clear()
    return _cached_orders_fill_fetch("default")


__all__ = [
    "OrdersFillRateError",
    "WeeklyFillRate",
    "COMPLETED_LINE_STATUSES",
    "COL_ORDERED_LBS", "COL_ORIGINAL_ORDERED_LBS", "COL_CANCELED_LBS",
    "COL_SHIPPED_LBS", "COL_COMPLETED_ORIGINAL_LBS",
    "COL_COMPLETED_SHIPPED_LBS", "COL_COMPLETED_CANCELED_LBS",
    "COL_LINES", "COL_OPEN_LINES",
    "FILL_RATE_GROSS", "FILL_RATE_NET", "CUT_RATE",
    "dedupe_products", "dedupe_customers", "scope_sales_orders",
    "join_dimensions", "week_start", "tidy_orders", "build_weekly_fill_rate",
    "fetch_orders_fill_df",
]
