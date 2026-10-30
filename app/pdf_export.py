"""PDF result sheet export (proposal: "PDF result sheet").

Uses fpdf2 when installed; returns ``None`` otherwise so the route can show
a friendly message instead of a stack trace.

Text safety: fpdf2's built-in Helvetica only covers cp1252, and even Unicode
TTFs miss symbols such as ✓ or emoji. Every dynamic string therefore passes
through ``_clean`` — the active font's glyph coverage decides what is kept,
with a punctuation fallback table (em dash → "-", … → "...") for everything
else — so an exotic student name or exam title can never crash the export.
"""

from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)

#: Unicode punctuation / symbols -> ASCII, used for glyphs the font lacks.
_REPL = {
    "\u00a0": " ",      # no-break space
    "\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-",
    "\u2014": "-", "\u2015": "-",          # dashes (exam titles use these)
    "\u2018": "'", "\u2019": "'", "\u201a": "'",
    "\u201c": '"', "\u201d": '"', "\u201e": '"',
    "\u2022": "-",                          # bullet
    "\u2026": "...",                        # ellipsis
    "\u00b7": "-",                          # middle dot
    "\u2190": "<-", "\u2192": "->", "\u2194": "<->",
    "\u2713": "v", "\u2714": "v",           # check marks
    "\u2717": "x", "\u2718": "x",           # cross marks
    "\u2705": "ok", "\u274c": "x",          # emoji ticks
    "\u26a0": "!",                          # warning sign
    "\u2605": "*", "\u2606": "*",           # stars
    "\u2610": "[ ]", "\u2611": "[x]",       # ballot boxes
}

#: Glyphs fpdf2's core Helvetica can encode (cp1252 printable range).
_CORE_COVERAGE = set(range(0x20, 0x7F)) | set(range(0xA0, 0x100))

#: (regular, bold, italic) candidate files per platform + env override.
_FONT_DIRS = [
    # Windows
    Path("C:/Windows/Fonts"),
    # Linux
    Path("/usr/share/fonts/truetype/dejavu"),
    Path("/usr/share/fonts/truetype/liberation"),
    Path("/usr/share/fonts/truetype/freefont"),
    # macOS
    Path("/System/Library/Fonts/Supplemental"),
    Path("/Library/Fonts"),
]
_REGULAR = ("arial.ttf", "segoeui.ttf", "tahoma.ttf", "verdana.ttf",
            "DejaVuSans.ttf", "LiberationSans-Regular.ttf", "FreeSans.ttf")
_BOLD = ("arialbd.ttf", "segoeuib.ttf", "tahomabd.ttf", "verdanab.ttf",
         "DejaVuSans-Bold.ttf", "LiberationSans-Bold.ttf", "FreeSansBold.ttf")
_ITALIC = ("ariali.ttf", "segoeuii.ttf", "tahoma.ttf", "verdanai.ttf",
           "DejaVuSans-Oblique.ttf", "LiberationSans-Italic.ttf",
           "FreeSansOblique.ttf")


def _find_fonts() -> tuple[Path | None, Path | None, Path | None]:
    """Locate a Unicode TTF triple (regular, bold, italic) if one exists."""
    import os

    override = os.environ.get("EXAM_PDF_FONT")
    if override and Path(override).is_file():
        p = Path(override)
        return p, p, p
    for folder in _FONT_DIRS:
        if not folder.is_dir():
            continue
        regular = next((folder / n for n in _REGULAR if (folder / n).is_file()),
                       None)
        if regular is None:
            continue
        bold = next((folder / n for n in _BOLD if (folder / n).is_file()), regular)
        italic = next((folder / n for n in _ITALIC if (folder / n).is_file()), regular)
        return regular, bold, italic
    return None, None, None


def _font_cmap(path: Path) -> set[int] | None:
    """Code points covered by *path* (None when fontTools cannot read it)."""
    try:
        from fontTools.ttLib import TTFont

        with TTFont(str(path)) as ft:
            cmap = ft.getBestCmap()
            return set(cmap) if cmap else None
    except Exception:  # pragma: no cover - defensive
        return None


def _fpdf():
    try:
        from fpdf import FPDF
        return FPDF
    except Exception:
        return None


def result_sheet_pdf(exam, attempts, summary: dict) -> bytes | None:
    """A4 result sheet for *exam*; ``None`` when the PDF cannot be produced."""
    FPDF = _fpdf()
    if FPDF is None:
        return None
    try:
        return _build(FPDF, exam, attempts, summary)
    except Exception:  # noqa: BLE001 - a download must never 500
        log.exception("PDF export failed for exam %s", exam.id)
        return None


def _build(FPDF, exam, attempts, summary: dict) -> bytes:
    regular, bold, italic = _find_fonts()
    pdf = FPDF(format="A4")
    pdf.set_auto_page_break(auto=True, margin=15)

    if regular is not None:
        family = "Body"
        try:
            pdf.add_font(family, "", str(regular))
            pdf.add_font(family, "B", str(bold))
            pdf.add_font(family, "I", str(italic))
            coverage = _font_cmap(regular) or set(range(0x20, 0x7F))
        except Exception:  # pragma: no cover - fall back to core font
            family = "Helvetica"
            coverage = _CORE_COVERAGE
    else:
        family = "Helvetica"
        coverage = _CORE_COVERAGE

    def s(text) -> str:
        """Sanitize one string against the active font's glyph coverage."""
        return _clean(str(text) if text is not None else "", coverage)

    def font(style: str, size: float) -> None:
        pdf.set_font(family, style, size)

    pdf.add_page()

    font("B", 16)
    pdf.cell(0, 10, "Result sheet", new_x="LMARGIN", new_y="NEXT")
    font("", 11)
    pdf.cell(0, 7, s(f"Exam: {exam.title}"), new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 7,
             f"Pass mark: {exam.pass_mark}%   |   "
             f"Questions: {len(exam.questions)}   |   "
             f"Duration: {exam.duration_minutes} min",
             new_x="LMARGIN", new_y="NEXT")
    if summary.get("count"):
        pdf.cell(0, 7,
                 f"Average: {_pct(summary['average'])}%   |   "
                 f"Highest: {_pct(summary['highest'])}%   |   "
                 f"Lowest: {_pct(summary['lowest'])}%   |   "
                 f"Pass rate: {_pct(summary['pass_rate'])}%",
                 new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)

    # Table header.
    font("B", 10)
    widths = [45, 18, 30, 30, 25, 25, 22]
    headers = ["Student", "Try", "Score", "Percent", "Warnings", "Risk", "Status"]
    for width, header in zip(widths, headers):
        pdf.cell(width, 8, s(header), border=1)
    pdf.ln()

    font("", 9)
    for attempt in attempts:
        percent = round(100 * attempt.score / attempt.total, 1) \
            if attempt.total and attempt.score is not None else None
        values = [
            (attempt.student.display_name or "")[:24],
            str(attempt.attempt_no),
            f"{_num(attempt.score)}/{_num(attempt.total)}",
            f"{_pct(percent)}%" if percent is not None else "-",
            str(attempt.warning_count),
            f"{attempt.risk_score} {attempt.risk_level or ''}".strip() or "-",
            attempt.status,
        ]
        for width, value in zip(widths, values):
            pdf.cell(width, 7, s(str(value)[:28]), border=1)
        pdf.ln()

    if not attempts:
        pdf.cell(0, 10, "No attempts yet.", new_x="LMARGIN", new_y="NEXT")

    pdf.ln(6)
    font("I", 8)
    pdf.cell(0, 6,
             s("Generated by the Virtual Exam System — tamper-evident "
                "evidence chains are verifiable in the app."),
             new_x="LMARGIN", new_y="NEXT")
    return bytes(pdf.output())


def _clean(text: str, coverage: set[int]) -> str:
    """Keep glyphs the font can draw; map the rest via ``_REPL`` or "?"."""
    out = []
    for ch in text:
        cp = ord(ch)
        if cp in coverage:
            out.append(ch)
        elif ch in _REPL:
            out.append(_REPL[ch])
        else:
            out.append("?")
    return "".join(out)


def _pct(value) -> str:
    """100.0 -> '100', 66.7 -> '66.7', None -> '-'."""
    if value is None:
        return "-"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _num(value) -> str:
    if value is None:
        return "-"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)
