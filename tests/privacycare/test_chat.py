# The questionnaire chat session core, against real rows. Inserts are
# rolled back.
import pytest
import sqlalchemy
from sqlalchemy.orm import Session

from fides.api.privacycare.api.answers import write_answer
from fides.api.privacycare.chat import (
    ChatSession,
    advance,
    current_question,
    open_or_resume,
    pending_question_ids,
    record_answer,
    record_message,
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
                  "carol@example.com")

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
    record_answer(db, session, "An answer.", "carol@example.com")

    session = advance(db, session)

    assert session.status == "completed"
    assert current_question(db, session) is None
    completed_at = db.execute(
        sqlalchemy.text("SELECT completed_at FROM questionnaire WHERE id = :i"),
        {"i": session.id},
    ).scalar()
    assert completed_at is not None


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
