# The two questionnaire-chat routes, against real rows. Inserts are rolled
# back (see the `db` fixture in test_api_assessments.py, imported below along
# with the seeding helpers and `_fake_client` rather than duplicating them).
#
# phrase_question/judge_reply are ALWAYS monkeypatched here — never a real
# gateway call. The `_stub_llm` fixture below applies a default stub to every
# test in this module (autouse), and individual tests override judge_reply's
# stub where the test is specifically about a non-responsive reply.
import json
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
    # array, not an envelope with e.g. a `messages` key.
    #
    # Asserted on the REGISTERED ROUTE's response_model, not on the handler's
    # return annotation. FastAPI serialises what the decorator declares, so a
    # refactor that wrapped the response there — the exact mistake that has
    # shipped three empty screens in this module — would leave the annotation
    # untouched and pass an annotation-only check.
    from fides.api.privacycare.api.router import PRIVACYCARE_CHAT_PREFIX
    from fides.api.privacycare.asgi import app

    route = next(
        r
        for r in app.routes
        if getattr(r, "path", "") == f"{PRIVACYCARE_CHAT_PREFIX}/messages/{{questionnaire_id}}"
        and "GET" in getattr(r, "methods", set())
    )
    assert route.response_model == List[QuestionnaireChatMessage], (
        f"the registered route serialises {route.response_model!r}, not the "
        "bare array the slice declares"
    )


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


# Parity for the chat RESPONSE models lives in test_api_schemas.py alongside
# every other response-contract test, and is richer there (it adds optionality
# checks for both responses). It used to be duplicated here; two copies parsing
# the same .ts is maintenance surface that rots at different rates, and the
# weaker copy is the one people trust. The REQUEST schemas and the status enum
# stay here, where the routes that consume them are tested.
def test_start_chat_request_matches_the_shipped_contract():
    assert set(StartChatRequest.model_fields) == _feature_interface_fields(
        "StartChatRequest"
    )


def test_chat_reply_request_field_set_matches_the_shipped_contract():
    # Field NAMES match the TS interface exactly; only assessment_id's
    # OPTIONALITY deviates (see test_chat_reply_request_assessment_id_is_
    # not_required above and ChatReplyRequest's own schemas.py docstring).
    assert set(ChatReplyRequest.model_fields) == _feature_interface_fields(
        "ChatReplyRequest"
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


# --- The whole-range review's CRITICAL: two in-flight replies ---


def _seeded_chat(db, monkeypatch, question_count: int):
    """Template + assessment + `question_count` questions + a started chat.
    Returns (assessment_id, questionnaire_id).
    """
    tid = _seed_template(db)
    aid = _seed_assessment(db, tid, "Interleaved Chat DPIA")
    for i in range(question_count):
        _seed_question(db, tid, f"q{i}", "necessity", i + 1)
    db.flush()
    _no_commit(db, monkeypatch)
    questionnaire_id = start_questionnaire_chat(
        StartChatRequest(assessment_id=aid), db=db
    ).questionnaire_id
    return aid, questionnaire_id


def _filed(db, assessment_id: str) -> dict:
    """{question_key: [answer_text, ...]} — every version, oldest first."""
    rows = db.execute(
        sqlalchemy.text(
            "SELECT q.question_key, av.answer_text, av.version_number "
            "FROM assessment_answer a "
            "JOIN answer_version av ON av.answer_id = a.id "
            "JOIN assessment_question q ON q.id = a.question_id "
            "WHERE a.assessment_id = :aid "
            "ORDER BY q.question_key, av.version_number"
        ),
        {"aid": assessment_id},
    ).mappings().all()
    filed: dict = {}
    for row in rows:
        filed.setdefault(row["question_key"], []).append(row["answer_text"])
    return filed


def test_two_interleaved_replies_ask_every_question_exactly_once(db, monkeypatch):
    """The defect the whole-range review found, as a permanent test.

    _session_by_id takes no lock, so two replies in flight for the same
    questionnaire genuinely both read the same index — that read is what is
    stale here, reproduced by handing every reply in this test the SAME
    handle read once at index 0. Before the fix: reply A filed q0 and
    advanced the row to 1; reply B filed against its stale 0 (q0 again) and
    then advanced from the STORED 1 to 2. q1 was never asked, never
    answered, and the session went on to report itself complete — a DPIA
    filed under DPA 2019 s31 with a question silently blank.

    Threads are not used deliberately: a genuine two-connection test has to
    COMMIT for the second transaction to see the first, and nothing in this
    suite is allowed to leave a row behind. What is actually under test is
    not Postgres' lock but the composition — that the index an answer is
    filed against and the index advanced from come from one authoritative
    read taken after the lock — and a stale handle proves that directly.
    """
    assessment_id, questionnaire_id = _seeded_chat(db, monkeypatch, 3)
    stale = _session_by_id(db, questionnaire_id)
    assert stale.current_question_index == 0, "the read every reply below shares"

    judged: list = []
    asked: list = []
    monkeypatch.setattr(
        "fides.api.privacycare.api.chat.judge_reply",
        lambda question, reply, **kw: judged.append(question["question_key"]) or True,
    )
    monkeypatch.setattr(
        "fides.api.privacycare.api.chat.phrase_question",
        lambda question, context, **kw: asked.append(question["question_key"])
        or f"[phrased] {question['question_key']}",
    )
    # Every reply is handed the same index-0 handle — the shape an unlocked
    # read produces when two replies are in flight at once.
    monkeypatch.setattr(
        "fides.api.privacycare.api.chat._session_by_id",
        lambda _db, _questionnaire_id: stale,
    )

    responses = [
        reply_to_questionnaire_chat(
            ChatReplyRequest(questionnaire_id=questionnaire_id, message_text=text),
            db=db,
            client=_fake_client("carol@example.com"),
        )
        for text in ("Reply A.", "Reply B.", "Reply C.")
    ]

    assert judged == ["q0", "q1", "q2"], (
        "each reply must be judged against the question the session is "
        f"actually on, in order: {judged}"
    )
    assert _filed(db, assessment_id) == {
        "q0": ["Reply A."],
        "q1": ["Reply B."],
        "q2": ["Reply C."],
    }, "every question answered exactly once, none skipped, none filed twice"
    assert asked == ["q1", "q2"], (
        f"every remaining question asked exactly once: {asked}"
    )
    assert [r.answered_questions for r in responses] == [1, 2, 3]
    assert responses[-1].status == QuestionnaireSessionStatus.COMPLETED
    assert _questionnaire_row(db, questionnaire_id)["current_question_index"] == 3


def test_a_stale_reply_files_against_the_question_the_row_is_actually_on(
    db, monkeypatch
):
    # The other half of the same defect: not just "no question skipped" but
    # "no answer filed against the wrong question". The turn's lock is taken
    # before the question is chosen, so a stale handle cannot cause the
    # officer's words to be judged against q0 and filed against q1.
    assessment_id, questionnaire_id = _seeded_chat(db, monkeypatch, 2)
    stale = _session_by_id(db, questionnaire_id)
    db.execute(
        sqlalchemy.text(
            "UPDATE questionnaire SET current_question_index = 1 WHERE id = :i"
        ),
        {"i": questionnaire_id},
    )
    judged: list = []
    monkeypatch.setattr(
        "fides.api.privacycare.api.chat.judge_reply",
        lambda question, reply, **kw: judged.append(question["question_key"]) or True,
    )
    monkeypatch.setattr(
        "fides.api.privacycare.api.chat._session_by_id",
        lambda _db, _questionnaire_id: stale,
    )

    reply_to_questionnaire_chat(
        ChatReplyRequest(
            questionnaire_id=questionnaire_id, message_text="The officer's words."
        ),
        db=db,
        client=_fake_client("carol@example.com"),
    )

    assert judged == ["q1"], "judged against the question the row is on"
    assert _filed(db, assessment_id) == {"q1": ["The officer's words."]}


# --- The whole-range review's MAJOR: a question id that will not resolve ---


def _break_question_ids(db, questionnaire_id: str, question_ids: list) -> None:
    db.execute(
        sqlalchemy.text(
            "UPDATE questionnaire SET provider_context = CAST(:pc AS JSONB) "
            "WHERE id = :i"
        ),
        {"pc": json.dumps({"question_ids": question_ids}), "i": questionnaire_id},
    )


def test_a_reply_skips_an_unresolvable_question_and_asks_the_next_one(db, monkeypatch):
    # Before: current_question returned None for an unresolvable entry, the
    # route read that as "session already complete", and that branch never
    # advances. Every reply forever answered "the questionnaire is now
    # complete" while the row sat at in_progress on the same index, with no
    # answer ever fileable.
    assessment_id, questionnaire_id = _seeded_chat(db, monkeypatch, 2)
    session = _session_by_id(db, questionnaire_id)
    _break_question_ids(
        db, questionnaire_id, ["q_deleted_since", session.question_ids[1]]
    )

    response = reply_to_questionnaire_chat(
        ChatReplyRequest(
            questionnaire_id=questionnaire_id, message_text="The officer's words."
        ),
        db=db,
        client=_fake_client("carol@example.com"),
    )

    assert _filed(db, assessment_id) == {"q1": ["The officer's words."]}, (
        "the bad entry is skipped and the reply files against the next real "
        "question, rather than stalling with nothing fileable"
    )
    assert response.status == QuestionnaireSessionStatus.COMPLETED
    assert _questionnaire_row(db, questionnaire_id)["status"] == "completed"


def test_a_question_on_another_template_does_not_500_the_reply(db, monkeypatch):
    # write_answer raises QuestionNotInTemplateError — a ValueError, which
    # the route catches nowhere — so a frozen question_ids entry that has
    # drifted off the assessment's template used to be an uncaught 500 on
    # every reply, at the same index, forever.
    assessment_id, questionnaire_id = _seeded_chat(db, monkeypatch, 2)
    session = _session_by_id(db, questionnaire_id)
    other_tid = _seed_template(db)
    foreign = _seed_question(db, other_tid, "q_foreign", "necessity", 1)
    db.flush()
    _break_question_ids(db, questionnaire_id, [foreign, session.question_ids[1]])

    response = reply_to_questionnaire_chat(
        ChatReplyRequest(
            questionnaire_id=questionnaire_id, message_text="The officer's words."
        ),
        db=db,
        client=_fake_client("carol@example.com"),
    )

    assert isinstance(response, ChatReplyResponse), "no 500, no ValueError"
    assert _filed(db, assessment_id) == {"q1": ["The officer's words."]}


def test_a_session_with_no_question_ids_at_all_does_not_stall_in_progress(
    db, monkeypatch
):
    # provider_context is free-form JSONB on an Ethyca-authored table, so a
    # questionnaire row carrying no question_ids key is reachable without
    # this app ever writing one. The officer being told "complete" while the
    # row says in_progress forever is the dead end; the row is reconciled.
    _, questionnaire_id = _seeded_chat(db, monkeypatch, 1)
    db.execute(
        sqlalchemy.text(
            "UPDATE questionnaire SET provider_context = CAST('{}' AS JSONB) "
            "WHERE id = :i"
        ),
        {"i": questionnaire_id},
    )

    response = reply_to_questionnaire_chat(
        ChatReplyRequest(questionnaire_id=questionnaire_id, message_text="Hello?"),
        db=db,
        client=_fake_client("carol@example.com"),
    )

    assert response.status == QuestionnaireSessionStatus.COMPLETED
    assert response.answered_questions == 0
    assert response.total_questions == 0
    assert _questionnaire_row(db, questionnaire_id)["status"] == "completed", (
        "the bot said complete; the row must not still say in_progress"
    )


# --- The whole-range review's MAJOR: answered_questions is a count ---


def test_answered_questions_counts_answers_not_the_cursor(db, monkeypatch):
    # A session whose cursor has run ahead of the answers actually filed —
    # the shape a skipped question leaves behind. answered_questions must
    # follow answer_version, the same definition answered_count and
    # completeness use, not the pointer.
    _, questionnaire_id = _seeded_chat(db, monkeypatch, 3)
    session = _session_by_id(db, questionnaire_id)
    _break_question_ids(
        db,
        questionnaire_id,
        ["q_deleted_since", session.question_ids[1], session.question_ids[2]],
    )

    response = reply_to_questionnaire_chat(
        ChatReplyRequest(questionnaire_id=questionnaire_id, message_text="An answer."),
        db=db,
        client=_fake_client("carol@example.com"),
    )

    assert _questionnaire_row(db, questionnaire_id)["current_question_index"] == 2
    assert response.answered_questions == 1, (
        "the cursor is at 2 because index 0 was skipped; only ONE answer has "
        "been filed and that is what the officer is shown"
    )
    assert response.total_questions == 3


def test_a_reply_after_a_nothing_pending_start_does_not_report_one_of_zero(
    db, monkeypatch
):
    # No concurrency needed to see the old bug: start's nothing-pending
    # branch advanced an empty question list, and the next reply reported
    # "1 answered of 0".
    tid = _seed_template(db)
    aid = _seed_assessment(db, tid, "Nothing Pending Reply DPIA")
    qid = _seed_question(db, tid, "q1", "necessity", 1)
    write_answer(db, aid, qid, "Answered before chat started.", "alice@example.com")
    db.flush()
    _no_commit(db, monkeypatch)
    questionnaire_id = start_questionnaire_chat(
        StartChatRequest(assessment_id=aid), db=db
    ).questionnaire_id

    response = reply_to_questionnaire_chat(
        ChatReplyRequest(questionnaire_id=questionnaire_id, message_text="Anything?"),
        db=db,
        client=_fake_client("carol@example.com"),
    )

    assert (response.answered_questions, response.total_questions) == (0, 0)


# --- The whole-range review's MINOR: start is a read when it resumes ---


def test_starting_twice_returns_the_transcript_without_re_asking(db, monkeypatch):
    # A page refresh or a second tab used to grow the transcript — the audit
    # artifact — by one bot turn per visit, and pay for a gateway completion
    # each time.
    calls: list = []
    monkeypatch.setattr(
        "fides.api.privacycare.api.chat.phrase_question",
        lambda question, context, **kw: calls.append(question["question_key"])
        or f"[phrased] {question['question_key']}",
    )
    _, questionnaire_id = _seeded_chat(db, monkeypatch, 2)
    assert calls == ["q0"]

    again = start_questionnaire_chat(
        StartChatRequest(assessment_id=_session_by_id(db, questionnaire_id).assessment_id),
        db=db,
    )

    assert again.questionnaire_id == questionnaire_id
    assert calls == ["q0"], "resuming must not spend a second gateway call"
    assert [m.text for m in again.messages] == ["[phrased] q0"], (
        "resuming returns the transcript it found, not a second copy of the "
        "question the officer is already looking at"
    )
    assert (
        db.execute(
            sqlalchemy.text(
                "SELECT COUNT(*) FROM chat_message WHERE questionnaire_id = :i"
            ),
            {"i": questionnaire_id},
        ).scalar()
        == 1
    )


def test_resuming_onto_a_question_nobody_has_asked_yet_does_ask_it(db, monkeypatch):
    # The test is "has this index already been asked", not "is the transcript
    # non-empty": a session resumed onto a question the officer has never
    # seen must still be asked it.
    calls: list = []
    _, questionnaire_id = _seeded_chat(db, monkeypatch, 2)
    aid = _session_by_id(db, questionnaire_id).assessment_id
    db.execute(
        sqlalchemy.text(
            "UPDATE questionnaire SET current_question_index = 1 WHERE id = :i"
        ),
        {"i": questionnaire_id},
    )
    monkeypatch.setattr(
        "fides.api.privacycare.api.chat.phrase_question",
        lambda question, context, **kw: calls.append(question["question_key"])
        or f"[phrased] {question['question_key']}",
    )

    again = start_questionnaire_chat(StartChatRequest(assessment_id=aid), db=db)

    assert calls == ["q1"]
    assert [m.text for m in again.messages] == ["[phrased] q0", "[phrased] q1"]
