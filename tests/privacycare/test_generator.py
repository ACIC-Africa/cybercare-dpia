import uuid

import pytest
import sqlalchemy
from sqlalchemy.orm import Session

from fides.api.privacycare.api.assessments import _assessment_detail, _evidence_for
from fides.api.privacycare.generator import (
    GENERATOR_AUTHOR,
    draft_from_context,
    answer_questions,
)
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


_CONTEXT = {
    "system": {"name": "CRM", "description": "Customer relationship system"},
    "privacy_declaration": {
        "name": "Email campaigns",
        "data_use": "marketing.advertising",
        "data_categories": ["user.contact.email", "user.behavior"],
    },
    "data_use": {"name": "Advertising", "description": "Promoting goods."},
}


def _question(coverage, sources, key="q1", text="A question?"):
    return {
        "id": f"aq_{uuid.uuid4().hex[:8]}",
        "question_key": key,
        "question_text": text,
        "guidance": None,
        "expected_coverage": coverage,
        "fides_sources": sources,
    }


def test_full_coverage_drafts_a_complete_answer_from_the_sources():
    question = _question(
        "full", ["privacy_declaration.data_use", "data_use.name"]
    )

    draft = draft_from_context(question, _CONTEXT)

    assert draft is not None
    assert draft.answer_status == "complete"
    assert draft.answer_source == "system"
    assert "marketing.advertising" in draft.answer_text
    assert "Advertising" in draft.answer_text


def test_full_coverage_answer_carries_one_evidence_item_per_resolved_source():
    question = _question(
        "full", ["privacy_declaration.data_use", "data_use.name"]
    )

    draft = draft_from_context(question, _CONTEXT)

    items = draft.evidence["items"]
    assert len(items) == 2
    assert [i["source_key"] for i in items] == [
        "privacy_declaration.data_use",
        "data_use.name",
    ]
    assert [i["citation_number"] for i in items] == [1, 2]
    assert {i["type"] for i in items} == {"system"}
    assert items[0]["value"] == "marketing.advertising"


def test_none_coverage_drafts_nothing():
    # An answer_version asserting "nobody answered this" is not an answer.
    # 243 of the 376 shipped questions are `none`; writing a row for each
    # would bury the audit chain a regulator reads. The detail route already
    # renders an unanswered question as needs_input
    # (_question_response: `q["answer_status"] or "needs_input"`).
    assert draft_from_context(_question("none", []), _CONTEXT) is None


def test_full_coverage_with_no_resolvable_source_drafts_nothing():
    # expected_coverage says the record CAN answer this; if the record
    # turns out not to hold the fact, the honest outcome is an unanswered
    # question, not a confident answer built from nothing.
    question = _question("full", ["privacy_declaration.retention_period"])

    assert draft_from_context(question, _CONTEXT) is None


def test_full_coverage_skips_sources_that_do_not_resolve():
    question = _question(
        "full",
        ["privacy_declaration.data_use", "privacy_declaration.retention_period"],
    )

    draft = draft_from_context(question, _CONTEXT)

    assert len(draft.evidence["items"]) == 1
    assert "retention_period" not in draft.answer_text


def test_partial_coverage_drafts_nothing_without_the_llm():
    # The LLM branch lands in the next task. Until then `partial` must be a
    # deliberate no-write, not an accidental one.
    question = _question("partial", ["system.name"])

    assert draft_from_context(question, _CONTEXT) is None


def test_answer_questions_writes_only_the_full_coverage_questions(db):
    tid = _seed_template(db)
    aid = _seed_assessment(db, tid, "Coverage DPIA")
    full_q = _seed_question(db, tid, "full_q", "necessity", 1)
    none_q = _seed_question(db, tid, "none_q", "necessity", 2)
    db.execute(
        sqlalchemy.text(
            "UPDATE assessment_question SET expected_coverage = 'full', "
            "fides_sources = :sources WHERE id = :id"
        ),
        {"sources": ["privacy_declaration.data_use"], "id": full_q},
    )
    db.execute(
        sqlalchemy.text(
            "UPDATE assessment_question SET expected_coverage = 'none' "
            "WHERE id = :id"
        ),
        {"id": none_q},
    )
    db.flush()

    written = answer_questions(db, aid, _CONTEXT, use_llm=False, model=None)

    assert written == 1
    answered = db.execute(
        sqlalchemy.text(
            "SELECT question_id FROM assessment_answer WHERE assessment_id = :aid"
        ),
        {"aid": aid},
    ).scalars().all()
    assert answered == [full_q]


def test_generated_answers_are_attributed_to_the_generator(db):
    tid = _seed_template(db)
    aid = _seed_assessment(db, tid, "Attribution DPIA")
    qid = _seed_question(db, tid, "full_q", "necessity", 1)
    db.execute(
        sqlalchemy.text(
            "UPDATE assessment_question SET expected_coverage = 'full', "
            "fides_sources = :sources WHERE id = :id"
        ),
        {"sources": ["privacy_declaration.data_use"], "id": qid},
    )
    db.flush()

    answer_questions(db, aid, _CONTEXT, use_llm=False, model=None)

    row = db.execute(
        sqlalchemy.text(
            "SELECT av.created_by, av.answer_source, av.change_type "
            "FROM assessment_answer a "
            "JOIN answer_version av ON av.id = a.current_version_id "
            "WHERE a.assessment_id = :aid"
        ),
        {"aid": aid},
    ).mappings().first()

    assert row["created_by"] == GENERATOR_AUTHOR
    assert row["answer_source"] == "system"
    assert row["change_type"] == "ai_generated"


def test_citation_numbers_are_unique_across_one_assessment(db):
    # Evidence citations are rendered as [1], [2] … in the DPIA report. Two
    # different answers both claiming citation [1] makes the report's
    # references ambiguous.
    tid = _seed_template(db)
    aid = _seed_assessment(db, tid, "Citations DPIA")
    for order, key in enumerate(("q_a", "q_b"), start=1):
        qid = _seed_question(db, tid, key, "necessity", order)
        db.execute(
            sqlalchemy.text(
                "UPDATE assessment_question SET expected_coverage = 'full', "
                "fides_sources = :sources WHERE id = :id"
            ),
            {"sources": ["privacy_declaration.data_use", "data_use.name"], "id": qid},
        )
    db.flush()

    answer_questions(db, aid, _CONTEXT, use_llm=False, model=None)

    numbers = []
    rows = db.execute(
        sqlalchemy.text(
            "SELECT av.evidence FROM assessment_answer a "
            "JOIN answer_version av ON av.id = a.current_version_id "
            "WHERE a.assessment_id = :aid"
        ),
        {"aid": aid},
    ).scalars().all()
    for evidence in rows:
        numbers.extend(item["citation_number"] for item in evidence["items"])

    assert sorted(numbers) == [1, 2, 3, 4]


def test_full_coverage_evidence_round_trips_through_both_reader_paths(db):
    # Fix round 1 (coordinator finding, CRITICAL): the nine tests above only
    # assert on the dict draft_from_context/answer_questions returns — none
    # of them follow the {"items": [...]} container into the database and
    # back out through the code that actually renders it. That is exactly
    # how a shape mismatch with the only consumer (_evidence_item_from_
    # payload, which used to treat the whole payload as ONE EvidenceItem)
    # passed nine green tests while making every full-coverage answer's
    # evidence vanish from both the /evidence endpoint and the detail
    # screen's evidence drawer.
    #
    # This test goes end to end: generate a full-coverage answer citing TWO
    # sources, then read it back through BOTH real consumer paths and assert
    # both citations arrive, in order, with source_key and citation_number
    # intact.
    tid = _seed_template(db)
    aid = _seed_assessment(db, tid, "Round Trip DPIA")
    qid = _seed_question(db, tid, "full_q", "necessity", 1)
    db.execute(
        sqlalchemy.text(
            "UPDATE assessment_question SET expected_coverage = 'full', "
            "fides_sources = :sources WHERE id = :id"
        ),
        {"sources": ["privacy_declaration.data_use", "data_use.name"], "id": qid},
    )
    db.flush()

    written = answer_questions(db, aid, _CONTEXT, use_llm=False, model=None)
    assert written == 1

    # Path 1: the /evidence endpoint.
    evidence = _evidence_for(db, aid)
    assert evidence["total_count"] == 2
    endpoint_pairs = [
        (item["source_key"], item["citation_number"]) for item in evidence["items"]
    ]
    assert endpoint_pairs == [
        ("privacy_declaration.data_use", 1),
        ("data_use.name", 2),
    ]

    # Path 2: the detail screen's per-question evidence.
    detail = _assessment_detail(db, aid)
    question = detail.question_groups[0].questions[0]
    detail_pairs = [
        (item.source_key, item.citation_number) for item in question.evidence
    ]
    assert detail_pairs == [
        ("privacy_declaration.data_use", 1),
        ("data_use.name", 2),
    ]


def test_citation_counter_holds_when_a_middle_question_drafts_nothing(db):
    # MINOR (coordinator review): the counter only advances by the number of
    # items a WRITTEN draft actually carried (see answer_questions), so a
    # question that drafts nothing in the middle of a template must not
    # leave a gap in the numbering — q1 gets [1], q3 gets [2], never [1],
    # [3]. Nothing proved this before; test_citation_numbers_are_unique_
    # across_one_assessment only ever seeded adjacent full-coverage
    # questions, so it could not distinguish "holds" from "always advances
    # by len(sources) regardless of whether the draft was written".
    tid = _seed_template(db)
    aid = _seed_assessment(db, tid, "No Gap DPIA")
    q1 = _seed_question(db, tid, "q1", "necessity", 1)
    q2 = _seed_question(db, tid, "q2", "necessity", 2)
    q3 = _seed_question(db, tid, "q3", "necessity", 3)
    db.execute(
        sqlalchemy.text(
            "UPDATE assessment_question SET expected_coverage = 'full', "
            "fides_sources = :sources WHERE id = :id"
        ),
        {"sources": ["privacy_declaration.data_use"], "id": q1},
    )
    db.execute(
        sqlalchemy.text(
            "UPDATE assessment_question SET expected_coverage = 'none' "
            "WHERE id = :id"
        ),
        {"id": q2},
    )
    db.execute(
        sqlalchemy.text(
            "UPDATE assessment_question SET expected_coverage = 'full', "
            "fides_sources = :sources WHERE id = :id"
        ),
        {"sources": ["data_use.name"], "id": q3},
    )
    db.flush()

    written = answer_questions(db, aid, _CONTEXT, use_llm=False, model=None)
    assert written == 2

    rows = db.execute(
        sqlalchemy.text(
            "SELECT a.question_id, av.evidence FROM assessment_answer a "
            "JOIN answer_version av ON av.id = a.current_version_id "
            "WHERE a.assessment_id = :aid"
        ),
        {"aid": aid},
    ).mappings().all()
    citations_by_question = {
        row["question_id"]: [item["citation_number"] for item in row["evidence"]["items"]]
        for row in rows
    }
    assert citations_by_question == {q1: [1], q3: [2]}
