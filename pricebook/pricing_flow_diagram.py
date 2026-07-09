"""Reference figure for the Pricebook Editor: how a submitted price flows
through Oracle Pricing and the methods used to verify it.

This is a hand-built, self-contained **inline SVG** (not a raster screenshot)
so it stays crisp at any width, respects the app font, and needs no binary
asset in the repo.  It is presented on its own white "figure card" so it
reads identically in either Streamlit theme.

The single public entry point is :func:`render`, which draws a foldable
section wrapping the figure — the working editor below it stays primary.
"""
from __future__ import annotations

import streamlit as st

# ── Palette (mirrors the source diagram) ─────────────────────────────────────
_PIPE_FILL, _PIPE_STROKE = "#ECE8E1", "#D6CFC4"      # taupe pipeline stages
_PIPE_TITLE, _PIPE_SUB = "#3B3833", "#837D73"
_CARD_FILL, _CARD_STROKE = "#D9EDE7", "#B7DED2"      # mint verification cards
_CARD_TITLE, _CARD_SUB = "#2C6A58", "#5E8A7C"
_LEGEND_FILL = "#FFFFFF"
_ARROW = "#A9A296"                                    # solid flow arrows
_DASH = "#C7C1B7"                                     # dashed cross-links
_LABEL = "#8A847A"                                    # small annotation text

_FONT = "Inter, 'Segoe UI', Roboto, system-ui, sans-serif"


# ── Low-level SVG builders ───────────────────────────────────────────────────

def _lines_block(cx: float, y: float, h: float, lines: list[tuple]) -> str:
    """Vertically-centre a block of horizontally-centred text lines in a box.

    *lines* is a list of ``(text, size, weight, color)`` tuples.
    """
    gap = 4.0
    total = sum(size for _t, size, _w, _c in lines) + gap * (len(lines) - 1)
    top = y + (h - total) / 2.0
    out: list[str] = []
    cursor = top
    for text, size, weight, color in lines:
        cursor += size
        out.append(
            f'<text x="{cx:.1f}" y="{cursor:.1f}" text-anchor="middle" '
            f'font-size="{size}" font-weight="{weight}" fill="{color}">'
            f"{_esc(text)}</text>"
        )
        cursor += gap
    return "".join(out)


def _box(
    x: float, y: float, w: float, h: float,
    title: str, subs: list[str], *,
    fill: str, stroke: str, title_color: str, sub_color: str,
    title_size: int = 13, sub_size: int = 10,
) -> str:
    """A rounded stage/card box with a bold title and 0-N subtitle lines."""
    lines = [(title, title_size, 700, title_color)]
    lines += [(s, sub_size, 400, sub_color) for s in subs]
    return (
        f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="10" ry="10" '
        f'fill="{fill}" stroke="{stroke}" stroke-width="1.4"/>'
        + _lines_block(x + w / 2.0, y, h, lines)
    )


def _flow_arrow(cx: float, y1: float, y2: float) -> str:
    """A solid vertical flow arrow at *cx* from *y1* down to *y2*.

    The arrowhead is an explicit filled triangle (not an SVG ``marker``) so
    the figure renders identically regardless of the host's SVG sanitiser.
    """
    return (
        f'<line x1="{cx}" y1="{y1}" x2="{cx}" y2="{y2 - 5:.1f}" '
        f'stroke="{_ARROW}" stroke-width="1.8"/>'
        f'<path d="M{cx - 4.5:.1f},{y2 - 6:.1f} L{cx + 4.5:.1f},{y2 - 6:.1f} '
        f'L{cx:.1f},{y2:.1f} z" fill="{_ARROW}"/>'
    )


def _dash(path: str) -> str:
    """A subtle dashed connector (no arrowhead), used for verification links."""
    return (
        f'<path d="{path}" fill="none" stroke="{_DASH}" stroke-width="1.4" '
        f'stroke-dasharray="4 3"/>'
    )


def _esc(text: str) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ── Figure assembly ──────────────────────────────────────────────────────────

def _svg() -> str:
    """Return the full inline SVG for the pricing-flow reference figure."""
    parts: list[str] = [
        '<svg viewBox="0 0 640 638" width="100%" '
        'preserveAspectRatio="xMidYMid meet" role="img" '
        'aria-label="Oracle pricing flow and verification methods" '
        f'style="font-family:{_FONT};height:auto;max-height:640px;">',
    ]

    def pipe(x, y, w, h, t, subs):
        return _box(x, y, w, h, t, subs, fill=_PIPE_FILL, stroke=_PIPE_STROKE,
                    title_color=_PIPE_TITLE, sub_color=_PIPE_SUB)

    def card(x, y, w, h, t, subs):
        return _box(x, y, w, h, t, subs, fill=_CARD_FILL, stroke=_CARD_STROKE,
                    title_color=_CARD_TITLE, sub_color=_CARD_SUB)

    # ── Left: submit → PO pipeline ────────────────────────────────
    parts += [
        pipe(44, 26, 196, 54, "Submit price", ["VBCS / REST API / FBDI"]),
        pipe(44, 112, 196, 54, "Interface table", ["Staging area"]),
        pipe(44, 198, 196, 54, "Transactional tables", ["Live pricing data"]),
        pipe(44, 284, 196, 66, "Pricing engine",
             ["Strategies, rules, qualifiers,", "dates, UOM"]),
        pipe(44, 384, 196, 48, "PO picks up price", []),
        _flow_arrow(142, 80, 110),
        _flow_arrow(142, 166, 196),
        _flow_arrow(142, 252, 282),
        _flow_arrow(142, 350, 382),
        f'<text x="250" y="186" font-size="11" fill="{_LABEL}" '
        f'font-style="italic">ESS import job</text>',
    ]

    # ── Right: verification methods ───────────────────────────────
    parts += [
        f'<text x="366" y="42" font-size="15" font-weight="700" '
        f'fill="{_PIPE_TITLE}">Verification methods</text>',
        card(366, 58, 250, 56, "Price list lookup", ["Pricing Administration UI"]),
        card(366, 130, 250, 56, "REST API query", ["priceLists endpoint"]),
        card(366, 202, 250, 56, "Price book",
             ["Pre-calculated prices", "via REST API"]),
        card(366, 274, 250, 60, "Draft order test",
             ["Runs full pricing engine", "in Order Management"]),
        # Which stage each method reads from (dashed cross-links).
        _dash("M240,214 C300,214 306,86 366,86"),
        _dash("M240,236 C300,236 306,158 366,158"),
        _dash("M240,306 C300,306 306,230 366,230"),
        _dash("M240,326 C300,326 306,304 366,304"),
    ]

    # ── ESS refresh job → reporting layer ─────────────────────────
    parts += [
        _dash("M300,352 C300,440 322,470 392,472"),
        f'<text transform="translate(296,404) rotate(-90)" font-size="11" '
        f'fill="{_LABEL}" font-style="italic" text-anchor="middle">'
        "ESS refresh job</text>",
        pipe(392, 452, 200, 50, "Reporting tables", []),
        _flow_arrow(492, 502, 534),
        pipe(372, 536, 240, 58, "BI Publisher report", ["DG Price Build Report"]),
        f'<text x="492" y="612" text-anchor="middle" font-size="10" '
        f'fill="{_LABEL}">May lag behind transactional data</text>',
        f'<text x="492" y="626" text-anchor="middle" font-size="10" '
        f'fill="{_LABEL}">depending on refresh schedule</text>',
    ]

    # ── Legend: what each method checks ───────────────────────────
    parts.append(
        f'<rect x="40" y="452" width="300" height="174" rx="10" ry="10" '
        f'fill="{_LEGEND_FILL}" stroke="{_PIPE_STROKE}" stroke-width="1.4"/>'
    )
    parts.append(
        f'<text x="56" y="480" font-size="13" font-weight="700" '
        f'fill="{_PIPE_TITLE}">What each method checks</text>'
    )
    legend = [
        ("Price list lookup", "Data exists"),
        ("REST API query", "Data exists"),
        ("Price book", "Calculated price"),
        ("Draft order test", "Full engine logic"),
        ("BI report", "Reporting snapshot"),
    ]
    row_y = 508
    for method, checks in legend:
        parts.append(
            f'<text x="56" y="{row_y}" font-size="11" fill="{_PIPE_TITLE}">'
            f"{_esc(method)}</text>"
            f'<text x="205" y="{row_y}" font-size="11" fill="{_PIPE_SUB}">'
            f"{_esc(checks)}</text>"
        )
        row_y += 23

    parts.append("</svg>")
    return "".join(parts)


def render(*, expanded: bool = True) -> None:
    """Render the pricing-flow reference figure inside a foldable section.

    Parameters
    ----------
    expanded
        Whether the section starts open.  Default open so the figure is
        visible on load; the planner can collapse it to focus on the editor.
    """
    with st.expander("📈 How a price flows — and how to verify it", expanded=expanded):
        st.markdown(
            '<div style="background:#ffffff;border:1px solid #E4DFD6;'
            "border-radius:14px;padding:18px 20px;margin:2px 0 6px;"
            'box-shadow:0 1px 3px rgba(60,50,40,0.06);overflow-x:auto;">'
            f"{_svg()}</div>",
            unsafe_allow_html=True,
        )
        st.caption(
            "A submitted price flows down the left pipeline into live "
            "transactional data and the pricing engine before a PO picks it "
            "up.  The five verification methods (right) each read from a "
            "different stage — so a price can *exist* (list lookup / REST) "
            "yet still price differently once the **engine** runs (Price "
            "book / Draft order), and the **BI report** may lag behind live "
            "data on its refresh schedule."
        )
