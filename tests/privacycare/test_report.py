# The DPIA report as a document model. Inserts are rolled back (see the
# `db` fixture below, same convention as test_api_assessments.py and every
# other file in this package).
import pytest
import sqlalchemy
from sqlalchemy.orm import Session

from fides.api.privacycare.api.answers import write_answer
from fides.api.privacycare.api.assessments import _assessment_detail
from fides.api.privacycare.report import Report, ReportCitation, build_report
from fides.api.privacycare.risk.banding import CRITICAL, LOW
from fides.api.privacycare.risk.odpc import CONSULTATION_WINDOW_DAYS, OdpcFinding
from fides.api.privacycare.risk.register import add_risk
from tests.privacycare.test_api_assessments import (
    _seed_answer_with_evidence,
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


def test_unknown_assessment_raises_lookup_error(db):
    with pytest.raises(LookupError):
        build_report(db, "no-such-assessment")


def test_metadata_carries_name_system_data_use_and_template(db):
    tid = _seed_template(db)
    aid = _seed_assessment(
        db,
        tid,
        "Payroll Processing DPIA",
        data_use="essential.service",
        data_use_name="Essential Service",
        system="sys_payroll",
    )
    db.flush()
    report = build_report(db, aid)
    metadata = dict(report.metadata)
    assert metadata["Assessment Name"] == "Payroll Processing DPIA"
    assert metadata["System"] == "sys_payroll"
    assert metadata["Data Use"] == "Essential Service"
    # No template name was set on this row (assessment_template.name is
    # fixed to 'Kenya DPA 2019 DPIA' by _seed_template) — assert the real
    # value, not a placeholder, so this test would fail if the join broke.
    assert metadata["Template"] == "Kenya DPA 2019 DPIA"
    assert "Payroll Processing DPIA" in report.title


def test_every_question_appears_including_unanswered(db):
    # 2 of 3 questions get no answer at all (D-PDF-5): omitting them would
    # overstate the assessment's progress to a regulator.
    tid = _seed_template(db)
    aid = _seed_assessment(db, tid, "Mixed Progress DPIA")
    q1 = _seed_question(db, tid, "q1", "necessity", 1)
    _seed_question(db, tid, "q2", "necessity", 1)
    _seed_question(db, tid, "q3", "security", 2)
    _seed_answer_with_evidence(db, aid, q1, {"id": "ev_1", "type": "ai_analysis"})
    db.flush()

    report = build_report(db, aid)
    all_questions = [q for section in report.sections for q in section.questions]
    assert len(all_questions) == 3

    unanswered = [q for q in all_questions if q.answer_status == "needs_input"]
    assert len(unanswered) == 2
    for q in unanswered:
        assert q.answer_text == ""
        assert q.author is None


def test_answered_question_carries_author_and_source(db):
    tid = _seed_template(db)
    aid = _seed_assessment(db, tid, "Authored DPIA")
    qid = _seed_question(db, tid, "q1", "necessity", 1)
    _seed_answer_with_evidence(
        db,
        aid,
        qid,
        {"id": "ev_1", "type": "ai_analysis"},
        answer_source="ai_analysis",
        created_by="scribe@cybota.com",
    )
    db.flush()

    report = build_report(db, aid)
    question = report.sections[0].questions[0]
    assert question.answer_source == "ai_analysis"
    assert question.author == "scribe@cybota.com"
    assert question.answer_status == "complete"


def test_citations_carry_source_key_and_number(db):
    tid = _seed_template(db)
    aid = _seed_assessment(db, tid, "Cited DPIA")
    qid = _seed_question(db, tid, "q1", "necessity", 1)
    _seed_answer_with_evidence(
        db,
        aid,
        qid,
        {
            "id": "ev_1",
            "type": "ai_analysis",
            "value": "System X processes payroll data.",
            "created_at": "2026-09-01T00:00:00+00:00",
            "source_key": "privacy_declaration.data_use",
            "citation_number": 1,
        },
        answer_source="ai_analysis",
        created_by="scribe@cybota.com",
    )
    db.flush()

    report = build_report(db, aid)
    question = report.sections[0].questions[0]
    assert question.citations == [
        ReportCitation(
            number=1,
            source_key="privacy_declaration.data_use",
            value="System X processes payroll data.",
            type="ai_analysis",
        )
    ]


def test_counts_match_the_detail_screen_exactly(db):
    # Rule 2: assert this equality directly, not "looks about right". A PDF
    # that disagrees with the screen is worse than no PDF.
    #
    # The COUNTS are asserted equal to the screen's. The percentage is not,
    # and deliberately: see
    # test_the_percentage_is_derived_from_the_counts_not_the_stored_column
    # below — the screen's own percentage comes from a stored column that
    # can disagree with its own live counts.
    tid = _seed_template(db)
    aid = _seed_assessment(db, tid, "Reconciled DPIA")
    q1 = _seed_question(db, tid, "q1", "necessity", 1)
    _seed_question(db, tid, "q2", "necessity", 1)
    q3 = _seed_question(db, tid, "q3", "security", 2)
    _seed_answer_with_evidence(
        db, aid, q1, {"id": "ev_1", "type": "ai_analysis"}, answer_status="complete"
    )
    _seed_answer_with_evidence(
        db, aid, q3, {"id": "ev_2", "type": "ai_analysis"}, answer_status="partial"
    )
    db.flush()

    detail = _assessment_detail(db, aid)
    report = build_report(db, aid)

    assert report.answered_count == sum(
        g.answered_count for g in detail.question_groups
    )
    assert report.total_count == sum(g.total_count for g in detail.question_groups)
    # Concretely, not just "equal to itself": one of three questions is
    # complete.
    assert report.answered_count == 1
    assert report.total_count == 3
    assert report.completeness == pytest.approx(100 / 3)


def test_the_percentage_is_derived_from_the_counts_not_the_stored_column(db):
    """The contradiction, reproduced and closed.

    privacy_assessment.completeness is written at ANSWER time; the counts
    are read live. Nothing refreshes the column when a template gains
    questions, and Ethyca's migrations add questions to an existing
    template in place — so the column can say 100 while 1 of 3 questions
    is answered, and the document printed both numbers in one sentence.

    This forces the exact divergence (a stored 100.0 over a live 1-of-3)
    rather than waiting for a migration to produce it, and pins that the
    report ignores the column. Mutation check: restoring
    `completeness=detail.completeness` makes this fail.
    """
    tid = _seed_template(db)
    aid = _seed_assessment(db, tid, "Contradiction DPIA")
    q1 = _seed_question(db, tid, "q1", "necessity", 1)
    _seed_question(db, tid, "q2", "necessity", 1)
    _seed_question(db, tid, "q3", "security", 2)
    _seed_answer_with_evidence(
        db, aid, q1, {"id": "ev_1", "type": "ai_analysis"}, answer_status="complete"
    )
    db.execute(
        sqlalchemy.text(
            "UPDATE privacy_assessment SET completeness = 100 WHERE id = :aid"
        ),
        {"aid": aid},
    )
    db.flush()

    detail = _assessment_detail(db, aid)
    assert detail.completeness == 100, "the stored column was not made to lie"

    report = build_report(db, aid)
    assert report.answered_count == 1
    assert report.total_count == 3
    assert report.completeness == pytest.approx(100 / 3)


@pytest.mark.parametrize(
    "statuses",
    [
        (),
        ("complete",),
        ("complete", "partial"),
        ("complete", "complete", "partial"),
    ],
)
def test_the_percentage_can_never_disagree_with_the_counts(db, statuses):
    """The property, not one example: whatever the shape, the number the
    document prints as a percentage IS the two numbers it prints beside it.
    """
    tid = _seed_template(db)
    aid = _seed_assessment(db, tid, "Invariant DPIA")
    for index, status in enumerate(statuses):
        qid = _seed_question(db, tid, f"q{index}", "necessity", 1)
        _seed_answer_with_evidence(
            db,
            aid,
            qid,
            {"id": f"ev_{index}", "type": "ai_analysis"},
            answer_status=status,
        )
    db.flush()

    report = build_report(db, aid)
    expected = (
        (report.answered_count / report.total_count) * 100
        if report.total_count
        else 0.0
    )
    assert report.completeness == pytest.approx(expected)
    assert 0.0 <= report.completeness <= 100.0


@pytest.mark.parametrize("blank", ["", "   \n\t "])
def test_an_answer_with_no_text_is_not_counted_as_answered(db, blank):
    """UpdateAnswerRequest.answer_text has no minimum length and
    write_answer defaults answer_status to "complete", so `PUT
    .../questions/{id}` with `{"answer_text": ""}` files a COMPLETE answer
    with nothing in it. It used to count toward the filed document's
    completion figure. An answer with no text is not an answer.

    The screen still counts it (this module does not change what
    _assessment_detail reports); the filed document does not — and it can
    only ever count DOWN, never up.
    """
    tid = _seed_template(db)
    aid = _seed_assessment(db, tid, "Blank Answer DPIA")
    qid = _seed_question(db, tid, "q1", "necessity", 1)
    write_answer(db, aid, qid, blank, "carol@example.com")
    db.flush()

    detail = _assessment_detail(db, aid)
    assert sum(g.answered_count for g in detail.question_groups) == 1, (
        "the blank answer was not filed as complete — this test is no longer "
        "exercising the case it exists for"
    )

    report = build_report(db, aid)
    assert report.total_count == 1
    assert report.answered_count == 0
    assert report.completeness == 0.0
    # The row itself is still printed, with its recorded status — the
    # document must not silently drop a question (D-PDF-5).
    assert len(report.sections[0].questions) == 1


def test_export_mode_is_carried(db):
    tid = _seed_template(db)
    aid = _seed_assessment(db, tid, "Export Mode DPIA")
    db.flush()

    assert build_report(db, aid).export_mode == "external"
    assert build_report(db, aid, export_mode="internal").export_mode == "internal"


def test_build_report_returns_a_report_instance(db):
    tid = _seed_template(db)
    aid = _seed_assessment(db, tid, "Type Check DPIA")
    db.flush()
    assert isinstance(build_report(db, aid), Report)


def test_the_officers_words_pass_through_untouched(db):
    # The report model must not escape, sanitise or truncate. Escaping belongs
    # to the renderer, and doing it in both places double-escapes: an officer
    # who writes `R&D` sees `R&amp;D` in the document they signed and the
    # regulator reads.
    #
    # Asserted rather than inspected, because the very next task adds escaping
    # on the rendering side. That is exactly when someone "helpfully" adds it
    # here too.
    awkward = (
        'Shared with R&D <partners> — see "Annexe 1".\n'
        "Retention: 7 years; reviewed annually."
    )
    tid = _seed_template(db)
    aid = _seed_assessment(db, tid, "Verbatim Text DPIA")
    qid = _seed_question(db, tid, "q1", "necessity", 1)
    write_answer(db, aid, qid, awkward, "carol@example.com")
    db.flush()

    report = build_report(db, aid)

    answers = [q.answer_text for s in report.sections for q in s.questions]
    assert awkward in answers, (
        f"the officer's text was altered on the way into the report model: {answers!r}"
    )


# --- Task 4: the ODPC finding, wired into the report. Spec acceptance 7 is
# not met by a function nobody can see — build_report must call
# risk.odpc.evaluate and surface the result, both as a typed field (for any
# future caller that wants it as data) and as metadata rows (so it is
# printed, not just carried).


def test_report_carries_the_odpc_finding_as_data(db):
    tid = _seed_template(db)
    aid = _seed_assessment(db, tid, "Low Risk DPIA")
    db.flush()

    report = build_report(db, aid)

    assert isinstance(report.odpc, OdpcFinding)
    assert report.odpc.required is False
    assert report.odpc.band == LOW
    assert report.odpc.window_days == CONSULTATION_WINDOW_DAYS


def test_a_critical_register_states_consultation_is_required_in_metadata(db):
    tid = _seed_template(db)
    aid = _seed_assessment(db, tid, "Critical Risk DPIA")
    add_risk(db, assessment_id=aid, category="confidentiality",
              description="catastrophic exposure of fuel card PINs",
              likelihood=5, severity=5)
    db.flush()

    report = build_report(db, aid)
    metadata = dict(report.metadata)

    assert report.odpc.required is True
    assert report.odpc.band == CRITICAL
    # Silence reads as "not assessed" — the verdict, the 60-day window, and
    # the risk that drove it must all be readable straight off the metadata
    # rows a person (or a regulator) reads at the top of the document.
    odpc_row = metadata["ODPC Prior Consultation"]
    assert "required" in odpc_row.lower()
    assert str(CONSULTATION_WINDOW_DAYS) in odpc_row
    assert "catastrophic exposure of fuel card PINs" in odpc_row


def test_a_low_risk_register_states_consultation_is_not_required_in_metadata(db):
    # "Say the negative out loud" (task brief) — a low-risk assessment must
    # not just omit the ODPC row; it must say NOT required, so silence can
    # never be misread as "nobody checked".
    tid = _seed_template(db)
    aid = _seed_assessment(db, tid, "Low Risk DPIA")
    db.flush()

    report = build_report(db, aid)
    metadata = dict(report.metadata)

    assert report.odpc.required is False
    odpc_row = metadata["ODPC Prior Consultation"]
    assert "not required" in odpc_row.lower()
