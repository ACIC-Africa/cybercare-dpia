# The PDF export route (task 3, config-and-pdf plan): GET
# .../{assessment_id}/pdf. Inserts are rolled back by this file's own `db`
# fixture, same convention as every other file in this package.
#
# "Three things here each fail SILENTLY if done wrong" (task brief) — this
# file is organised around those three, plus the route's own contract:
#   1. THE LAZY IMPORT — test_reportlab_is_never_imported_at_module_scope
#      (static) and test_a_broken_reportlab_import_surfaces_as_... (dynamic).
#   2. THE FONT — test_missing_font_raises_pdfrendererror_instead_of_...,
#      test_locate_font_file_finds_dejavu_sans_on_this_host, and the glyph
#      round-trip test.
#   3. THE FILENAME —
#      test_content_disposition_filename_survives_the_admin_uis_exact_regex.
#
# Every assertion on PDF content below extracts real text back out with
# pypdf and checks it is actually there — never `len(pdf_bytes) > 0`, never
# a mocked call.
import ast
import inspect
import io
import re
import sys

import pytest
import sqlalchemy
from fastapi import HTTPException
from pypdf import PdfReader
from sqlalchemy.orm import Session

from fides.api.privacycare.api import reports as reports_module
from fides.api.privacycare.api.answers import write_answer
from fides.api.privacycare.api.reports import _sanitize_filename, get_assessment_pdf
from fides.api.privacycare.pdf import PDFRenderError, render_pdf
from fides.api.privacycare.pdf import _locate_font_file as locate_font_file
from fides.api.privacycare.report import Report, build_report
from tests.privacycare.test_api_assessments import (
    _seed_assessment,
    _seed_question,
    _seed_template,
)

DB_URL = "postgresql://postgres:fides@127.0.0.1:5442/fides"


@pytest.fixture
def db():
    engine = sqlalchemy.create_engine(DB_URL)
    with Session(engine) as session:
        yield session
        session.rollback()


def _extract_text(pdf_bytes: bytes) -> str:
    """Real text extracted from the rendered PDF, whitespace-normalised.

    ReportLab wraps a Paragraph at whatever width its column/margin gives
    it, and pypdf's extraction inserts a newline at each wrap point — a
    layout detail (which line something happens to land on) that has
    nothing to do with whether the CONTENT survived. Collapsing all
    whitespace runs to a single space makes every assertion in this file
    test content, not incidental line-wrapping.
    """
    reader = PdfReader(io.BytesIO(pdf_bytes))
    raw = "\n".join(page.extract_text() or "" for page in reader.pages)
    return re.sub(r"\s+", " ", raw)


def _empty_report(**overrides) -> Report:
    defaults = dict(
        title="Dummy DPIA",
        metadata=[],
        sections=[],
        answered_count=0,
        total_count=0,
        completeness=0.0,
        export_mode="external",
    )
    defaults.update(overrides)
    return Report(**defaults)


# ── 1. The lazy import ──────────────────────────────────────────────────


def test_reportlab_is_never_imported_at_module_scope():
    """A module-scope `import reportlab` takes the ENTIRE API down at boot
    if ReportLab is broken/missing — it must live inside render_pdf. This
    parses pdf.py's own source and inspects only its TOP-LEVEL statements
    (module body, not nested inside any function), so it fails the instant
    someone hoists the import back up — the exact regression the brief
    warns is otherwise invisible.
    """
    import fides.api.privacycare.pdf as pdf_module

    tree = ast.parse(inspect.getsource(pdf_module))
    for node in tree.body:
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        else:
            continue
        offenders = [n for n in names if n.startswith("reportlab")]
        assert not offenders, (
            f"reportlab imported at module scope in pdf.py: {offenders} — "
            "this must be an import inside render_pdf, not at the top of "
            "the file"
        )


def test_a_broken_reportlab_import_surfaces_as_pdfrendererror(monkeypatch):
    """Simulates "ReportLab is not installed" without uninstalling it:
    setting a sys.modules entry to None forces the next `import <name>` to
    raise ImportError (documented CPython import-system behaviour). If
    render_pdf's `import reportlab...` block, or its except ImportError
    clause, were ever removed, this raises a bare ImportError instead of
    PDFRenderError and the test fails.
    """
    monkeypatch.setitem(sys.modules, "reportlab", None)

    with pytest.raises(PDFRenderError, match="not available"):
        render_pdf(_empty_report())


# ── 2. The font ──────────────────────────────────────────────────────────


def test_locate_font_file_finds_dejavu_sans_on_this_host():
    path = locate_font_file("DejaVuSans.ttf")
    assert path is not None, "DejaVu Sans should be installed on this host"
    assert path.endswith("DejaVuSans.ttf")
    import os

    assert os.path.isfile(path)


def test_missing_font_raises_pdfrendererror_instead_of_a_silent_fallback(
    monkeypatch,
):
    """The bar the brief sets: "fail loudly ... do not fall back to a
    built-in font, because that failure is invisible." Forcing
    _locate_font_file to find nothing must raise PDFRenderError, never
    quietly proceed with a ReportLab built-in font.
    """
    import fides.api.privacycare.pdf as pdf_module

    monkeypatch.setattr(pdf_module, "_locate_font_file", lambda suffix: None)

    with pytest.raises(PDFRenderError, match="Unicode"):
        pdf_module.render_pdf(_empty_report())


def test_answer_text_glyphs_survive_superscript_subscript_currency_and_diacritic(
    db,
):
    """D-PDF-3, verbatim from the brief: ReportLab's built-in fonts render
    a glyph they don't have as a SOLID BLACK BOX rather than raising —
    invisible unless the exact characters are checked for on the other
    side of a real extraction. This writes an officer-typed answer
    containing a subscript numeral, a superscript numeral, a currency
    symbol and an accented name, then proves every one of them round-trips
    through the rendered PDF as real, extractable text.
    """
    tid = _seed_template(db)
    aid = _seed_assessment(db, tid, "Glyph Roundtrip DPIA")
    qid = _seed_question(db, tid, "q1", "necessity", 1)
    awkward = (
        "Emissions: CO₂ output across 10 m² of floor space, "
        "cost €500, filed by José Muñoz."
    )
    write_answer(db, aid, qid, awkward, "jose@example.com")
    db.flush()

    report = build_report(db, aid)
    text = _extract_text(render_pdf(report))

    assert "CO₂" in text, "subscript numeral did not round-trip"
    assert "m²" in text, "superscript numeral did not round-trip"
    assert "€500" in text, "currency symbol did not round-trip"
    assert "José Muñoz" in text, "diacritics did not round-trip"


# ── Content: the report says what it says ────────────────────────────────


def test_pdf_bytes_contain_name_answer_author_citation_and_unanswered_status(
    db,
):
    tid = _seed_template(db)
    aid = _seed_assessment(db, tid, "Fuel Card CRM Assessment")
    q1 = _seed_question(db, tid, "q1", "necessity", 1)
    _seed_question(db, tid, "q2", "necessity", 1)  # left unanswered on purpose

    write_answer(
        db,
        aid,
        q1,
        "The processing is limited to fuel card transactions only.",
        "carol@example.com",
        evidence={
            "id": "ev_1",
            "type": "ai_analysis",
            "value": "Confirmed via the privacy declaration.",
            "source_key": "privacy_declaration.data_use",
            "citation_number": 1,
        },
    )
    db.flush()

    report = build_report(db, aid)
    text = _extract_text(render_pdf(report))

    assert "Fuel Card CRM Assessment" in text
    assert "The processing is limited to fuel card transactions only." in text
    assert "carol@example.com" in text
    assert "privacy_declaration.data_use" in text
    # D-PDF-5 / brief: the PDF must say what is unanswered, not merely omit it.
    assert "NOT YET ANSWERED" in text
    assert "1 of 2 question(s) answered" in text
    assert "1 question(s) remain UNANSWERED" in text


def test_export_mode_is_printed_in_the_footer(db):
    tid = _seed_template(db)
    aid = _seed_assessment(db, tid, "Footer Mode DPIA")
    db.flush()

    for mode in ("internal", "external"):
        report = build_report(db, aid, export_mode=mode)
        text = _extract_text(render_pdf(report))
        assert f"Export mode: {mode}" in text


def test_the_officers_raw_markup_characters_do_not_break_the_document(db):
    """report.py deliberately leaves answer_text unescaped (its own module
    docstring); pdf.py's Paragraph markup IS parsed as lightweight XML, so
    an unescaped "&"/"<...>" would otherwise be parsed as an invalid tag.
    Reuses the exact awkward string test_report.py's
    test_the_officers_words_pass_through_untouched already proves reaches
    build_report unmodified — this proves it still renders (no exception)
    and the plain-text content survives into the PDF once escaped for
    ReportLab's parser.
    """
    tid = _seed_template(db)
    aid = _seed_assessment(db, tid, "Verbatim Text DPIA")
    qid = _seed_question(db, tid, "q1", "necessity", 1)
    awkward = (
        'Shared with R&D <partners> — see "Annexe 1".\n'
        "Retention: 7 years; reviewed annually."
    )
    write_answer(db, aid, qid, awkward, "carol@example.com")
    db.flush()

    report = build_report(db, aid)
    text = _extract_text(render_pdf(report))  # must not raise

    assert "Shared with R&D" in text
    assert "partners" in text
    assert "Retention: 7 years" in text


# ── The route: 404 / 503 / filename ──────────────────────────────────────


def test_unknown_assessment_maps_to_404(db):
    with pytest.raises(HTTPException) as exc_info:
        get_assessment_pdf("no-such-assessment", db=db)
    assert exc_info.value.status_code == 404
    assert "no-such-assessment" in exc_info.value.detail


def test_pdf_render_error_maps_to_a_503_whose_detail_names_the_cause(
    db, monkeypatch
):
    tid = _seed_template(db)
    aid = _seed_assessment(db, tid, "Broken Renderer DPIA")
    db.flush()

    def _boom(report):
        raise PDFRenderError("no usable font found")

    monkeypatch.setattr(reports_module, "render_pdf", _boom)

    with pytest.raises(HTTPException) as exc_info:
        reports_module.get_assessment_pdf(aid, db=db)
    assert exc_info.value.status_code == 503
    # The admin UI's downloadAssessmentReport reads exactly
    # `errorJson.detail` to show the officer why the download failed — the
    # cause must be IN the detail, not just "a 503 happened".
    assert exc_info.value.detail == "no usable font found"


# The admin UI's downloadAssessmentReport (privacy-assessments.slice.ts)
# extracts the filename with EXACTLY this regex. Copied verbatim rather than
# approximated, per the brief: "test the resulting filename against that
# exact regex, not against a guess at what it accepts."
_ADMIN_UI_FILENAME_REGEX = re.compile(r'filename="?([^";\n]+)"?')


def test_content_disposition_filename_survives_the_admin_uis_exact_regex(db):
    tid = _seed_template(db)
    # An assessment name is officer-supplied free text. This one carries a
    # quote, a semicolon and a newline — exactly the three characters the
    # admin UI's regex excludes from its captured group.
    awkward_name = 'Fuel "Card"; CRM\nreview'
    aid = _seed_assessment(db, tid, awkward_name)
    db.flush()

    response = get_assessment_pdf(aid, db=db)
    disposition = response.headers["content-disposition"]

    match = _ADMIN_UI_FILENAME_REGEX.search(disposition)
    assert match is not None, f"admin-UI regex found no filename in {disposition!r}"
    filename = match.group(1)

    assert filename.endswith(".pdf")
    assert '"' not in filename
    assert ";" not in filename
    assert "\n" not in filename
    # The sanitiser's concrete output for this exact awkward name — pinned
    # so a future change to the sanitiser is a deliberate decision, not an
    # accident nobody notices.
    assert filename == "Data Protection Impact Assessment Fuel Card CRM review.pdf"


def test_sanitize_filename_never_reintroduces_the_excluded_characters():
    # Direct, name-shaped fuzz of the same property the route-level test
    # above pins one concrete example of: no input can make it back out
    # with a quote, semicolon or newline still in it.
    for awkward in (
        'Quote "inside" a name',
        "Semicolon; inside; a name",
        "Newline\ninside\na name",
        '""";;;\n\n\n',
        "",
    ):
        result = _sanitize_filename(awkward)
        assert '"' not in result
        assert ";" not in result
        assert "\n" not in result
        assert result != ""
