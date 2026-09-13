# The DPIA report as a document model. Inserts are rolled back (see the
# `db` fixture below, same convention as test_api_assessments.py and every
# other file in this package).
import pytest
import sqlalchemy
from sqlalchemy.orm import Session

from fides.api.privacycare.api.assessments import _assessment_detail
from fides.api.privacycare.report import Report, ReportCitation, build_report
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


def test_counts_and_completeness_match_the_detail_screen_exactly(db):
    # Rule 2: assert this equality directly, not "looks about right". A PDF
    # that disagrees with the screen is worse than no PDF.
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
    assert report.completeness == detail.completeness
    # Concretely, not just "equal to itself": one of three questions is
    # complete.
    assert report.answered_count == 1
    assert report.total_count == 3


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
