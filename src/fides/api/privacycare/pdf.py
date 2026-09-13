# Renders a Report (fides.api.privacycare.report — a document MODEL, no
# ReportLab in it at all) into the actual PDF bytes a DPO files with Kenya's
# Office of the Data Protection Commissioner. This module is the only place
# in PrivacyCare that imports ReportLab, and it does not query the database:
# it consumes the Report dataclass and nothing else (see report.py's module
# docstring for why the model/renderer split exists).
#
# Three failure modes this module exists to make LOUD instead of silent
# (task 3 brief, "Three things here each fail SILENTLY if done wrong"):
#
# 1. THE LAZY IMPORT. `import reportlab...` happens inside render_pdf, not
#    at module scope. If it were at module scope, a broken/missing
#    ReportLab install would raise ImportError while
#    fides.api.privacycare.api.router is being imported at process boot —
#    taking the ENTIRE API down before the first request, for a dependency
#    only one route needs. Import it lazily here so a broken install
#    instead fails only the one PDF request, as a 503 (see
#    api/reports.py's mapping of PDFRenderError).
#
# 2. THE FONT. ReportLab's built-in Type 1 fonts (Helvetica etc.) have no
#    subscript/superscript glyphs and no coverage outside Latin-1. Ask one
#    to draw a glyph it doesn't have and it does not raise — it silently
#    draws a SOLID BLACK BOX. This module files officer-typed text
#    verbatim, for a regulator; "CO<sub>2</sub>", "m<super>2</super>", a
#    currency symbol, or a Kenyan name with a diacritic corrupting into a
#    black box would be invisible until someone opened the PDF by eye. A
#    TrueType font with real Unicode coverage (DejaVu Sans, located via
#    fc-list rather than a hardcoded path — see _locate_font_file) is
#    registered and used for ALL body text; if none is found, this module
#    fails loudly (PDFRenderError) rather than falling back to a built-in
#    font, because that fallback is exactly the invisible failure being
#    guarded against.
#
# 3. THE FILENAME. api/reports.py sanitises the officer-supplied assessment
#    name before it reaches Content-Disposition; this module has no part in
#    that (see _sanitize_filename there), but is documented here because
#    the two files exist for the same reason: an officer's free text is
#    filed verbatim in the BODY, so it must be trusted in the body and
#    distrusted in the HEADER.
from __future__ import annotations

import os
import subprocess
from xml.sax.saxutils import escape as xml_escape

from fides.api.privacycare.report import Report, ReportQuestion, ReportSection


class PDFRenderError(Exception):
    """Raised when the PDF cannot be produced — a broken/missing ReportLab
    install, a missing Unicode font, or a build-time failure in ReportLab
    itself. api/reports.py maps this to a 503 whose JSON `detail` names the
    cause (str(exc)), which the admin UI's downloadAssessmentReport
    responseHandler surfaces to the officer as `errorJson.detail` — so the
    message passed here IS user-facing and should say what actually broke,
    not just "PDF failed".
    """


# The two font names this module registers ReportLab under. Never
# "Helvetica"/"Times-Roman"/any other ReportLab built-in — see the module
# docstring, point 2.
_FONT_NAME = "PrivacyCareBody"
_FONT_NAME_BOLD = "PrivacyCareBody-Bold"


def _locate_font_file(filename_suffix: str) -> str | None:
    """Find a font file on this host by filename suffix, via `fc-list`.

    Deliberately not a hardcoded path (the brief: "locate it rather than
    assuming a path"). `fc-list` prints "<path>: <family>:style=<style>" per
    installed font, one per face — this host has 8 DejaVu faces registered
    that way. Matching on the TTF's own filename (e.g. "DejaVuSans.ttf",
    "DejaVuSans-Bold.ttf") rather than the family name fc-list reports is
    robust to a host where DejaVu is installed but not fontconfig's default
    match for "sans-serif", and to fc-list's family/style text differing
    slightly across fontconfig versions.

    Returns None (never raises) on any failure to run fc-list or find a
    match — the caller (_register_unicode_font) is what decides that's fatal
    and raises PDFRenderError with a message actionable by whoever deploys
    this.
    """
    try:
        result = subprocess.run(
            ["fc-list"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        path = line.split(":", 1)[0].strip()
        if path.endswith(filename_suffix) and os.path.isfile(path):
            return path
    return None


def _register_unicode_font(pdfmetrics, ttfont_cls) -> tuple[str, str]:
    """Register a DejaVu Sans TrueType font (regular + bold) under
    _FONT_NAME / _FONT_NAME_BOLD, and register the pair as a ReportLab font
    FAMILY so that `<b>`/`<strong>` markup inside a Paragraph resolves to
    the real bold face rather than a synthetically slanted regular one.

    Raises PDFRenderError — never falls back to a ReportLab built-in font —
    if the regular face cannot be found. See the module docstring, point 2,
    for why a silent fallback here is exactly the failure mode this
    function exists to prevent.
    """
    regular_path = _locate_font_file("DejaVuSans.ttf")
    if regular_path is None:
        raise PDFRenderError(
            "No Unicode-capable TrueType font found on this host (looked "
            "for DejaVuSans.ttf via `fc-list`). Refusing to fall back to a "
            "ReportLab built-in Type 1 font: those silently render "
            "subscript/superscript, non-Latin and currency glyphs as solid "
            "black boxes instead of raising, which is unacceptable for a "
            "document filed with a regulator. Install a DejaVu Sans "
            "TrueType font (e.g. the fonts-dejavu-core / ttf-dejavu "
            "package) on this host and retry."
        )
    bold_path = _locate_font_file("DejaVuSans-Bold.ttf")

    pdfmetrics.registerFont(ttfont_cls(_FONT_NAME, regular_path))
    if bold_path is not None:
        pdfmetrics.registerFont(ttfont_cls(_FONT_NAME_BOLD, bold_path))
        bold_name = _FONT_NAME_BOLD
    else:
        # Bold face missing but regular found: still real Unicode coverage,
        # just no bold weight. Register the family pointing bold at the
        # regular face rather than failing — headings render un-bold
        # instead of as black boxes, which is a cosmetic loss, not a
        # correctness one.
        bold_name = _FONT_NAME

    pdfmetrics.registerFontFamily(
        _FONT_NAME,
        normal=_FONT_NAME,
        bold=bold_name,
        italic=_FONT_NAME,
        boldItalic=bold_name,
    )
    return _FONT_NAME, bold_name


def _escaped(text: str | None) -> str:
    """Escape officer-typed text for ReportLab's mini-XML Paragraph markup.

    report.py's build_report deliberately leaves answer_text untouched
    (see its own module docstring: "escaping belongs entirely to whatever
    eventually renders this data"). A Paragraph's text IS parsed as
    lightweight XML/HTML (that's how <sub>/<super>/<br/> work at all), so
    an officer's raw "R&D <partners>" would otherwise be parsed as an
    (invalid) tag rather than shown as text. `\\n` becomes `<br/>` — a bare
    newline is just whitespace to a Paragraph and would otherwise collapse
    the officer's paragraph breaks into one run-on line.
    """
    return xml_escape(text or "").replace("\n", "<br/>\n")


def _status_label(question: ReportQuestion) -> str:
    if question.answer_status == "needs_input" and not question.answer_text:
        return "NOT YET ANSWERED"
    return question.answer_status.replace("_", " ").upper()


def render_pdf(report: Report) -> bytes:
    """Render a Report as PDF bytes.

    Raises PDFRenderError if ReportLab cannot be imported, if no usable
    Unicode font is found, or if building the document otherwise fails.
    Never raises ImportError/AttributeError/etc. directly — every failure
    path funnels through PDFRenderError so api/reports.py has exactly one
    exception type to map to a 503.
    """
    try:
        import io

        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.lib.units import mm
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        from reportlab.platypus import (
            PageBreak,
            Paragraph,
            SimpleDocTemplate,
            Spacer,
            Table,
            TableStyle,
        )
    except ImportError as exc:
        raise PDFRenderError(
            f"ReportLab is not available in this environment: {exc}"
        ) from exc

    # May itself raise PDFRenderError (no font found) — deliberately not
    # caught by the broad `except Exception` below, so that message reaches
    # the caller unwrapped rather than as "Failed to build the PDF
    # document: Failed to build the PDF document: ...".
    font_name, bold_font_name = _register_unicode_font(pdfmetrics, TTFont)

    try:
        styles = getSampleStyleSheet()
        title_style = ParagraphStyle(
            "PrivacyCareTitle",
            parent=styles["Title"],
            fontName=bold_font_name,
            fontSize=18,
            leading=22,
        )
        section_style = ParagraphStyle(
            "PrivacyCareSection",
            parent=styles["Heading2"],
            fontName=bold_font_name,
            fontSize=13,
            leading=16,
            spaceBefore=14,
            spaceAfter=6,
        )
        question_style = ParagraphStyle(
            "PrivacyCareQuestion",
            parent=styles["Normal"],
            fontName=bold_font_name,
            fontSize=10.5,
            leading=13,
            spaceBefore=8,
            spaceAfter=2,
        )
        body_style = ParagraphStyle(
            "PrivacyCareBody",
            parent=styles["Normal"],
            fontName=font_name,
            fontSize=10,
            leading=13,
        )
        meta_style = ParagraphStyle(
            "PrivacyCareMeta",
            parent=body_style,
            fontSize=8.5,
            textColor=colors.grey,
        )
        unanswered_style = ParagraphStyle(
            "PrivacyCareUnanswered",
            parent=body_style,
            textColor=colors.red,
            fontName=bold_font_name,
        )
        citation_style = ParagraphStyle(
            "PrivacyCareCitation",
            parent=body_style,
            fontSize=8,
        )
        summary_style = ParagraphStyle(
            "PrivacyCareSummary",
            parent=body_style,
            fontSize=11,
            spaceBefore=10,
            spaceAfter=10,
        )

        table_style = TableStyle(
            [
                ("FONTNAME", (0, 0), (-1, -1), font_name),
                ("FONTSIZE", (0, 0), (-1, -1), 8.5),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
                ("BACKGROUND", (0, 0), (0, -1), colors.whitesmoke),
            ]
        )
        citation_header_style = TableStyle(
            [
                ("FONTNAME", (0, 0), (-1, -1), font_name),
                ("FONTNAME", (0, 0), (-1, 0), bold_font_name),
                ("FONTSIZE", (0, 0), (-1, -1), 8),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
                ("BACKGROUND", (0, 0), (-1, 0), colors.whitesmoke),
                # Tight padding: a source_key is a dotted path
                # ("privacy_declaration.data_use") with no internal spaces,
                # so ReportLab wraps it at an arbitrary character boundary
                # (not a hyphenated one) the instant it doesn't fit the
                # column — every point of padding reclaimed here is a point
                # less likely to fracture a source_key mid-word.
                ("LEFTPADDING", (0, 0), (-1, -1), 2),
                ("RIGHTPADDING", (0, 0), (-1, -1), 2),
                ("TOPPADDING", (0, 0), (-1, -1), 2),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
            ]
        )

        story: list = [Paragraph(_escaped(report.title), title_style)]

        meta_rows = [
            [Paragraph(_escaped(label), meta_style), Paragraph(_escaped(value), meta_style)]
            for label, value in report.metadata
        ]
        meta_table = Table(meta_rows, colWidths=[40 * mm, 130 * mm])
        meta_table.setStyle(table_style)
        story.append(meta_table)

        # D-PDF-5 / brief: "the PDF must state what is unanswered" — an
        # assessment must never read as finished to a regulator just
        # because nothing on the page said otherwise.
        unanswered_count = report.total_count - report.answered_count
        summary_text = (
            f"Completion: {report.answered_count} of {report.total_count} "
            f"question(s) answered ({report.completeness:.1f}% complete)."
        )
        if unanswered_count > 0:
            summary_text += (
                f" {unanswered_count} question(s) remain UNANSWERED — this "
                "assessment is NOT complete."
            )
        story.append(Paragraph(_escaped(summary_text), summary_style))

        sections = report.sections
        for section_index, section in enumerate(sections):
            if section_index > 0:
                story.append(PageBreak())
            story.append(_section_flowable(section, section_style, Paragraph))
            for question in section.questions:
                story.extend(
                    _question_flowables(
                        question,
                        question_style=question_style,
                        body_style=body_style,
                        meta_style=meta_style,
                        unanswered_style=unanswered_style,
                        citation_style=citation_style,
                        citation_header_style=citation_header_style,
                        Paragraph=Paragraph,
                        Table=Table,
                        Spacer=Spacer,
                        mm=mm,
                    )
                )

        footer_text = f"Export mode: {report.export_mode}"

        def _draw_footer(canvas, doc) -> None:
            canvas.saveState()
            canvas.setFont(font_name, 8)
            canvas.setFillColor(colors.grey)
            canvas.drawString(20 * mm, 10 * mm, footer_text)
            canvas.drawRightString(
                doc.pagesize[0] - 20 * mm, 10 * mm, f"Page {doc.page}"
            )
            canvas.restoreState()

        buffer = io.BytesIO()
        doc = SimpleDocTemplate(
            buffer,
            pagesize=A4,
            leftMargin=20 * mm,
            rightMargin=20 * mm,
            topMargin=20 * mm,
            bottomMargin=20 * mm,
            title=report.title,
        )
        doc.build(story, onFirstPage=_draw_footer, onLaterPages=_draw_footer)
        return buffer.getvalue()
    except PDFRenderError:
        raise
    except Exception as exc:  # noqa: BLE001 - funnel every build failure into PDFRenderError
        raise PDFRenderError(f"Failed to build the PDF document: {exc}") from exc


def _section_flowable(section: ReportSection, section_style, paragraph_cls):
    return paragraph_cls(_escaped(section.title), section_style)


def _question_flowables(
    question: ReportQuestion,
    *,
    question_style,
    body_style,
    meta_style,
    unanswered_style,
    citation_style,
    citation_header_style,
    Paragraph,
    Table,
    Spacer,
    mm,
) -> list:
    flowables: list = [Paragraph(_escaped(question.question_text), question_style)]

    if question.answer_status == "needs_input" and not question.answer_text:
        flowables.append(Paragraph(_status_label(question), unanswered_style))
    else:
        flowables.append(Paragraph(_escaped(question.answer_text), body_style))
        # Each dynamic piece is escaped INDIVIDUALLY, then joined with a
        # literal "&middot;" entity ReportLab's Paragraph parser resolves to
        # a middle dot. Escaping the whole joined string instead would turn
        # that entity back into the literal text "&amp;middot;". `author`
        # is created_by — normally an identity string
        # (_created_by_from_client), but not guaranteed free of XML-special
        # characters, so it is escaped like everything else here rather
        # than trusted.
        attribution = (
            f"Author: {_escaped(question.author or 'Unknown')} &middot; "
            f"Source: {_escaped(question.answer_source)} &middot; "
            f"Status: {_escaped(_status_label(question))}"
        )
        flowables.append(Paragraph(attribution, meta_style))

    if question.citations:
        header_row = [
            Paragraph(label, citation_style)
            for label in ("#", "Source", "Value", "Type")
        ]
        data_rows = [
            [
                Paragraph(
                    str(citation.number) if citation.number is not None else "",
                    citation_style,
                ),
                Paragraph(_escaped(citation.source_key), citation_style),
                Paragraph(_escaped(citation.value), citation_style),
                Paragraph(_escaped(citation.type), citation_style),
            ]
            for citation in question.citations
        ]
        citation_table = Table(
            [header_row, *data_rows],
            colWidths=[8 * mm, 60 * mm, 77 * mm, 20 * mm],
        )
        citation_table.setStyle(citation_header_style)
        flowables.append(citation_table)

    flowables.append(Spacer(1, 4 * mm))
    return flowables
