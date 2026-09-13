# The two questionnaire-chat routes, against real rows. Inserts are rolled
# back (see the `db` fixture in test_api_assessments.py, imported below along
# with the seeding helpers and `_fake_client` rather than duplicating them).
#
# phrase_question/judge_reply are ALWAYS monkeypatched here — never a real
# gateway call. The `_stub_llm` fixture below applies a default stub to every
# test in this module (autouse), and individual tests override judge_reply's
# stub where the test is specifically about a non-responsive reply.
from typing import List

import pytest
import sqlalchemy
from fastapi import HTTPException
from sqlalchemy.orm import Session

from fides.api.privacycare.api.answers import write_answer
from fides.api.privacycare.api.chat import (
    _session_by_id,
    get_questionnaire_chat_messages,
    reply_to_questionnaire_chat,
    start_questionnaire_chat,
)
from fides.api.privacycare.api.schemas import (
    ChatReplyRequest,
    ChatReplyResponse,
    QuestionnaireChatMessage,
    QuestionnaireSessionStatus,
    StartChatRequest,
    StartChatResponse,
)
from fides.api.privacycare.chat import QUESTIONNAIRE_STATUSES
from tests.privacycare.test_api_assessments import (
    _fake_client,
    _seed_assessment,
    _seed_question,
    _seed_template,
)
from tests.privacycare.test_api_schemas import (
    _feature_interface_field_specs,
    _feature_interface_fields,
    _ts_enum_values,
)

DB_URL = "postgresql://postgres:fides@127.0.0.1:5442/fides"


@pytest.fixture
def db():
    engine = sqlalchemy.create_engine(DB_URL)
    with Session(engine) as session:
        yield session
        session.rollback()


@pytest.fixture(autouse=True)
def _stub_llm(monkeypatch):
    # Every test in this module goes through this stub instead of a real
    # gateway call. Phrasing always succeeds and is traceable back to the
    # question it phrased (so a test can tell which question a bot message
    # is about without inspecting question_index); judging defaults to
    # ANSWERED — the handful of tests about a deflection override this one
    # function for just that test.
    monkeypatch.setattr(
        "fides.api.privacycare.api.chat.phrase_question",
        lambda question, context, **kw: f"[phrased] {question['question_key']}",
    )
    monkeypatch.setattr(
        "fides.api.privacycare.api.chat.judge_reply",
        lambda question, reply, **kw: True,
    )


def _no_commit(db, monkeypatch):
    # Every route test commits nothing — a real commit would persist this
    # test's seeded rows past the `db` fixture's rollback and into the
    # shared database (the exact failure the task brief calls out: "A
    # review on this plan committed one stray row and broke six unrelated
    # tests").
    monkeypatch.setattr(db, "commit", lambda: None)


def _answer_row(db, assessment_id: str, question_id: str):
    return db.execute(
        sqlalchemy.text(
            "SELECT av.answer_text, av.answer_status, av.answer_source, "
            "       av.change_type, av.created_by "
            "FROM assessment_answer a "
            "JOIN answer_version av ON av.id = a.current_version_id "
            "WHERE a.assessment_id = :aid AND a.question_id = :qid"
        ),
        {"aid": assessment_id, "qid": question_id},
    ).mappings().first()


def _questionnaire_row(db, questionnaire_id: str):
    return db.execute(
        sqlalchemy.text(
            "SELECT status, current_question_index FROM questionnaire "
            "WHERE id = :id"
        ),
        {"id": questionnaire_id},
    ).mappings().first()


# --- POST plus/chat/questionnaire/start ---


def test_start_returns_a_questionnaire_id_a_bot_message_and_total_questions(
    db, monkeypatch
):
    tid = _seed_template(db)
    aid = _seed_assessment(db, tid, "Start Chat DPIA")
    _seed_question(db, tid, "q1", "necessity", 1)
    db.flush()
    _no_commit(db, monkeypatch)

    response = start_questionnaire_chat(StartChatRequest(assessment_id=aid), db=db)

    assert isinstance(response, StartChatResponse)
    assert response.questionnaire_id
    assert response.assessment_id == aid
    assert response.total_questions == 1
    assert len(response.messages) == 1
    assert response.messages[0].is_bot_message is True
    assert response.messages[0].text == "[phrased] q1"


def test_start_on_an_assessment_with_nothing_pending_returns_a_completed_session_and_no_questions(
    db, monkeypatch
):
    tid = _seed_template(db)
    aid = _seed_assessment(db, tid, "Nothing Pending DPIA")
    qid = _seed_question(db, tid, "q1", "necessity", 1)
    # Answered directly through write_answer BEFORE any chat session ever
    # exists for this assessment (not through chat.record_answer, which
    # would require a session to already be open) — pending_question_ids
    # (chat.py) excludes a 'complete' answer, so the FIRST session
    # open_or_resume ever creates here has zero questions to ask.
    write_answer(db, aid, qid, "Already answered before chat started.", "alice@example.com")
    db.flush()
    _no_commit(db, monkeypatch)

    response = start_questionnaire_chat(StartChatRequest(assessment_id=aid), db=db)

    assert response.total_questions == 0
    assert response.messages == [], (
        "nothing pending must not be shown an empty prompt"
    )
    # The underlying session really is completed, not merely reported as if
    # it were — advance() is what actually transitions it.
    row = _questionnaire_row(db, response.questionnaire_id)
    assert row["status"] == "completed"


def test_start_unknown_assessment_is_a_404(db, monkeypatch):
    _no_commit(db, monkeypatch)
    with pytest.raises(HTTPException) as exc_info:
        start_questionnaire_chat(
            StartChatRequest(assessment_id="no-such-assessment"), db=db
        )
    assert exc_info.value.status_code == 404


# --- POST plus/chat/questionnaire/reply ---


def _started_session(db, monkeypatch, *, question_count: int = 2) -> str:
    """Seed a template/assessment with `question_count` pending questions,
    start its chat session, and return the questionnaire_id. Shared setup
    for every reply test below.
    """
    tid = _seed_template(db)
    aid = _seed_assessment(db, tid, "Reply Chat DPIA")
    for i in range(question_count):
        _seed_question(db, tid, f"q{i}", "necessity", i + 1)
    db.flush()
    _no_commit(db, monkeypatch)
    return start_questionnaire_chat(StartChatRequest(assessment_id=aid), db=db).questionnaire_id


def test_reply_files_the_answer_verbatim_and_returns_the_next_bot_message(
    db, monkeypatch
):
    questionnaire_id = _started_session(db, monkeypatch, question_count=2)
    session_before = _session_by_id(db, questionnaire_id)
    answered_question_id = session_before.question_ids[0]

    response = reply_to_questionnaire_chat(
        ChatReplyRequest(
            questionnaire_id=questionnaire_id,
            message_text="We rely on consent, captured at signup.",
        ),
        db=db,
        client=_fake_client("carol@example.com"),
    )

    assert isinstance(response, ChatReplyResponse)
    row = _answer_row(db, session_before.assessment_id, answered_question_id)
    assert row["answer_text"] == "We rely on consent, captured at signup."
    assert row["answer_status"] == "complete"
    assert row["answer_source"] == "user_input"
    assert row["change_type"] == "human_edited"
    assert row["created_by"] == "carol@example.com"

    assert response.answered_questions == 1
    assert response.total_questions == 2
    assert response.status == QuestionnaireSessionStatus.IN_PROGRESS
    # Echo of the recorded answer, then the next question phrased — both
    # bot turns from this reply, in order.
    assert len(response.bot_messages) == 2
    assert all(m.is_bot_message for m in response.bot_messages)
    assert response.bot_messages[1].text == "[phrased] q1"


def test_the_echoed_bot_message_quotes_the_recorded_answer(db, monkeypatch):
    # The task-2 review's ruling: an answer that gets filed must be echoed
    # back verbatim in the very next bot message, so the officer sees what
    # went in at the moment it happens rather than discovering a wrongly
    # judged deflection on review later.
    questionnaire_id = _started_session(db, monkeypatch, question_count=1)
    recorded_text = "Legal basis is legitimate interest, documented in policy 4.2."

    response = reply_to_questionnaire_chat(
        ChatReplyRequest(questionnaire_id=questionnaire_id, message_text=recorded_text),
        db=db,
        client=_fake_client("carol@example.com"),
    )

    assert recorded_text in response.bot_messages[0].text


def test_a_non_responsive_reply_files_nothing_and_re_asks(db, monkeypatch):
    questionnaire_id = _started_session(db, monkeypatch, question_count=1)
    session_before = _session_by_id(db, questionnaire_id)
    question_id = session_before.question_ids[0]

    monkeypatch.setattr(
        "fides.api.privacycare.api.chat.judge_reply",
        lambda question, reply, **kw: False,
    )

    response = reply_to_questionnaire_chat(
        ChatReplyRequest(
            questionnaire_id=questionnaire_id, message_text="I'll check with legal."
        ),
        db=db,
        client=_fake_client("carol@example.com"),
    )

    assert _answer_row(db, session_before.assessment_id, question_id) is None, (
        "a deflection judged non-responsive must file no answer"
    )
    assert response.answered_questions == 0
    assert response.status == QuestionnaireSessionStatus.IN_PROGRESS
    assert len(response.bot_messages) == 1, "a re-ask, nothing else"
    assert response.bot_messages[0].text == "[phrased] q0"

    row = _questionnaire_row(db, questionnaire_id)
    assert row["current_question_index"] == 0, "the session must not advance"


def test_the_final_reply_flips_status_to_completed(db, monkeypatch):
    questionnaire_id = _started_session(db, monkeypatch, question_count=1)

    response = reply_to_questionnaire_chat(
        ChatReplyRequest(
            questionnaire_id=questionnaire_id, message_text="The only answer needed."
        ),
        db=db,
        client=_fake_client("carol@example.com"),
    )

    assert response.status == QuestionnaireSessionStatus.COMPLETED
    assert response.answered_questions == 1
    assert response.total_questions == 1
    row = _questionnaire_row(db, questionnaire_id)
    assert row["status"] == "completed"


def test_reply_to_an_unknown_questionnaire_id_is_a_404(db, monkeypatch):
    _no_commit(db, monkeypatch)
    with pytest.raises(HTTPException) as exc_info:
        reply_to_questionnaire_chat(
            ChatReplyRequest(questionnaire_id="qnr_doesnotexist", message_text="Hi"),
            db=db,
            client=_fake_client("carol@example.com"),
        )
    assert exc_info.value.status_code == 404


def test_reply_route_created_by_comes_from_the_caller_not_the_body(db, monkeypatch):
    # Same rule update_answer holds itself to: authorship of the filed
    # answer must come from the AUTHENTICATED client, never something the
    # request body could forge — and ChatReplyRequest has no field for it
    # at all.
    questionnaire_id = _started_session(db, monkeypatch, question_count=1)
    session_before = _session_by_id(db, questionnaire_id)
    question_id = session_before.question_ids[0]

    reply_to_questionnaire_chat(
        ChatReplyRequest(questionnaire_id=questionnaire_id, message_text="An answer."),
        db=db,
        client=_fake_client("dana@example.com", id="client_should_not_be_used"),
    )

    row = _answer_row(db, session_before.assessment_id, question_id)
    assert row["created_by"] == "dana@example.com"


def test_assessment_id_absent_from_the_reply_body_is_accepted(db, monkeypatch):
    # The wire body the UI actually sends: privacy-assessments.slice.ts's
    # chat mutation strips assessment_id before the request goes out
    # (`query: ({ assessment_id: _, ...body }) => body`). ChatReplyRequest
    # must accept a body carrying only questionnaire_id/message_text.
    questionnaire_id = _started_session(db, monkeypatch, question_count=1)

    request = ChatReplyRequest.model_validate(
        {"questionnaire_id": questionnaire_id, "message_text": "An answer."}
    )
    assert request.assessment_id is None

    response = reply_to_questionnaire_chat(
        request, db=db, client=_fake_client("carol@example.com")
    )
    assert isinstance(response, ChatReplyResponse)


def test_chat_reply_request_assessment_id_is_not_required():
    # The documented, deliberate deviation from the TS contract (which
    # types assessment_id as required, no `?`): the field must not be a
    # required Pydantic field despite that, or a body genuinely missing it
    # (as the UI sends) would 422 rather than reach the route at all.
    assert not ChatReplyRequest.model_fields["assessment_id"].is_required()


# --- GET plus/chat/questionnaire/messages/{questionnaire_id} ---


def test_get_messages_returns_the_transcript_oldest_first(db, monkeypatch):
    questionnaire_id = _started_session(db, monkeypatch, question_count=2)

    reply_to_questionnaire_chat(
        ChatReplyRequest(
            questionnaire_id=questionnaire_id,
            message_text="We rely on consent, captured at signup.",
        ),
        db=db,
        client=_fake_client("carol@example.com"),
    )

    messages = get_questionnaire_chat_messages(questionnaire_id, db=db)

    # start's own bot question, then the officer's reply, then the two bot
    # turns reply produces (echo + next question) — four rows, oldest
    # first, exactly matching chat.transcript's own ORDER BY.
    assert [m.text for m in messages] == [
        "[phrased] q0",
        "We rely on consent, captured at signup.",
        'Recorded as your answer: "We rely on consent, captured at signup."',
        "[phrased] q1",
    ]
    assert [m.is_bot_message for m in messages] == [True, False, True, True]
    assert messages[1].sender_email == "carol@example.com"
    assert all(m.timestamp for m in messages), "transcript() always stamps a timestamp"


def test_get_messages_is_the_bare_array_the_slice_declares():
    # getQuestionnaireChatMessages (privacy-assessments.slice.ts) types its
    # query as build.query<QuestionnaireChatMessage[], string> — a bare
    # array, not an envelope with e.g. a `messages` key. The route's own
    # response_model carries this; this test pins the return type of the
    # handler itself so a future refactor cannot silently wrap it.
    assert get_questionnaire_chat_messages.__annotations__["return"] == List[
        QuestionnaireChatMessage
    ]


def test_get_messages_on_a_session_with_nothing_said_yet_is_a_200_with_an_empty_list(
    db, monkeypatch
):
    # A session that exists but has no messages (chat.py's "nothing
    # pending" branch never writes a bot message) must not be confused
    # with an unknown questionnaire_id — that is a 404, this is a 200.
    tid = _seed_template(db)
    aid = _seed_assessment(db, tid, "Nothing Pending For Transcript DPIA")
    qid = _seed_question(db, tid, "q1", "necessity", 1)
    write_answer(db, aid, qid, "Already answered before chat started.", "alice@example.com")
    db.flush()
    _no_commit(db, monkeypatch)
    questionnaire_id = start_questionnaire_chat(
        StartChatRequest(assessment_id=aid), db=db
    ).questionnaire_id

    messages = get_questionnaire_chat_messages(questionnaire_id, db=db)

    assert messages == []


def test_get_messages_unknown_questionnaire_id_is_a_404(db, monkeypatch):
    _no_commit(db, monkeypatch)
    with pytest.raises(HTTPException) as exc_info:
        get_questionnaire_chat_messages("qnr_doesnotexist", db=db)
    assert exc_info.value.status_code == 404


# --- TS parity: the shipped contract this module's schemas mirror ---


def test_questionnaire_chat_message_matches_the_shipped_contract():
    assert set(QuestionnaireChatMessage.model_fields) == _feature_interface_fields(
        "QuestionnaireChatMessage"
    )


def test_questionnaire_chat_message_optionality_matches_the_shipped_contract():
    for field, is_optional in _feature_interface_field_specs(
        "QuestionnaireChatMessage"
    ).items():
        pydantic_required = QuestionnaireChatMessage.model_fields[field].is_required()
        assert pydantic_required == (not is_optional), field


def test_start_chat_request_matches_the_shipped_contract():
    assert set(StartChatRequest.model_fields) == _feature_interface_fields(
        "StartChatRequest"
    )


def test_start_chat_response_matches_the_shipped_contract():
    assert set(StartChatResponse.model_fields) == _feature_interface_fields(
        "StartChatResponse"
    )


def test_chat_reply_request_field_set_matches_the_shipped_contract():
    # Field NAMES match the TS interface exactly; only assessment_id's
    # OPTIONALITY deviates (see test_chat_reply_request_assessment_id_is_
    # not_required above and ChatReplyRequest's own schemas.py docstring).
    assert set(ChatReplyRequest.model_fields) == _feature_interface_fields(
        "ChatReplyRequest"
    )


def test_chat_reply_response_matches_the_shipped_contract():
    assert set(ChatReplyResponse.model_fields) == _feature_interface_fields(
        "ChatReplyResponse"
    )


def test_the_question_status_enum_matches_the_shipped_contract():
    assert {s.value for s in QuestionnaireSessionStatus} == _ts_enum_values(
        "QuestionnaireSessionStatus"
    )


def test_question_status_enum_matches_chat_py_questionnaire_statuses():
    # chat.py's QUESTIONNAIRE_STATUSES pins the DB/Ethyca-Python vocabulary;
    # this schema pins the UI vocabulary. Both must name the same three
    # states.
    assert {s.value for s in QuestionnaireSessionStatus} == QUESTIONNAIRE_STATUSES


def test_the_feature_types_file_was_actually_read_for_chat_types():
    # Same guard test_api_schemas.py's own
    # test_the_feature_types_file_was_actually_read applies: a bad path or
    # regex would make the parity assertions above compare empty sets and
    # pass vacuously.
    assert len(_feature_interface_fields("QuestionnaireChatMessage")) == 6
    assert len(_feature_interface_fields("StartChatResponse")) == 4
    assert len(_feature_interface_fields("ChatReplyResponse")) == 4
