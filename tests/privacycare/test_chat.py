# The questionnaire chat session core, against real rows. Inserts are
# rolled back.
import json

import pytest
import sqlalchemy
from sqlalchemy.orm import Session

from fides.api.privacycare.api.answers import write_answer
from fides.api.privacycare.chat import (
    ChatSession,
    advance,
    answered_count,
    begin_turn,
    current_question,
    current_question_id,
    open_or_resume,
    pending_question_ids,
    question_already_asked,
    record_answer,
    record_answer_and_advance,
    record_message,
    skip_unresolvable_questions,
    transcript,
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


def test_a_session_selects_only_questions_that_are_not_complete(db):
    # D-CHAT-4: the same definition answered_count and completeness use. A
    # `partial` machine draft IS offered — it is exactly the kind of answer a
    # human should confirm — while a `complete` one is not asked again.
    tid = _seed_template(db)
    aid = _seed_assessment(db, tid, "Chat DPIA")
    done = _seed_question(db, tid, "q_done", "necessity", 1)
    draft = _seed_question(db, tid, "q_draft", "necessity", 2)
    empty = _seed_question(db, tid, "q_empty", "necessity", 3)
    write_answer(db, aid, done, "Answered.", "alice@example.com")
    write_answer(db, aid, draft, "Machine draft.", "privacycare-generator",
                 answer_status="partial", answer_source="ai_analysis",
                 change_type="ai_generated")
    db.flush()

    assert pending_question_ids(db, aid) == [draft, empty]


def test_starting_twice_resumes_the_open_session(db):
    # D-CHAT-5: two open sessions would race on current_question_index and
    # interleave answers into the wrong questions.
    tid = _seed_template(db)
    aid = _seed_assessment(db, tid, "Resume DPIA")
    _seed_question(db, tid, "q1", "necessity", 1)
    db.flush()

    first = open_or_resume(db, aid)
    second = open_or_resume(db, aid)

    assert first.id == second.id
    count = db.execute(
        sqlalchemy.text("SELECT COUNT(*) FROM questionnaire WHERE assessment_id = :a"),
        {"a": aid},
    ).scalar()
    assert count == 1


def test_an_explicit_question_subset_is_honoured(db):
    tid = _seed_template(db)
    aid = _seed_assessment(db, tid, "Subset DPIA")
    first = _seed_question(db, tid, "q1", "necessity", 1)
    _seed_question(db, tid, "q2", "necessity", 2)
    db.flush()

    assert pending_question_ids(db, aid, include_question_ids=[first]) == [first]


def test_recording_an_answer_writes_it_verbatim_as_a_human_answer(db):
    # D-CHAT-1 and D-CHAT-3. The officer's own words, filed as theirs.
    tid = _seed_template(db)
    aid = _seed_assessment(db, tid, "Verbatim DPIA")
    qid = _seed_question(db, tid, "q1", "necessity", 1)
    db.flush()
    session = open_or_resume(db, aid)

    record_answer(db, session, "We rely on consent, captured at signup.",
                  "carol@example.com", question_index=0)

    row = db.execute(
        sqlalchemy.text(
            "SELECT av.answer_text, av.answer_status, av.answer_source, "
            "       av.change_type, av.created_by "
            "FROM assessment_answer a "
            "JOIN answer_version av ON av.id = a.current_version_id "
            "WHERE a.assessment_id = :a AND a.question_id = :q"
        ),
        {"a": aid, "q": qid},
    ).mappings().first()
    assert row["answer_text"] == "We rely on consent, captured at signup."
    assert row["answer_status"] == "complete"
    assert row["answer_source"] == "user_input"
    assert row["change_type"] == "human_edited"
    assert row["created_by"] == "carol@example.com"


def test_advancing_past_the_last_question_completes_the_session(db):
    tid = _seed_template(db)
    aid = _seed_assessment(db, tid, "Completing DPIA")
    _seed_question(db, tid, "q1", "necessity", 1)
    db.flush()
    session = open_or_resume(db, aid)
    record_answer(db, session, "An answer.", "carol@example.com",
                  question_index=0)

    session = advance(db, session, from_index=0)

    assert session.status == "completed"
    assert current_question(db, session) is None
    # clock_timestamp(), not now(). now() is transaction START, so inside one
    # transaction it can stamp a completion time EARLIER than the work that
    # produced it — the exact bug found and fixed in tasks.py on this branch.
    # Comparing against a clock reading taken before the call distinguishes
    # them: now() would be at or before it, clock_timestamp() after.
    completed_at, before, after = db.execute(
        sqlalchemy.text(
            "SELECT q.completed_at, now(), clock_timestamp() "
            "FROM questionnaire q WHERE q.id = :i"
        ),
        {"i": session.id},
    ).first()
    assert completed_at is not None
    assert completed_at > before, (
        "completed_at looks like now() (transaction start), which can predate "
        f"the work it records: {completed_at} vs now()={before}"
    )
    assert completed_at <= after


def test_messages_come_back_in_order_with_their_sender(db):
    tid = _seed_template(db)
    aid = _seed_assessment(db, tid, "Transcript DPIA")
    _seed_question(db, tid, "q1", "necessity", 1)
    db.flush()
    session = open_or_resume(db, aid)
    record_message(db, session.id, "What is the lawful basis?", is_bot=True,
                   question_index=0)
    record_message(db, session.id, "Consent.", is_bot=False,
                   sender_email="carol@example.com", question_index=0)

    messages = transcript(db, session.id)

    assert [m["text"] for m in messages] == ["What is the lawful basis?", "Consent."]
    assert [m["is_bot_message"] for m in messages] == [True, False]
    assert messages[1]["sender_email"] == "carol@example.com"


def test_a_session_for_an_unknown_assessment_raises(db):
    with pytest.raises(LookupError):
        open_or_resume(db, "pa_does_not_exist")




def _set_question_ids(db, questionnaire_id: str, question_ids: list[str]) -> None:
    """Rewrite a session's frozen question list directly.

    provider_context is free-form JSONB on an Ethyca-authored table, so a
    question_ids entry that no longer resolves is reachable in production
    two ways: a question removed from the template after the session froze
    its list, and a questionnaire row this app did not write. Both look
    like this from chat.py's side.
    """
    db.execute(
        sqlalchemy.text(
            "UPDATE questionnaire "
            "SET provider_context = CAST(:pc AS JSONB) WHERE id = :i"
        ),
        {"pc": json.dumps({"question_ids": question_ids}), "i": questionnaire_id},
    )


def _stored(db, questionnaire_id: str):
    return db.execute(
        sqlalchemy.text(
            "SELECT status, current_question_index FROM questionnaire WHERE id = :i"
        ),
        {"i": questionnaire_id},
    ).mappings().first()


def test_begin_turn_re_reads_the_stored_index_and_status_under_the_lock(db):
    # The whole-range review's CRITICAL, in one assertion. `advance` used to
    # re-read the index while `record_answer` used the one carried on the
    # session object — two halves of one read-then-write, each deciding for
    # itself, disagreeing whenever a concurrent reply had moved the row in
    # between. Neither reads any more; begin_turn is the single read, and it
    # takes the parent-assessment lock BEFORE it, so nothing can move the
    # row between this read and the writes derived from it.
    tid = _seed_template(db)
    aid = _seed_assessment(db, tid, "Begin Turn DPIA")
    _seed_question(db, tid, "q1", "necessity", 1)
    _seed_question(db, tid, "q2", "necessity", 2)
    _seed_question(db, tid, "q3", "necessity", 3)
    db.flush()
    session = open_or_resume(db, aid)

    # Someone else moved the session on and stopped it. `session` still says
    # index 0 / in_progress.
    db.execute(
        sqlalchemy.text(
            "UPDATE questionnaire "
            "SET current_question_index = 1, status = 'stopped' WHERE id = :i"
        ),
        {"i": session.id},
    )

    fresh = begin_turn(db, session)

    assert fresh.current_question_index == 1
    assert fresh.status == "stopped", (
        "begin_turn read the index under the lock but carried status off the "
        "stale object — the same half-corrected read the index defect was"
    )


def test_record_and_advance_file_and_move_from_the_same_index(db):
    # One lock, one read, both writes: the answer is filed against the index
    # begin_turn read, and the session advances from that SAME index. Handed
    # a deliberately stale session (index 0) against a row that has moved on
    # to index 1, the answer must land on q2 — not on q1 (the stale read)
    # with the cursor jumping to 2 and q2 never asked.
    tid = _seed_template(db)
    aid = _seed_assessment(db, tid, "Pairing DPIA")
    q1 = _seed_question(db, tid, "q1", "necessity", 1)
    q2 = _seed_question(db, tid, "q2", "necessity", 2)
    q3 = _seed_question(db, tid, "q3", "necessity", 3)
    db.flush()
    stale = open_or_resume(db, aid)
    db.execute(
        sqlalchemy.text(
            "UPDATE questionnaire SET current_question_index = 1 WHERE id = :i"
        ),
        {"i": stale.id},
    )

    moved = record_answer_and_advance(
        db, stale, "The officer's words.", "carol@example.com"
    )

    assert moved.current_question_index == 2
    answered = db.execute(
        sqlalchemy.text(
            "SELECT a.question_id, av.answer_text FROM assessment_answer a "
            "JOIN answer_version av ON av.id = a.current_version_id "
            "WHERE a.assessment_id = :a"
        ),
        {"a": aid},
    ).mappings().all()
    assert [(r["question_id"], r["answer_text"]) for r in answered] == [
        (q2, "The officer's words.")
    ]
    assert q1 not in {r["question_id"] for r in answered}
    assert q3 not in {r["question_id"] for r in answered}


def test_record_answer_will_not_file_against_an_index_past_the_end(db):
    tid = _seed_template(db)
    aid = _seed_assessment(db, tid, "Past End DPIA")
    _seed_question(db, tid, "q1", "necessity", 1)
    db.flush()
    session = open_or_resume(db, aid)

    with pytest.raises(LookupError) as exc_info:
        record_answer(db, session, "An answer.", "carol@example.com", question_index=1)

    assert "no question at index 1" in str(exc_info.value), (
        "the message must name what was actually absent, not the questionnaire"
    )


def test_current_question_id_tells_past_the_end_from_did_not_resolve(db):
    # current_question returns None for both, which is exactly how an
    # unresolvable entry used to be routed into the "session is already
    # complete" branch and stall there forever.
    tid = _seed_template(db)
    aid = _seed_assessment(db, tid, "Distinguish DPIA")
    _seed_question(db, tid, "q1", "necessity", 1)
    db.flush()
    session = open_or_resume(db, aid)
    ghost = ChatSession(
        id=session.id,
        assessment_id=aid,
        status="in_progress",
        current_question_index=0,
        question_ids=["q_deleted_since"],
    )
    past_end = ChatSession(
        id=session.id,
        assessment_id=aid,
        status="in_progress",
        current_question_index=1,
        question_ids=[session.question_ids[0]],
    )

    assert current_question(db, ghost) is None
    assert current_question_id(ghost) == "q_deleted_since", "not past the end"
    assert current_question(db, past_end) is None
    assert current_question_id(past_end) is None, "genuinely past the end"


def test_a_question_that_no_longer_resolves_is_skipped_not_stalled_on(db):
    tid = _seed_template(db)
    aid = _seed_assessment(db, tid, "Ghost Question DPIA")
    real = _seed_question(db, tid, "q1", "necessity", 1)
    db.flush()
    session = open_or_resume(db, aid)
    _set_question_ids(db, session.id, ["q_deleted_since", real])
    session = ChatSession(
        id=session.id,
        assessment_id=aid,
        status="in_progress",
        current_question_index=0,
        question_ids=["q_deleted_since", real],
    )

    session = skip_unresolvable_questions(db, session)

    assert session.current_question_index == 1
    assert current_question(db, session)["id"] == real
    assert _stored(db, session.id)["current_question_index"] == 1


def test_a_question_from_another_template_is_skipped_rather_than_a_500(db):
    # write_answer's _require_question_in_template raises
    # QuestionNotInTemplateError — a ValueError, which the route does not
    # catch — so a frozen question_ids entry that has drifted off the
    # assessment's template used to be an uncaught 500 on every reply, at
    # the same index, forever. current_question resolves through the same
    # join write_answer enforces, so the entry never reaches write_answer.
    tid = _seed_template(db)
    other_tid = _seed_template(db)
    aid = _seed_assessment(db, tid, "Foreign Question DPIA")
    foreign = _seed_question(db, other_tid, "q_foreign", "necessity", 1)
    mine = _seed_question(db, tid, "q1", "necessity", 1)
    db.flush()
    session = open_or_resume(db, aid)
    _set_question_ids(db, session.id, [foreign, mine])
    session = ChatSession(
        id=session.id,
        assessment_id=aid,
        status="in_progress",
        current_question_index=0,
        question_ids=[foreign, mine],
    )

    session = skip_unresolvable_questions(db, session)

    assert session.current_question_index == 1
    assert current_question(db, session)["id"] == mine


def test_a_session_whose_every_question_is_unresolvable_completes(db):
    tid = _seed_template(db)
    aid = _seed_assessment(db, tid, "All Ghosts DPIA")
    _seed_question(db, tid, "q1", "necessity", 1)
    db.flush()
    opened = open_or_resume(db, aid)
    session = ChatSession(
        id=opened.id,
        assessment_id=aid,
        status="in_progress",
        current_question_index=0,
        question_ids=["q_gone_a", "q_gone_b"],
    )

    session = skip_unresolvable_questions(db, session)

    assert current_question_id(session) is None
    assert session.status == "completed"
    assert _stored(db, opened.id)["status"] == "completed"


def test_answered_questions_is_a_count_of_complete_answers_not_the_cursor(db):
    tid = _seed_template(db)
    aid = _seed_assessment(db, tid, "Counting DPIA")
    q1 = _seed_question(db, tid, "q1", "necessity", 1)
    _seed_question(db, tid, "q2", "necessity", 2)
    q3 = _seed_question(db, tid, "q3", "necessity", 3)
    db.flush()
    session = open_or_resume(db, aid)
    assert answered_count(db, session) == 0

    record_answer(db, session, "First.", "carol@example.com", question_index=0)
    db.flush()
    assert answered_count(db, session) == 1

    # The cursor moved two places while only one more answer was filed —
    # the shape a skipped question leaves behind. The count must follow the
    # answers, not the pointer.
    session = advance(db, session, from_index=0)
    session = advance(db, session, from_index=1)
    record_answer(db, session, "Third.", "carol@example.com", question_index=2)
    db.flush()
    assert session.current_question_index == 2
    assert answered_count(db, session) == 2
    assert {q1, q3} == {
        r[0]
        for r in db.execute(
            sqlalchemy.text(
                "SELECT question_id FROM assessment_answer WHERE assessment_id = :a"
            ),
            {"a": aid},
        ).all()
    }


def test_a_partial_answer_does_not_count_as_answered(db):
    # The SAME definition answered_count (assessments.py) and completeness
    # (answers.py) use: only a CURRENT version with answer_status exactly
    # 'complete'. A machine draft offered for review is not progress.
    tid = _seed_template(db)
    aid = _seed_assessment(db, tid, "Partial DPIA")
    qid = _seed_question(db, tid, "q1", "necessity", 1)
    db.flush()
    session = open_or_resume(db, aid)
    write_answer(db, aid, qid, "Machine draft.", "privacycare-generator",
                 answer_status="partial", answer_source="ai_analysis",
                 change_type="ai_generated")
    db.flush()

    assert answered_count(db, session) == 0


def test_answered_count_ignores_answers_outside_this_session(db):
    tid = _seed_template(db)
    aid = _seed_assessment(db, tid, "Scoped Count DPIA")
    q1 = _seed_question(db, tid, "q1", "necessity", 1)
    q2 = _seed_question(db, tid, "q2", "necessity", 2)
    db.flush()
    # Only q2 is pending when the session opens, so q1's answer belongs to
    # the assessment but not to this session's progress.
    write_answer(db, aid, q1, "Answered before the chat.", "alice@example.com")
    db.flush()
    session = open_or_resume(db, aid)
    assert session.question_ids == [q2]

    assert answered_count(db, session) == 0


def test_an_empty_session_completes_at_index_zero_not_one(db):
    # start's nothing-pending branch used to advance an empty question list
    # to index 1, so a later reply reported "1 answered of 0". The count no
    # longer comes from the cursor, and the cursor no longer runs past the
    # list it indexes either.
    tid = _seed_template(db)
    aid = _seed_assessment(db, tid, "Empty DPIA")
    db.flush()
    session = open_or_resume(db, aid)
    assert session.question_ids == []

    session = advance(db, session, from_index=0)

    assert session.status == "completed"
    assert session.current_question_index == 0
    assert _stored(db, session.id)["current_question_index"] == 0
    assert answered_count(db, session) == 0


def test_question_already_asked_sees_only_bot_turns_at_that_index(db):
    tid = _seed_template(db)
    aid = _seed_assessment(db, tid, "Asked DPIA")
    _seed_question(db, tid, "q1", "necessity", 1)
    db.flush()
    session = open_or_resume(db, aid)
    assert question_already_asked(db, session.id, 0) is False

    record_message(db, session.id, "Consent.", is_bot=False,
                   sender_email="carol@example.com", question_index=0)
    assert question_already_asked(db, session.id, 0) is False, (
        "the officer's own message is not the question being asked"
    )

    record_message(db, session.id, "What is the lawful basis?", is_bot=True,
                   question_index=0)
    assert question_already_asked(db, session.id, 0) is True
    assert question_already_asked(db, session.id, 1) is False
