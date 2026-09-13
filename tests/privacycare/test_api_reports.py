# The PDF export route (task 3, config-and-pdf plan): GET
# .../{assessment_id}/pdf. Inserts are rolled back by this file's own `db`
# fixture, same convention as every other file in this package.
#
# "Three things here each fail SILENTLY if done wrong" (task brief) — this
# file is organised around those three, plus the route's own contract:
#   1. THE LAZY IMPORT — test_reportlab_is_never_imported_at_module_scope
#      (static) and test_a_broken_reportlab_import_surfaces_as_... (dynamic).
#   2. THE FONT — test_missing_font_raises_pdfrendererror_instead_of_...,
#      the three package-data tests (the font must come out of the
#      installed distribution, not off this host — see their own
#      docstrings), and the glyph round-trip test.
#   3. THE FILENAME —
#      test_content_disposition_filename_survives_the_admin_uis_exact_regex.
#
# Every assertion on PDF content below extracts real text back out with
# pypdf and checks it is actually there — never `len(pdf_bytes) > 0`, never
# a mocked call.
import ast
import builtins
import glob
import inspect
import io
import os
import re
import shutil
import subprocess
import sys
import tomllib
from importlib import resources
from pathlib import Path

import pytest
import sqlalchemy
from fastapi import HTTPException
from pypdf import PdfReader
from sqlalchemy.orm import Session

from fides.api.privacycare.api import reports as reports_module
from fides.api.privacycare.api.answers import write_answer
from fides.api.privacycare.api.reports import _sanitize_filename, get_assessment_pdf
from fides.api.privacycare.pdf import PDFRenderError, render_pdf
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


# Directories a host keeps fonts in. The font this document is set in must
# come from NONE of them: the image PrivacyCare ships as
# (python-slim-bookworm plus curl/git/freetds) has no fontconfig and no
# DejaVu face, so a font found here is a font the deployment will not have.
_SYSTEM_FONT_DIRS = (
    "/usr/share/fonts",
    "/usr/local/share/fonts",
    "/usr/share/fonts/truetype",
    os.path.expanduser("~/.fonts"),
    os.path.expanduser("~/.local/share/fonts"),
)


def test_the_font_is_package_data_and_not_a_host_install():
    """The test that would have caught the 503.

    The previous version of this file asserted that DejaVu Sans was
    installed ON THIS HOST — which is true of a developer laptop and false
    of every deployment, so it passed while the shipped product returned
    503 for every export. What has to be true instead is that the font
    resolves out of the INSTALLED PACKAGE: same file, wherever
    ethyca-fides is installed, with nothing on the host consulted.
    """
    import fides.api.privacycare.pdf as pdf_module

    package_dir = Path(pdf_module.__file__).resolve().parent

    for filename in (pdf_module._REGULAR_FONT_FILE, pdf_module._BOLD_FONT_FILE):
        resource = pdf_module._vendored_font(filename)
        assert resource is not None, f"{filename} is not shipped as package data"
        assert resource.is_file()
        assert resource.read_bytes()[:4] in (b"\x00\x01\x00\x00", b"true"), (
            f"{filename} does not look like a real TrueType file"
        )

        resolved = str(resources.files(pdf_module._FONT_PACKAGE).joinpath(filename))
        assert str(package_dir) in resolved, (
            f"{filename} resolved to {resolved}, which is outside the "
            f"privacycare package at {package_dir}"
        )
        for system_dir in _SYSTEM_FONT_DIRS:
            assert not resolved.startswith(system_dir), (
                f"{filename} resolved to a HOST font at {resolved} — the "
                "deployment image has no such file"
            )


def test_font_resolution_consults_no_fc_list_and_no_system_font_directory():
    """Behavioural half of the test above: with every route off this
    process blocked, the font must still register.

    Blocks the ways the old implementation reached the host — a
    subprocess (`fc-list`), a PATH lookup, a filesystem walk/glob — and
    additionally fails the test if ANY system font directory is opened.
    Mutation-checked: restoring the fc-list lookup makes this raise.
    """
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    import fides.api.privacycare.pdf as pdf_module

    real_open = builtins.open

    def _no_host_fonts(file, *args, **kwargs):
        path = str(file)
        for system_dir in _SYSTEM_FONT_DIRS:
            assert not path.startswith(system_dir), (
                f"the renderer opened a HOST font file: {path}"
            )
        return real_open(file, *args, **kwargs)

    def _blocked(*args, **kwargs):
        raise AssertionError(
            f"font resolution shelled out to / searched the host: args={args!r}"
        )

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(builtins, "open", _no_host_fonts)
        mp.setattr(subprocess, "run", _blocked)
        mp.setattr(subprocess, "Popen", _blocked)
        mp.setattr(subprocess, "check_output", _blocked)
        mp.setattr(os, "popen", _blocked)
        mp.setattr(os, "walk", _blocked)
        mp.setattr(shutil, "which", _blocked)
        mp.setattr(glob, "glob", _blocked)
        mp.setattr(glob, "iglob", _blocked)

        regular, bold = pdf_module._register_unicode_font(pdfmetrics, TTFont)

    assert regular == pdf_module._FONT_NAME
    assert bold == pdf_module._FONT_NAME_BOLD


def test_pdf_py_imports_nothing_that_can_reach_the_host_for_a_font():
    """Static guard on the same property, so the seam cannot reopen
    quietly: pdf.py must not import subprocess/shutil/glob at all, and
    must contain no system font path.
    """
    import fides.api.privacycare.pdf as pdf_module

    source = inspect.getsource(pdf_module)
    tree = ast.parse(source)
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.append((node.module or "").split(".")[0])

    for forbidden in ("subprocess", "shutil", "glob"):
        assert forbidden not in imported, (
            f"pdf.py imports {forbidden} — the font must come from package "
            "data, never from a host lookup"
        )

    code = "\n".join(
        line for line in source.splitlines() if not line.lstrip().startswith("#")
    )
    for system_dir in ("/usr/share/fonts", "fc-list"):
        assert system_dir not in code, f"pdf.py's code references {system_dir!r}"


def test_the_font_files_are_declared_as_packaging_artifacts():
    """The font is only useful if it survives the build. Both the wheel
    and the sdist must name it explicitly — relying on "hatchling includes
    everything under packages" is what a future exclusion rule would
    silently break, and the failure mode is a 503 on every export, in
    production only.
    """
    repo_root = Path(__file__).resolve().parents[2]
    config = tomllib.loads((repo_root / "pyproject.toml").read_text())
    targets = config["tool"]["hatch"]["build"]["targets"]

    for target in ("wheel", "sdist"):
        artifacts = targets[target]["artifacts"]
        assert any(
            "privacycare/fonts" in entry and entry.endswith(".ttf")
            for entry in artifacts
        ), f"the {target} build does not declare the PrivacyCare font files"


def test_missing_font_raises_pdfrendererror_instead_of_a_silent_fallback(
    monkeypatch,
):
    """The bar the brief sets: "fail loudly ... do not fall back to a
    built-in font, because that failure is invisible." A distribution
    built without its font data must raise PDFRenderError, never quietly
    proceed with a ReportLab built-in font.
    """
    import fides.api.privacycare.pdf as pdf_module

    monkeypatch.setattr(pdf_module, "_vendored_font", lambda filename: None)

    with pytest.raises(PDFRenderError, match="missing from the installed package"):
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


def test_the_printed_completion_sentence_cannot_contradict_itself(db):
    """One sentence of a DPA 2019 s31 filing used to be able to say both
    "(100.0% complete)" and "6 question(s) remain UNANSWERED", because the
    counts were live and the percentage came from a stored column nothing
    refreshes when a template gains questions.

    This makes the column lie (100 over a live 1-of-3) and reads the real
    sentence back out of the rendered PDF: the percentage must be the two
    counts printed next to it, and the UNANSWERED clause must agree with
    both. Mutation check: restoring `completeness=detail.completeness` in
    build_report makes this fail.
    """
    tid = _seed_template(db)
    aid = _seed_assessment(db, tid, "Self Consistent DPIA")
    q1 = _seed_question(db, tid, "q1", "necessity", 1)
    _seed_question(db, tid, "q2", "necessity", 1)
    _seed_question(db, tid, "q3", "security", 2)
    write_answer(db, aid, q1, "Only this one is answered.", "carol@example.com")
    db.execute(
        sqlalchemy.text(
            "UPDATE privacy_assessment SET completeness = 100 WHERE id = :aid"
        ),
        {"aid": aid},
    )
    db.flush()

    text = _extract_text(render_pdf(build_report(db, aid)))

    assert "1 of 3 question(s) answered (33.3% complete)" in text, text
    assert "2 question(s) remain UNANSWERED" in text
    assert "100.0% complete" not in text


def test_an_empty_answer_prints_as_unanswered_not_as_an_authored_one(db):
    """A blank answer filed as COMPLETE used to render as

        Q1?
        Author: carol@example.com - Source: user_input - Status: COMPLETE

    — a question the regulator reads as answered and attributed, with no
    answer under it. It must print as unanswered, with no attribution, and
    the completion sentence must agree.
    """
    tid = _seed_template(db)
    aid = _seed_assessment(db, tid, "Blank Answer DPIA")
    qid = _seed_question(db, tid, "q1", "necessity", 1)
    write_answer(db, aid, qid, "", "carol@example.com")
    db.flush()

    text = _extract_text(render_pdf(build_report(db, aid)))

    assert "NOT YET ANSWERED" in text
    assert "carol@example.com" not in text, (
        "an empty answer printed an author, which reads as a finished answer"
    )
    assert "Status: COMPLETE" not in text
    assert "0 of 1 question(s) answered (0.0% complete)" in text
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


def test_pdf_render_error_maps_to_a_503_whose_detail_names_the_cause(db, monkeypatch):
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
    # The assessment id is appended deliberately: a title in a non-Latin
    # script sanitises to the generic fallback, and without the id a DPO
    # exporting several such assessments would get several identically named
    # files, each overwriting the last. See _report_filename.
    assert filename.startswith(
        "Data Protection Impact Assessment Fuel Card CRM review-"
    )
    assert filename.endswith(".pdf")


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


def test_a_long_assessment_name_cannot_produce_an_unsaveable_filename(db):
    """An assessment name is officer-supplied free text with no length
    limit. A 300-character name produced a 354-character filename — past
    the 255-BYTE cap on ext4, NTFS and APFS, where the browser truncates
    or fails the save outright. The UI's regex extracts it happily, so
    nothing upstream catches this.
    """
    from fides.api.privacycare.api.reports import _report_filename

    class _R:
        title = "Data Protection Impact Assessment: " + ("Fuel Card Review " * 20)

    filename = f"{_report_filename(_R(), 'pa_e5704ed3d717')}.pdf"

    assert len(filename.encode("utf-8")) <= 255, len(filename)
    # The id survives whole, at the end, where it keeps exports apart.
    assert filename.endswith("-pa_e5704ed3d717.pdf")
    assert _ADMIN_UI_FILENAME_REGEX.search(f'attachment; filename="{filename}"')


def test_two_long_names_that_differ_only_past_the_cut_still_differ(db):
    """Truncation must not become the new collision: the id is what makes
    two exports distinguishable, and it is appended after the cut.
    """
    from fides.api.privacycare.api.reports import _report_filename

    class _First:
        title = "A" * 300 + " first"

    class _Second:
        title = "A" * 300 + " second"

    first = _report_filename(_First(), "pa_aaaaaaaaaaaa")
    second = _report_filename(_Second(), "pa_bbbbbbbbbbbb")

    assert first != second
    assert len(f"{first}.pdf".encode("utf-8")) <= 255
    assert first.endswith("-pa_aaaaaaaaaaaa")


def test_the_route_itself_caps_the_filename_it_sends(db):
    # Through the real route and the real header, not just the helper.
    tid = _seed_template(db)
    aid = _seed_assessment(db, tid, "Fuel Card Review " * 20)
    db.flush()

    response = get_assessment_pdf(aid, db=db)
    disposition = response.headers["content-disposition"]
    filename = _ADMIN_UI_FILENAME_REGEX.search(disposition).group(1)

    assert len(filename.encode("utf-8")) <= 255, disposition
    assert filename.endswith(f"-{aid}.pdf")


def test_a_non_latin_title_still_yields_a_distinguishable_filename(db, monkeypatch):
    # The sanitiser keeps only Latin letters and a few marks, so a title in
    # another script collapses to the generic fallback. This product ships
    # into Kenya: a DPO exporting several assessments named in Swahili or
    # Arabic would get several files all called assessment-report.pdf, each
    # silently overwriting the last in their downloads folder.
    from fides.api.privacycare.api.reports import _report_filename

    class _R:
        title = "تقييم الأثر"

    first = _report_filename(_R(), "pa_aaaaaaaaaaaa")
    second = _report_filename(_R(), "pa_bbbbbbbbbbbb")

    assert first != second, (
        f"two differently-identified assessments produced the same filename: {first}"
    )
    assert "pa_aaaaaaaaaaaa" in first
    assert _ADMIN_UI_FILENAME_REGEX.search(f'attachment; filename="{first}.pdf"'), (
        "the disambiguated filename no longer survives the UI's own regex"
    )
