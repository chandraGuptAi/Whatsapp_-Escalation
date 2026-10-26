"""Saved Markdown report -> branded A4 PDF using the existing ReportLab dependency."""

from __future__ import annotations

import io
import os
import re
from datetime import datetime
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    Flowable,
    LongTable,
    Paragraph,
    Preformatted,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from ..utils.ist import IST

BLUE = colors.HexColor("#0866FF")
NAVY = colors.HexColor("#0B1B40")
MUTED = colors.HexColor("#64748B")
BORDER = colors.HexColor("#E2E8F0")
ZEBRA = colors.HexColor("#F7F9FC")
# Docker installs fonts-dejavu-core; Windows dev machines have Arial.
FONT_CANDIDATES = [
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    ("C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/arialbd.ttf"),
]


_glyphs: set[int] | None = None  # Code points the body font can draw; None means Helvetica.


def _fonts() -> tuple[str, str]:
    """A Unicode TTF (for ₹ and accented names) when installed; Helvetica otherwise."""
    global _glyphs
    if "ReportSans" in pdfmetrics.getRegisteredFontNames():
        return "ReportSans", "ReportSans-Bold"
    for regular, bold in FONT_CANDIDATES:
        if os.path.exists(regular) and os.path.exists(bold):
            try:
                pdfmetrics.registerFont(TTFont("ReportSans", regular))
                pdfmetrics.registerFont(TTFont("ReportSans-Bold", bold))
                pdfmetrics.registerFontFamily(
                    "ReportSans", normal="ReportSans", bold="ReportSans-Bold",
                    italic="ReportSans", boldItalic="ReportSans-Bold",
                )
                _glyphs = set(pdfmetrics.getFont("ReportSans").face.charToGlyph)
                return "ReportSans", "ReportSans-Bold"
            except Exception:
                break
    return "Helvetica", "Helvetica-Bold"


SEP = "␟"  # Stands in for "|" inside group tokens so table rows split only on real cell borders.


def _protect(text: str) -> str:
    return re.sub(r"\{\{G:[^{}]*\}\}", lambda m: m[0].replace("|", SEP), text)


def _clean(text: str) -> str:
    """Drop characters the font cannot draw (emoji, skin tones, joiners) instead of printing boxes."""
    if _glyphs is None:
        return "".join(ch for ch in text if ord(ch) < 0x2100 or ch == SEP)
    return "".join(ch for ch in text if ord(ch) < 0x80 or ord(ch) in _glyphs or ch == SEP)


def _inline(text: str) -> str:
    text = escape(_clean(text).strip())
    # Master reports cite groups as {{G:<id>|<name>}}; print the name only.
    text = re.sub(
        r"\{\{G:[^{}" + SEP + r"]*" + SEP + r"([^{}]*)\}\}",
        lambda m: '<font color="#0650CC"><b>' + m[1].replace(SEP, "|") + "</b></font>",
        _protect(text),
    )
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"<i>\1</i>", text)
    return re.sub(r"`([^`]+)`", r'<font face="Courier" color="#334155">\1</font>', text)


class _Rule(Flowable):
    def __init__(self, width: float):
        super().__init__()
        self.width = width

    def wrap(self, *_):
        return self.width, 8

    def draw(self):
        self.canv.setStrokeColor(BORDER)
        self.canv.setLineWidth(0.6)
        self.canv.line(0, 4, self.width, 4)


def _styles(regular: str, bold: str) -> dict:
    def style(name, font=regular, **extra):
        return ParagraphStyle(name, fontName=font, textColor=NAVY, alignment=TA_LEFT, **extra)

    return {
        "h1": style("h1", bold, fontSize=16, leading=20, spaceBefore=12, spaceAfter=6, keepWithNext=1),
        "h2": style("h2", bold, fontSize=13.5, leading=17, spaceBefore=12, spaceAfter=5, keepWithNext=1),
        "h3": style("h3", bold, fontSize=11.5, leading=15, spaceBefore=9, spaceAfter=4, keepWithNext=1),
        "body": style("body", fontSize=9.6, leading=14, spaceAfter=5),
        "bullet": style("bullet", fontSize=9.6, leading=14, leftIndent=14, bulletIndent=3, spaceAfter=2),
        "cell": style("cell", fontSize=8.2, leading=10.5),
        "head": ParagraphStyle("head", fontName=bold, fontSize=8.2, leading=10.5, textColor=colors.white),
        "meta": ParagraphStyle("meta", fontName=regular, fontSize=7.4, leading=10, textColor=MUTED),
        "fact": style("fact", bold, fontSize=9.6, leading=13),
        "code": ParagraphStyle("code", fontName="Courier", fontSize=7.8, leading=10, textColor=colors.HexColor("#334155")),
    }


def _table(rows: list[list[str]], width: float, s: dict) -> LongTable:
    columns = max(len(r) for r in rows)
    rows = [r + [""] * (columns - len(r)) for r in rows]
    # Width follows content length, bounded so no column collapses or dominates.
    weights = [min(40, max(9, max(len(r[i]) for r in rows))) for i in range(columns)]
    widths = [width * w / sum(weights) for w in weights]
    data = [[Paragraph(_inline(c), s["head"] if i == 0 else s["cell"]) for c in row] for i, row in enumerate(rows)]
    table = LongTable(data, colWidths=widths, repeatRows=1, hAlign="LEFT")
    style = [
        ("BACKGROUND", (0, 0), (-1, 0), BLUE),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LINEBELOW", (0, 0), (-1, -1), 0.4, BORDER),
        ("BOX", (0, 0), (-1, -1), 0.4, BORDER),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
    ]
    style += [("BACKGROUND", (0, i), (-1, i), ZEBRA) for i in range(2, len(data), 2)]
    table.setStyle(TableStyle(style))
    return table


def _code(lines: list[str], width: float, s: dict) -> Table:
    block = Preformatted(_clean("\n".join(lines))[:20000], s["code"])
    return Table([[block]], colWidths=[width], style=[
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F1F5F9")),
        ("BOX", (0, 0), (-1, -1), 0.4, BORDER),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ])


def markdown_story(text: str, width: float, s: dict) -> list:
    """Headings, paragraphs, bullet/numbered lists, tables, code blocks and rules."""
    story: list = []
    table: list[list[str]] = []
    code: list[str] | None = None
    numbered = re.compile(r"^(\d+)[.)]\s+(.*)")
    for line in _protect(text).splitlines() + [""]:
        stripped = line.strip()
        if code is not None:
            if stripped.startswith("```"):
                story += [_code(code, width, s), Spacer(1, 6)]
                code = None
            else:
                code.append(line)
            continue
        if stripped.startswith("|") and "|" in stripped[1:]:
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            if not all(re.fullmatch(r":?-+:?", c.replace(" ", "")) for c in cells if c):
                table.append(cells)
            continue
        if table:
            story += [_table(table, width, s), Spacer(1, 8)]
            table = []
        if stripped.startswith("```"):
            code = []
        elif not stripped:
            continue
        elif re.fullmatch(r"[-*_]{3,}", stripped):
            story.append(_Rule(width))
        elif stripped.startswith("#"):
            level = len(stripped) - len(stripped.lstrip("#"))
            story.append(Paragraph(_inline(stripped.lstrip("# ")), s["h1" if level <= 1 else "h2" if level == 2 else "h3"]))
        elif stripped.startswith(("- ", "* ", "• ")):
            story.append(Paragraph(_inline(stripped[2:]), s["bullet"], bulletText="•"))
        elif numbered.match(stripped):
            number, rest = numbered.match(stripped).groups()
            story.append(Paragraph(_inline(rest), s["bullet"], bulletText=number + "."))
        else:
            story.append(Paragraph(_inline(stripped), s["body"]))
    if code:
        story.append(_code(code, width, s))
    return story


def report_pdf(report: dict, subject: str | None = None) -> bytes:
    regular, bold = _fonts()
    s = _styles(regular, bold)
    s["h2"].textColor = BLUE
    output = io.BytesIO()
    page_w, page_h = A4
    margin = 16 * mm
    width = page_w - 2 * margin
    master = report.get("scope") == "master"
    title = "Master Escalation Report" if master else "Group Escalation Report"
    subject = _clean(subject or report.get("group_name") or report.get("group_id") or "").strip()
    generated = datetime.fromtimestamp(report["created_at"], IST).strftime("%d %b %Y, %I:%M %p IST")
    window = report.get("window_label") or "—"
    facts = [
        ("Cluster" if master else "Group", subject),
        ("Period", window),
        ("Generated", generated),
        ("Messages analysed", f"{report.get('message_count') or 0:,}"),
    ]

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setStrokeColor(BORDER)
        canvas.line(margin, 12 * mm, page_w - margin, 12 * mm)
        canvas.setFont(regular, 7.5)
        canvas.setFillColor(MUTED)
        canvas.drawString(margin, 8 * mm, f"{title} · {subject} · {generated}")
        canvas.drawRightString(page_w - margin, 8 * mm, f"Page {doc.page}")
        canvas.restoreState()

    def first_page(canvas, doc):
        canvas.saveState()
        band = 34 * mm
        canvas.setFillColor(NAVY)
        canvas.rect(0, page_h - band, page_w, band, stroke=0, fill=1)
        canvas.setFillColor(BLUE)
        canvas.rect(0, page_h - band, page_w, 1.6 * mm, stroke=0, fill=1)
        canvas.setFillColor(colors.HexColor("#9DB8F2"))
        canvas.setFont(bold, 8.5)
        canvas.drawString(margin, page_h - 11 * mm, "BIGSHIP  ·  ESCALATION ANALYST")
        canvas.setFillColor(colors.white)
        canvas.setFont(bold, 19)
        canvas.drawString(margin, page_h - 20 * mm, title)
        canvas.setFont(regular, 10)
        canvas.setFillColor(colors.HexColor("#C7D5F0"))
        canvas.drawString(margin, page_h - 26.5 * mm, f"{subject}  ·  {window}")
        canvas.restoreState()
        footer(canvas, doc)

    document = SimpleDocTemplate(
        output, pagesize=A4, leftMargin=margin, rightMargin=margin, topMargin=16 * mm, bottomMargin=18 * mm,
        title=f"{title} - {subject}", author="Bigship Escalation Analyst",
    )
    summary = Table(
        [[Paragraph(escape(label.upper()), s["meta"]) for label, _ in facts],
         [Paragraph(escape(str(value)), s["fact"]) for _, value in facts]],
        colWidths=[width / len(facts)] * len(facts),
        style=[
            ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F5F8FF")),
            ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#D6E4FF")),
            ("LEFTPADDING", (0, 0), (-1, -1), 9),
            ("TOPPADDING", (0, 0), (-1, 0), 8),
            ("BOTTOMPADDING", (0, 0), (-1, 0), 1),
            ("BOTTOMPADDING", (0, -1), (-1, -1), 8),
        ],
    )
    story = [Spacer(1, 22 * mm), summary, Spacer(1, 12)]
    story += markdown_story(report.get("report") or "", width, s)
    document.build(story, onFirstPage=first_page, onLaterPages=footer)
    return output.getvalue()
