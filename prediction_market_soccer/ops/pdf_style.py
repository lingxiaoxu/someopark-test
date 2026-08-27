"""Shared PDF styling for prediction_market_soccer reports.

Mirrors the house style of the root pairs-book reports (PnLReport.py /
RiskManager.py): the same CJK font registration, the dark-navy header rows with a
gold underline, alternating row shading, and the money/pct colour convention.
Copied (not imported) so the prediction_market_soccer package stays fully isolated from
root-repo code — only the *visual* style is shared, never the logic.

Public surface:
  FONT, colours (C_*), paragraph styles (title/sub/h1/body/footer/note),
  money(), pct(), C()/H() table cells, make_table(), make_kv_table(),
  note_item(), bullet(), new_doc(), title_block(), section().
"""
from __future__ import annotations

import os

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import cm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    HRFlowable,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

# ── Font (CJK-capable, same search order as PnLReport.py) ─────────────────────
def _register_cjk() -> str:
    for path in (
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/STHeiti Light.ttc",
        "/Library/Fonts/Arial Unicode MS.ttf",
        "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    ):
        if os.path.exists(path):
            try:
                pdfmetrics.registerFont(TTFont("CJK", path))
                return "CJK"
            except Exception:
                continue
    return "Helvetica"


FONT = _register_cjk()

# ── House colours ─────────────────────────────────────────────────────────────
C_HEADER = colors.HexColor("#1a1a2e")
C_SUBHDR = colors.HexColor("#16213e")
C_ROW_ALT = colors.HexColor("#f7f9fc")
C_POS = colors.HexColor("#1a7a4a")
C_NEG = colors.HexColor("#c0392b")
C_GOLD = colors.HexColor("#d4a843")
C_BORDER = colors.HexColor("#cccccc")
C_GRAY = colors.HexColor("#888888")

PAGE_W = A4[0] - 4 * cm   # usable width inside 2cm margins


def S(name, **kw) -> ParagraphStyle:
    kw.setdefault("fontName", FONT)
    return ParagraphStyle(name, **kw)


title_style = S("TT", fontSize=15, leading=19, alignment=TA_CENTER, spaceAfter=4)
sub_style = S("ST", fontSize=8.5, leading=12, alignment=TA_CENTER,
              textColor=colors.HexColor("#555555"), spaceAfter=14)
h1_style = S("H1", fontSize=10, leading=14, textColor=C_HEADER, spaceBefore=12, spaceAfter=5)
body_style = S("BD", fontSize=8, leading=11.5)
note_style = S("NT", fontSize=7.5, leading=11, textColor=colors.HexColor("#444444"))
footer_style = S("FT", fontSize=7, leading=9.5, alignment=TA_CENTER, textColor=C_GRAY, spaceBefore=6)


# ── Value formatters (PnL colour convention) ──────────────────────────────────
def money(v, color=True, unit="") -> str:
    if v is None:
        return "N/A"
    s = f"+{abs(v):,.2f}{unit}" if v >= 0 else f"-{abs(v):,.2f}{unit}"
    if not color:
        return s
    c = "#1a7a4a" if v >= 0 else "#c0392b"
    return f'<font color="{c}">{s}</font>'


def pct(v) -> str:
    if v is None:
        return "N/A"
    s = f"+{abs(v):.2f}%" if v >= 0 else f"-{abs(v):.2f}%"
    c = "#1a7a4a" if v >= 0 else "#c0392b"
    return f'<font color="{c}"><b>{s}</b></font>'


# ── Table cells ───────────────────────────────────────────────────────────────
def C(text, align="LEFT", header=False) -> Paragraph:
    s = ParagraphStyle("_cx", fontName=FONT, fontSize=7.5, leading=10.5,
                       alignment=(TA_RIGHT if align == "RIGHT" else 0),
                       textColor=(colors.white if header else colors.black))
    content = f"<b>{text}</b>" if header else str(text)
    return Paragraph(content, s)


def H(text, align="LEFT") -> Paragraph:
    return C(text, align=align, header=True)


def make_table(data, col_widths, header_rows=1):
    """Dark-header table with gold underline + alternating row shading."""
    t = Table(data, colWidths=col_widths, repeatRows=header_rows)
    n = len(data)
    cmds = [
        ("FONTNAME", (0, 0), (-1, -1), FONT),
        ("FONTSIZE", (0, 0), (-1, -1), 7.5),
        ("LEADING", (0, 0), (-1, -1), 10.5),
        ("BACKGROUND", (0, 0), (-1, header_rows - 1), C_HEADER),
        ("TEXTCOLOR", (0, 0), (-1, header_rows - 1), colors.white),
        ("ALIGN", (0, 0), (-1, -1), "LEFT"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 3.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3.5),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("GRID", (0, 0), (-1, -1), 0.35, C_BORDER),
        ("LINEBELOW", (0, header_rows - 1), (-1, header_rows - 1), 0.9, C_GOLD),
    ]
    for i in range(header_rows, n):
        bg = C_ROW_ALT if (i - header_rows) % 2 == 1 else colors.white
        cmds.append(("BACKGROUND", (0, i), (-1, i), bg))
    t.setStyle(TableStyle(cmds))
    return t


def make_kv_table(rows, label_w=9.5 * cm, val_w=5 * cm):
    """Two-column label/value summary box with gold top/bottom rules."""
    sl = ParagraphStyle("_sl", fontName=FONT, fontSize=7.5, leading=10.5)
    sv = ParagraphStyle("_sv", fontName=FONT, fontSize=7.5, leading=10.5, alignment=TA_RIGHT)
    data = [[Paragraph(f"<b>{r[0]}</b>", sl), Paragraph(str(r[1]), sv)] for r in rows]
    t = Table(data, colWidths=[label_w, val_w])
    t.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), FONT),
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING", (0, 0), (-1, -1), 7),
        ("RIGHTPADDING", (0, 0), (-1, -1), 7),
        ("LINEABOVE", (0, 0), (-1, 0), 0.5, C_GOLD),
        ("LINEBELOW", (0, -1), (-1, -1), 0.5, C_GOLD),
        ("ROWBACKGROUNDS", (0, 0), (-1, -1), [colors.HexColor("#fafbfc"), colors.white]),
    ]))
    return t


def note_item(num: str, text: str, width: float = PAGE_W):
    sl = ParagraphStyle("_nl", fontName=FONT, fontSize=7.5, leading=10.5)
    st = ParagraphStyle("_nt", fontName=FONT, fontSize=7.5, leading=10.5)
    return Table(
        [[Paragraph(num, sl), Paragraph(text, st)]],
        colWidths=[0.6 * cm, width - 0.6 * cm],
        style=[
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING", (0, 0), (-1, -1), 1),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ],
    )


def bullet(text: str, color: str | None = None, width: float = PAGE_W):
    """A •-prefixed wrapping line (optional coloured marker, e.g. ⛔ rails)."""
    mark = "•" if color is None else text[:1]
    body = text if color is None else text[1:].strip()
    sl = ParagraphStyle("_bl", fontName=FONT, fontSize=7.5, leading=10.8,
                        textColor=(colors.HexColor(color) if color else colors.black))
    st = ParagraphStyle("_bt", fontName=FONT, fontSize=7.5, leading=10.8)
    return Table(
        [[Paragraph(f"<b>{mark}</b>", sl), Paragraph(body, st)]],
        colWidths=[0.55 * cm, width - 0.55 * cm],
        style=[
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING", (0, 0), (-1, -1), 1.2),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 1.2),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ],
    )


# ── Document scaffolding ──────────────────────────────────────────────────────
def new_doc(output_path: str) -> SimpleDocTemplate:
    return SimpleDocTemplate(
        output_path, pagesize=A4,
        leftMargin=2 * cm, rightMargin=2 * cm, topMargin=2 * cm, bottomMargin=2 * cm,
    )


def title_block(story: list, title: str, subtitle: str) -> None:
    story.append(Paragraph(title, title_style))
    story.append(Paragraph(subtitle, sub_style))
    story.append(HRFlowable(width="100%", thickness=1, color=C_GOLD, spaceAfter=10))


def section(story: list, heading: str) -> None:
    story.append(Paragraph(heading, h1_style))
