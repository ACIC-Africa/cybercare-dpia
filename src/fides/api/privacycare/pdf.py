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
#    TrueType font with real Unicode coverage (DejaVu Sans) is registered
#    and used for ALL body text; if it cannot be loaded, this module fails
#    loudly (PDFRenderError) rather than falling back to a built-in font,
#    because that fallback is exactly the invisible failure being guarded
#    against.
#
#    THE FONT IS VENDORED, not located on the host. An earlier revision
#    shelled out to `fc-list` and searched the host's installed fonts. That
#    made the document depend on a system package and a system tool — the
#    exact dependency class D-PDF-1 rejected WeasyPrint to avoid — and the
#    image this product ships as (python-slim-bookworm plus curl, git and
#    freetds; Dockerfile) carries neither fontconfig nor any DejaVu face.
#    Every export in every real deployment therefore 503'd, while the tests
#    passed because the developer host happened to have DejaVu installed: a
#    test that passes because of the HOST is worth nothing. The .ttf now
#    ships inside this package (fonts/, with its licence) and is read
#    through importlib.resources relative to this module — no `fc-list`, no
#    filesystem search, nothing outside the installed distribution.
#
# 3. THE FILENAME. api/reports.py sanitises the officer-supplied assessment
#    name before it reaches Content-Disposition; this module has no part in
#    that (see _sanitize_filename there), but is documented here because
#    the two files exist for the same reason: an officer's free text is
#    filed verbatim in the BODY, so it must be trusted in the body and
#    distrusted in the HEADER.
from __future__ import annotations

import io
from importlib import resources
from typing import TYPE_CHECKING
from xml.sax.saxutils import escape as xml_escape

from fides.api.privacycare.report import (
    ODPC_METADATA_LABEL,
    Report,
    ReportQuestion,
    ReportSection,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from importlib.resources.abc import Traversable

    from fides.api.privacycare.risk.odpc import OdpcFinding


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


# The font files vendored into this package (fonts/, alongside their
# licence), and the package path they are read from. A PACKAGE path, not a
# filesystem path: importlib.resources resolves it relative to this
# installed module, so it is found wherever the distribution is installed
# and cannot be satisfied — or defeated — by anything on the host.
_FONT_PACKAGE = "fides.api.privacycare.fonts"
_REGULAR_FONT_FILE = "DejaVuSans.ttf"
_BOLD_FONT_FILE = "DejaVuSans-Bold.ttf"


def _vendored_font(filename: str) -> "Traversable | None":
    """The vendored font file `filename` as an importlib.resources
    Traversable, or None if this distribution does not carry it.

    None means the package was built without its font data (see
    pyproject.toml's wheel/sdist `artifacts` entries, which exist to stop
    exactly that) — a packaging failure, not a host configuration the
    deployer can fix. Never raises; _register_unicode_font is what decides
    that a missing regular face is fatal.
    """
    try:
        resource = resources.files(_FONT_PACKAGE).joinpath(filename)
        return resource if resource.is_file() else None
    except (ModuleNotFoundError, OSError):
        return None


def _register_unicode_font(pdfmetrics, ttfont_cls) -> tuple[str, str]:
    """Register a DejaVu Sans TrueType font (regular + bold) under
    _FONT_NAME / _FONT_NAME_BOLD, and register the pair as a ReportLab font
    FAMILY so that `<b>`/`<strong>` markup inside a Paragraph resolves to
    the real bold face rather than a synthetically slanted regular one.

    Reads the font out of this package's own data (see _vendored_font);
    it consults nothing on the host. Raises PDFRenderError — never falls
    back to a ReportLab built-in font — if the regular face cannot be
    loaded. See the module docstring, point 2, for why a silent fallback
    here is exactly the failure mode this function exists to prevent.

    The .ttf bytes are handed to TTFont as a file object rather than a
    path: a Traversable is not guaranteed to have a filesystem path at all
    (a zip-imported distribution has none), and reading it is the one
    access that works for every installation shape.
    """
    regular = _vendored_font(_REGULAR_FONT_FILE)
    if regular is None:
        raise PDFRenderError(
            f"The Unicode font this document is set in ({_REGULAR_FONT_FILE}) "
            f"is missing from the installed package ({_FONT_PACKAGE}). "
            "Refusing to fall back to a ReportLab built-in Type 1 font: "
            "those silently render subscript/superscript, non-Latin and "
            "currency glyphs as solid black boxes instead of raising, which "
            "is unacceptable for a document filed with a regulator. The font "
            "ships as package data, so this is a broken/incomplete install "
            "of ethyca-fides rather than anything missing on this host — "
            "reinstall the package."
        )
    bold = _vendored_font(_BOLD_FONT_FILE)

    pdfmetrics.registerFont(ttfont_cls(_FONT_NAME, io.BytesIO(regular.read_bytes())))
    if bold is not None:
        pdfmetrics.registerFont(
            ttfont_cls(_FONT_NAME_BOLD, io.BytesIO(bold.read_bytes()))
        )
        bold_name = _FONT_NAME_BOLD
    else:
        # Bold face missing but regular found: still real Unicode coverage,
        # just no bold weight. Register the family pointing bold at the
        # regular face rather than failing — headings render un-bold
        # instead of as black boxes, which is a cosmetic loss, not a
        # correctness one. (Both faces are vendored, so this branch is
        # reachable only from a partial install.)
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


def _has_text(question: ReportQuestion) -> bool:
    """Is there anything in this answer for a regulator to read?

    The renderer's own predicate, and deliberately looser than report.py's
    _is_answered (which also requires `complete`): an answer that is only
    `partial` still has words in it and is printed, with its status shown
    as PARTIAL. What is NOT printed as an answer is an EMPTY one — and
    "empty" is not the same as "needs_input", because
    UpdateAnswerRequest.answer_text has no minimum length and write_answer
    defaults answer_status to "complete", so a blank string can arrive
    filed as COMPLETE. Keyed on the text, not the status, so it cannot be
    fooled by one.
    """
    return bool((question.answer_text or "").strip())


def _status_label(question: ReportQuestion) -> str:
    if not _has_text(question):
        return "NOT YET ANSWERED"
    return question.answer_status.replace("_", " ").upper()


def _odpc_callout_style(finding: "OdpcFinding", unanswered_style, summary_style):
    """Which of THIS FILE'S EXISTING paragraph styles the ODPC finding is
    rendered in for its own, dedicated paragraph — never a new one.

    Fix round 1, Important finding 1: the metadata table applies one
    uniform 8.5pt grey style (table_style, via meta_style) to every row —
    "ODPC Prior Consultation" printed exactly like "Created: 2026-09-16"
    two rows below it, on a document where that row can mean a regulator
    filing is legally required before processing may begin. The brief's
    "where it cannot be missed" is not satisfied by being in the right
    block of the document; it has to be visually distinct too.

    REQUIRED reuses unanswered_style — the same red + bold weight
    _question_flowables already uses for "NOT YET ANSWERED" elsewhere in
    this file — because this is the one line that must be SEEN, not read
    carefully. NOT REQUIRED still gets promoted to its own paragraph (the
    negative case stays visible; silence reads as "not assessed" — see
    render_pdf) but at summary_style's plain weight, the same weight the
    completion sentence already has: informational, not urgent. No third
    style is introduced for this — matching the house convention rather
    than inventing one is the point of the fix.
    """
    return unanswered_style if finding.required else summary_style


def render_pdf(report: Report) -> bytes:
    """Render a Report as PDF bytes.

    Raises PDFRenderError if ReportLab cannot be imported, if no usable
    Unicode font is found, or if building the document otherwise fails.
    Never raises ImportError/AttributeError/etc. directly — every failure
    path funnels through PDFRenderError so api/reports.py has exactly one
    exception type to map to a 503.
    """
    try:
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
            [
                Paragraph(_escaped(label), meta_style),
                Paragraph(_escaped(value), meta_style),
            ]
            for label, value in report.metadata
        ]
        meta_table = Table(meta_rows, colWidths=[40 * mm, 130 * mm])
        meta_table.setStyle(table_style)
        story.append(meta_table)

        # Task 4 fix round 1, Important finding 1: the ODPC verdict is
        # ALSO a row inside meta_table above (report.py's _metadata_rows
        # builds it), but that row is structural, not prominent — every
        # row shares the same tiny grey style. Promoted here into its own
        # paragraph, alongside the completion summary below, in whichever
        # of the two EXISTING styles _odpc_callout_style picks (never a
        # new one). Pulled from report.metadata rather than recomputed
        # from report.odpc, so there is exactly one place this sentence is
        # composed (report.py's _odpc_metadata_value) even though it now
        # prints twice.
        #
        # Final whole-branch review, minor finding: looked up by
        # ODPC_METADATA_LABEL (imported from report.py), not a second
        # "ODPC Prior Consultation" string literal here — the two used to
        # be able to drift independently, silently dropping the callout
        # the moment they did. Do NOT change this to report.odpc.reason:
        # reason lacks the "REQUIRED —" verdict prefix and the driving-risk
        # sentence _odpc_metadata_value appends, so reading it directly
        # would weaken the callout, not just relocate it.
        odpc_callout_text = dict(report.metadata).get(ODPC_METADATA_LABEL)
        if odpc_callout_text:
            story.append(
                Paragraph(
                    _escaped(odpc_callout_text),
                    _odpc_callout_style(report.odpc, unanswered_style, summary_style),
                )
            )

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

    if not _has_text(question):
        # No attribution line in this branch, deliberately: an empty answer
        # must not print as "Author: ... - Source: ... - Status: COMPLETE"
        # under a question with nothing beneath it. report.py's
        # _is_answered excludes the same answers from the completion
        # figure, so the page and the sentence at the top of it agree.
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
