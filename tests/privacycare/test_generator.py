import uuid

import pytest
import sqlalchemy
from sqlalchemy.orm import Session

from fides.api.privacycare import llm as llm_module
from fides.api.privacycare.api.assessments import _assessment_detail, _evidence_for
from fides.api.privacycare.generator import (
    _SOURCE_ROOT_LABELS,
    _label,
    GENERATION_CALLER,
    GENERATOR_AUTHOR,
    NEEDS_INPUT_SENTINEL,
    answer_questions,
    draft_from_context,
    draft_with_llm,
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


def test_a_label_names_the_subject_the_fact_belongs_to():
    # `system.name` and `privacy_declaration.name` are BOTH "Name" if the
    # dotted root is thrown away. The model then reads two facts under one
    # label and can attribute either to the wrong subject, while the evidence
    # items -- which keep source_key -- still cite correctly. A wrong
    # statement wearing a correct citation is the worst failure shape this
    # pipeline has.
    assert _label("system.name") == "System name"
    assert _label("privacy_declaration.name") == "Processing activity name"
    assert _label("data_use.name") == "Data use name"
    assert _label("data_category.name") == "Data category name"
    assert _label("privacy_declaration.data_use") == "Processing activity data use"


def test_an_unmapped_root_still_keeps_its_root_in_the_label():
    # A root added to assessment_question.fides_sources that nobody added to
    # _SOURCE_ROOT_LABELS must NOT silently collapse back to the field name
    # and reintroduce the collision. It falls back to the root itself.
    assert _label("vendor.name") == "Vendor name"
    assert _label("vendor.name") != _label("system.name")


def test_every_root_label_is_distinct():
    # The no-collision guarantee rests on this: two roots sharing a prefix
    # would make `a.name` and `b.name` the same label again.
    labels = list(_SOURCE_ROOT_LABELS.values())
    assert len(labels) == len(set(labels))


def test_no_shipped_question_has_two_sources_with_the_same_label(db):
    # The standing guarantee, asserted against the real data rather than a
    # hand-picked example: for EVERY question in the database, no two of its
    # fides_sources render under the same label. Before the root->prefix
    # mapping this failed on 7 questions, among them the opening question of
    # dpia_1_1, cpra_1_1 and cnil_1_1.
    rows = db.execute(
        sqlalchemy.text(
            "SELECT question_key, fides_sources FROM assessment_question "
            "WHERE fides_sources IS NOT NULL"
        )
    ).mappings().all()
    assert rows, "no shipped questions found -- the guarantee would be vacuous"

    collisions = {}
    for row in rows:
        sources = list(row["fides_sources"] or [])
        labels = [_label(source) for source in sources]
        if len(set(labels)) != len(set(sources)):
            collisions[row["question_key"]] = sources
    assert collisions == {}


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


def test_partial_coverage_asks_the_model_and_records_a_partial_answer(monkeypatch):
    captured = {}

    def _fake_complete(caller, messages, *, model, max_tokens, system=None):
        captured["caller"] = caller
        captured["messages"] = messages
        captured["model"] = model
        captured["system"] = system
        return "The activity is described as email marketing to existing customers."

    monkeypatch.setattr("fides.api.privacycare.generator.complete", _fake_complete)

    question = _question(
        "partial", ["system.name", "privacy_declaration.name"],
        text="What is the name and description of this processing activity?",
    )

    draft = draft_with_llm(question, _CONTEXT, model=None)

    assert draft.answer_status == "partial"
    assert draft.answer_source == "ai_analysis"
    assert draft.answer_text.startswith("The activity is described")
    assert captured["caller"] == GENERATION_CALLER


def test_an_llm_draft_cites_its_facts_as_ai_analysis_evidence(monkeypatch):
    # The facts are still the record's; what the model contributed is the
    # prose. Typing the evidence "ai_analysis" rather than "system" is what
    # tells a reviewer -- and a regulator -- that this answer was drafted
    # rather than read off the record.
    monkeypatch.setattr(
        "fides.api.privacycare.generator.complete", lambda *a, **k: "An answer."
    )

    question = _question(
        "partial", ["system.name", "privacy_declaration.name"]
    )

    draft = draft_with_llm(question, _CONTEXT, model=None)

    items = draft.evidence["items"]
    assert [i["source_key"] for i in items] == [
        "system.name",
        "privacy_declaration.name",
    ]
    assert {i["type"] for i in items} == {"ai_analysis"}, (
        "a drafted answer's citations must not claim to be read straight "
        f"off the system record: {items!r}"
    )
    assert [i["value"] for i in items] == ["CRM", "Email campaigns"]


def test_an_llm_draft_honours_citation_start(monkeypatch):
    # Citations render as [1], [2] ... across a whole exported DPIA. If the
    # LLM path restarted numbering, two answers would both claim [1] and the
    # report's references would be ambiguous.
    monkeypatch.setattr(
        "fides.api.privacycare.generator.complete", lambda *a, **k: "An answer."
    )

    question = _question(
        "partial", ["system.name", "privacy_declaration.name"]
    )

    draft = draft_with_llm(question, _CONTEXT, model=None, citation_start=7)

    assert [i["citation_number"] for i in draft.evidence["items"]] == [7, 8]


def test_the_prompt_carries_the_resolved_facts_and_the_question(monkeypatch):
    # The model's ONLY ground truth is the resolved sources. If the prompt
    # does not carry them, the model is free to invent the customer's
    # processing — which is the failure mode that makes a generated DPIA
    # worse than no DPIA.
    captured = {}

    def _fake_complete(caller, messages, *, model, max_tokens, system=None):
        captured["prompt"] = messages[0]["content"]
        captured["system"] = system
        return "An answer."

    monkeypatch.setattr("fides.api.privacycare.generator.complete", _fake_complete)

    question = _question(
        "partial", ["system.name", "privacy_declaration.name"],
        text="What is this activity?",
    )
    draft_with_llm(question, _CONTEXT, model=None)

    assert "CRM" in captured["prompt"]
    assert "Email campaigns" in captured["prompt"]
    assert "What is this activity?" in captured["prompt"]
    assert NEEDS_INPUT_SENTINEL in captured["system"]


def test_the_model_can_decline_and_that_writes_nothing(monkeypatch):
    # A model that cannot answer from the record must say so rather than
    # guess. The sentinel is how it says so, and a declined question is
    # left for a human exactly like a `none` question.
    monkeypatch.setattr(
        "fides.api.privacycare.generator.complete",
        lambda *a, **k: NEEDS_INPUT_SENTINEL,
    )

    question = _question("partial", ["system.name"])

    assert draft_with_llm(question, _CONTEXT, model=None) is None


def test_a_declining_reply_is_recognised_despite_surrounding_whitespace(monkeypatch):
    monkeypatch.setattr(
        "fides.api.privacycare.generator.complete",
        lambda *a, **k: f"  {NEEDS_INPUT_SENTINEL}\n",
    )
    assert draft_with_llm(_question("partial", ["system.name"]), _CONTEXT, model=None) is None


def test_an_empty_reply_writes_nothing(monkeypatch):
    monkeypatch.setattr(
        "fides.api.privacycare.generator.complete", lambda *a, **k: "   "
    )
    assert draft_with_llm(_question("partial", ["system.name"]), _CONTEXT, model=None) is None


def test_a_partial_question_with_no_resolvable_facts_never_calls_the_model(monkeypatch):
    # No facts means nothing to ground the answer in. Calling the model
    # anyway would spend budget to invite a hallucination.
    def _explode(*args, **kwargs):
        raise AssertionError("the model must not be called with no facts")

    monkeypatch.setattr("fides.api.privacycare.generator.complete", _explode)

    question = _question("partial", ["privacy_declaration.retention_period"])

    assert draft_with_llm(question, _CONTEXT, model=None) is None


def test_a_gateway_failure_leaves_the_question_unanswered(monkeypatch):
    # llm.py is explicit that a caller must NOT fall back to a direct model
    # call. The degraded outcome is an unanswered question, which a human
    # can still answer — not a bypassed redactor.
    def _unavailable(*args, **kwargs):
        raise llm_module.GatewayUnavailable("gateway returned 429: budget exceeded")

    monkeypatch.setattr("fides.api.privacycare.generator.complete", _unavailable)

    assert draft_with_llm(_question("partial", ["system.name"]), _CONTEXT, model=None) is None


def test_the_requested_model_is_passed_through(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        "fides.api.privacycare.generator.complete",
        lambda caller, messages, *, model, max_tokens, system=None: captured.update(
            model=model
        )
        or "An answer.",
    )

    draft_with_llm(_question("partial", ["system.name"]), _CONTEXT, model="claude-opus-5")

    assert captured["model"] == "claude-opus-5"


def test_answer_questions_skips_the_llm_entirely_when_use_llm_is_false(db, monkeypatch):
    def _explode(*args, **kwargs):
        raise AssertionError("use_llm=False must not reach the gateway")

    monkeypatch.setattr("fides.api.privacycare.generator.complete", _explode)

    tid = _seed_template(db)
    aid = _seed_assessment(db, tid, "No LLM DPIA")
    qid = _seed_question(db, tid, "partial_q", "necessity", 1)
    db.execute(
        sqlalchemy.text(
            "UPDATE assessment_question SET expected_coverage = 'partial', "
            "fides_sources = :sources WHERE id = :id"
        ),
        {"sources": ["system.name"], "id": qid},
    )
    db.flush()

    assert answer_questions(db, aid, _CONTEXT, use_llm=False, model=None) == 0


def test_answer_questions_writes_the_llm_answer_when_use_llm_is_true(db, monkeypatch):
    monkeypatch.setattr(
        "fides.api.privacycare.generator.complete",
        lambda *a, **k: "Drafted from the record.",
    )

    tid = _seed_template(db)
    aid = _seed_assessment(db, tid, "LLM DPIA")
    qid = _seed_question(db, tid, "partial_q", "necessity", 1)
    db.execute(
        sqlalchemy.text(
            "UPDATE assessment_question SET expected_coverage = 'partial', "
            "fides_sources = :sources WHERE id = :id"
        ),
        {"sources": ["system.name"], "id": qid},
    )
    db.flush()

    assert answer_questions(db, aid, _CONTEXT, use_llm=True, model=None) == 1

    row = db.execute(
        sqlalchemy.text(
            "SELECT av.answer_status, av.answer_source, av.answer_text "
            "FROM assessment_answer a "
            "JOIN answer_version av ON av.id = a.current_version_id "
            "WHERE a.assessment_id = :aid"
        ),
        {"aid": aid},
    ).mappings().first()
    assert row["answer_status"] == "partial"
    assert row["answer_source"] == "ai_analysis"
    assert row["answer_text"] == "Drafted from the record."
