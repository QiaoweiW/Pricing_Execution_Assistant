"""Minimal Word-document builder, so a page can offer its content as .docx.

Why this exists
---------------
The Documentation page is the one thing in the app people want to send to
someone who has no app access — a new planner, an auditor, a manager asking
"how is that number built?".  Screenshotting a Streamlit page is a poor answer.

This module owns *formatting only* and knows nothing about any particular
page's content: :class:`DocBuilder` exposes headings, paragraphs, bullets,
numbered steps, monospace blocks, callouts and two-column tables, and hands
back the finished file as bytes.  The page supplies the words.  Keeping the
split there means the manual's content stays beside the manual's UI, and this
file stays reusable if a second page ever wants a Word export.

``python-docx`` is an optional import
-------------------------------------
:data:`AVAILABLE` is ``False`` when it is not installed, and every entry point
raises :class:`DocxUnavailable` rather than blowing up at import time.  The
Documentation page is the app's landing page *and* its only sign-in surface —
it has to render even when something in the environment is missing, because
that is where people come to find out why.
"""
from __future__ import annotations

import io
from typing import Iterable, Optional, Sequence

try:                                              # pragma: no cover - env dep
    from docx import Document
    from docx.enum.table import WD_TABLE_ALIGNMENT
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.opc.constants import RELATIONSHIP_TYPE
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Pt, RGBColor
    AVAILABLE = True
except ImportError:                               # pragma: no cover - env dep
    AVAILABLE = False


class DocxUnavailable(RuntimeError):
    """Raised when a Word export is attempted without ``python-docx``."""


#: Darigold red, reused for headings so the export looks like the app.
_ACCENT = (0xD3, 0x2F, 0x2F)
_GREY = (0x60, 0x6A, 0x70)
_CODE_BG = "F2F4F5"


def _require() -> None:
    if not AVAILABLE:
        raise DocxUnavailable(
            "python-docx is not installed, so the Word export is unavailable. "
            "Add `python-docx>=1.1.0` to requirements.txt and redeploy."
        )


def _shade(element, fill: str) -> None:
    """Paint a paragraph / cell background (python-docx has no API for this)."""
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), fill)
    element.get_or_add_pPr().append(shd) if hasattr(element, "get_or_add_pPr") \
        else element.append(shd)


class DocBuilder:
    """Accumulate a Word document, then :meth:`to_bytes` it.

    Every method returns ``self`` so a page can chain, but the calls read
    perfectly well one per line and that is how the manual uses them.
    """

    def __init__(self, title: str, subtitle: str = "") -> None:
        _require()
        self.doc = Document()
        self._style_normal()
        head = self.doc.add_paragraph()
        run = head.add_run(title)
        run.bold = True
        run.font.size = Pt(26)
        run.font.color.rgb = RGBColor(*_ACCENT)
        if subtitle:
            sub = self.doc.add_paragraph()
            srun = sub.add_run(subtitle)
            srun.font.size = Pt(10.5)
            srun.font.color.rgb = RGBColor(*_GREY)

    # ── document-wide setup ────────────────────────────────────────────────
    def _style_normal(self) -> None:
        """Readable body text; Word's 11pt Calibri default is loose for this."""
        style = self.doc.styles["Normal"]
        style.font.name = "Calibri"
        style.font.size = Pt(10.5)
        style.paragraph_format.space_after = Pt(6)

    # ── block elements ─────────────────────────────────────────────────────
    def heading(self, text: str, level: int = 1) -> "DocBuilder":
        """A numbered-outline heading (level 1 = a top-level section)."""
        h = self.doc.add_heading(text, level=level)
        for run in h.runs:
            run.font.color.rgb = RGBColor(*_ACCENT) if level == 1 \
                else RGBColor(0x22, 0x28, 0x2B)
        return self

    def para(self, text: str, *, italic: bool = False,
             grey: bool = False, bold: bool = False) -> "DocBuilder":
        p = self.doc.add_paragraph()
        run = p.add_run(text)
        run.italic, run.bold = italic, bold
        if grey:
            run.font.size = Pt(9.5)
            run.font.color.rgb = RGBColor(*_GREY)
        return self

    def bullets(self, items: Iterable[str]) -> "DocBuilder":
        for item in items:
            self.doc.add_paragraph(str(item), style="List Bullet")
        return self

    def numbered(self, items: Iterable[str]) -> "DocBuilder":
        for item in items:
            self.doc.add_paragraph(str(item), style="List Number")
        return self

    def code(self, text: str) -> "DocBuilder":
        """A shaded monospace block — used for the formulas.

        Written line by line with explicit breaks: Word collapses a run's
        newlines, so a naive single run would render the whole formula on one
        unreadable line.
        """
        p = self.doc.add_paragraph()
        p.paragraph_format.space_after = Pt(10)
        run = p.add_run()
        run.font.name = "Consolas"
        run.font.size = Pt(9)
        lines = text.rstrip().split("\n")
        for i, line in enumerate(lines):
            if i:
                run.add_break()
            run.add_text(line)
        _shade(p._p, _CODE_BG)
        return self

    def callout(self, label: str, text: str) -> "DocBuilder":
        """A bold-labelled note — the page's ``st.info`` / ``st.warning``."""
        p = self.doc.add_paragraph()
        lab = p.add_run(f"{label} ")
        lab.bold = True
        lab.font.color.rgb = RGBColor(*_ACCENT)
        p.add_run(text)
        return self

    def table(self, headers: Sequence[str],
              rows: Iterable[Sequence[str]],
              links: Optional[Iterable[Optional[str]]] = None) -> "DocBuilder":
        """A header + body table.

        *links*, when given, is one URL per row applied to the FIRST cell, so
        the lakehouse index stays clickable in Word exactly as it is in the app.
        """
        rows = [list(r) for r in rows]
        table = self.doc.add_table(rows=1, cols=len(headers))
        table.style = "Table Grid"
        table.alignment = WD_TABLE_ALIGNMENT.LEFT
        for cell, text in zip(table.rows[0].cells, headers):
            cell.text = ""
            run = cell.paragraphs[0].add_run(str(text))
            run.bold = True
            run.font.size = Pt(9.5)
            _shade(cell._tc, _CODE_BG)
        link_list = list(links) if links is not None else [None] * len(rows)
        for row, url in zip(rows, link_list):
            cells = table.add_row().cells
            for i, (cell, text) in enumerate(zip(cells, row)):
                cell.text = ""
                p = cell.paragraphs[0]
                if i == 0 and url:
                    self._hyperlink(p, str(text), url)
                else:
                    run = p.add_run(str(text))
                    run.font.size = Pt(9.5)
        return self

    def link_bullets(self, items: Iterable) -> "DocBuilder":
        """Bulleted ``(label, url, trailing_text)`` rows as real hyperlinks."""
        for label, url, trailing in items:
            p = self.doc.add_paragraph(style="List Bullet")
            self._hyperlink(p, str(label), str(url))
            if trailing:
                p.add_run(f" — {trailing}")
        return self

    def page_break(self) -> "DocBuilder":
        self.doc.add_page_break()
        return self

    def rule(self) -> "DocBuilder":
        """A thin horizontal separator between sections."""
        p = self.doc.add_paragraph()
        p.paragraph_format.space_after = Pt(2)
        pbdr = OxmlElement("w:pBdr")
        bottom = OxmlElement("w:bottom")
        bottom.set(qn("w:val"), "single")
        bottom.set(qn("w:sz"), "6")
        bottom.set(qn("w:color"), "D8DEDF")
        pbdr.append(bottom)
        p._p.get_or_add_pPr().append(pbdr)
        return self

    # ── internals ──────────────────────────────────────────────────────────
    def _hyperlink(self, paragraph, text: str, url: str) -> None:
        """Insert a real clickable hyperlink (python-docx exposes no helper)."""
        r_id = paragraph.part.relate_to(
            url, RELATIONSHIP_TYPE.HYPERLINK, is_external=True)
        link = OxmlElement("w:hyperlink")
        link.set(qn("r:id"), r_id)
        run = OxmlElement("w:r")
        props = OxmlElement("w:rPr")
        colour = OxmlElement("w:color")
        colour.set(qn("w:val"), "0F6152")
        underline = OxmlElement("w:u")
        underline.set(qn("w:val"), "single")
        size = OxmlElement("w:sz")
        size.set(qn("w:val"), "19")          # half-points → 9.5pt
        props.append(colour)
        props.append(underline)
        props.append(size)
        run.append(props)
        text_el = OxmlElement("w:t")
        text_el.text = text
        run.append(text_el)
        link.append(run)
        paragraph._p.append(link)

    def footer_note(self, text: str) -> "DocBuilder":
        """A centred grey line — used for the generated-on stamp."""
        p = self.doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run = p.add_run(text)
        run.italic = True
        run.font.size = Pt(8.5)
        run.font.color.rgb = RGBColor(*_GREY)
        return self

    def to_bytes(self) -> bytes:
        buf = io.BytesIO()
        self.doc.save(buf)
        return buf.getvalue()
