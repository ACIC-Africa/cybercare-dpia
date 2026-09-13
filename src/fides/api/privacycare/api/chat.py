"""The three routes that drive the questionnaire chat: start, reply, and
the read-only transcript.

fides.api.privacycare.chat (the core, task 1) owns the session state
machine — opening/resuming a questionnaire, tracking which question is
current, and writing the officer's answer through write_answer.
fides.api.privacycare.chat_llm (task 2) owns the two gateway calls —
phrasing a question conversationally and judging whether a reply answered
it. This module is the thin route layer that sequences the two: on every
turn it asks chat.py what to do, asks chat_llm.py how to say it, and
persists the result as a chat_message row via chat.record_message.

Same conventions as api/tasks.py: route commits, the `_`-prefixed core
below does not; LookupError maps to 404; `status` is imported as
`status_codes` because `status` is also this module's own vocabulary (a
questionnaire's session status) — see tasks.py's own comment for why that
shadowing risk is worth naming explicitly rather than just avoiding by
convention.

A RULING from the task-2 review, not a stylistic choice: if the model
wrongly judges a deflection as answered, a non-answer is filed silently —
the worse of the two error directions a DPIA can make. The guard lives in
the conversation, not the prompt: whenever record_answer actually files
something, the very next bot message echoes the text just recorded, in
quotes, so the officer sees what went in at the moment it happens rather
than discovering it on review later. Because chat.record_answer's answer
chain is append-only (write_answer's own contract), a wrong echo is not a
disaster either — the officer's correction becomes version 2 with both
preserved.
"""
from typing import List

import sqlalchemy
from fastapi import Depends, HTTPException, Security
from fastapi import status as status_codes
from sqlalchemy.orm import Session

from fides.api.deps import get_db
from fides.api.models.client import ClientDetail
from fides.api.oauth.utils import verify_oauth_client
from fides.api.privacycare.api.identity import _created_by_from_client
from fides.api.privacycare.api.router import privacycare_chat_router
from fides.api.privacycare.api.schemas import (
    ChatReplyRequest,
    ChatReplyResponse,
    QuestionnaireChatMessage,
    StartChatRequest,
    StartChatResponse,
)
from fides.api.privacycare.chat import (
    ChatSession,
    advance,
    current_question,
    open_or_resume,
    record_answer,
    record_message,
    transcript,
)
from fides.api.privacycare.chat_llm import judge_reply, phrase_question
from fides.api.privacycare.context import GenerationTarget, build_context
from fides.common.scope_registry import SYSTEM_READ

# The one bot message shown once nothing is left to ask — either because
# the very last question was just answered (reply's completing turn) or
# because the officer's message arrived after the session was already
# complete (a stray extra message, e.g. a slow double-submit from the UI).
# Never sent as an empty string or omitted: an assistant turn with nothing
# to say reads to the officer as the app having silently failed.
_SESSION_COMPLETE_MESSAGE = (
    "That's everything for this session — thank you. "
    "The questionnaire is now complete."
)

_QUESTIONNAIRE_BY_ID_SQL = sqlalchemy.text(
    "SELECT id, assessment_id, status, current_question_index, provider_context "
    "FROM questionnaire WHERE id = :questionnaire_id"
)

# The same four denormalised columns select_targets (context.py's own
# caller, generator.py) reads off privacydeclaration/ctl_systems, except
# they are read here straight off privacy_assessment itself:
# generator.py's _generate_one already copies them onto the assessment row
# at creation time (system_fides_key, system_name, declaration_id,
# declaration_name, data_use, data_use_name, data_categories — the exact
# columns _ASSESSMENT_SQL in assessments.py already selects for the read
# surface), so a second join back through privacydeclaration/ctl_systems
# to rebuild the same facts would be redundant and could disagree with
# what generation actually recorded for this assessment if either
# upstream row changed since.
_ASSESSMENT_CONTEXT_SQL = sqlalchemy.text(
    "SELECT system_fides_key, system_name, declaration_id, declaration_name, "
    "       data_use, data_use_name, data_categories "
    "FROM privacy_assessment WHERE id = :assessment_id"
)


def _session_by_id(db: Session, questionnaire_id: str) -> ChatSession:
    """The ChatSession for an existing questionnaire row, found by ITS id
    rather than by assessment_id.

    open_or_resume (chat.py) is keyed on assessment_id because that is what
    `start` receives. `reply` receives questionnaire_id instead — the UI's
    slice strips assessment_id off the wire body entirely (see
    ChatReplyRequest's own docstring in schemas.py) — so the session has to
    be found the other way. Building the same ChatSession shape by hand
    here, rather than adding a second lookup mode to chat.open_or_resume,
    keeps that function's one contract ("assessment_id in, resume-or-create
    out") intact; this is a pure read with no create-if-missing branch, so
    it does not belong there.
    """
    row = (
        db.execute(_QUESTIONNAIRE_BY_ID_SQL, {"questionnaire_id": questionnaire_id})
        .mappings()
        .first()
    )
    if row is None:
        raise LookupError(f"No questionnaire with id {questionnaire_id}")
    provider_context = row["provider_context"] or {}
    return ChatSession(
        id=row["id"],
        assessment_id=row["assessment_id"],
        status=row["status"],
        current_question_index=row["current_question_index"],
        question_ids=list(provider_context.get("question_ids", [])),
    )


def _context_for(db: Session, assessment_id: str) -> dict:
    """The record chat_llm.phrase_question phrases a question against —
    the same build_context contract generator.py uses, applied to the one
    assessment this session belongs to rather than a whole generation run's
    worth of targets.
    """
    row = (
        db.execute(_ASSESSMENT_CONTEXT_SQL, {"assessment_id": assessment_id})
        .mappings()
        .first()
    )
    if row is None:
        raise LookupError(f"No assessment with id {assessment_id}")
    target = GenerationTarget(
        system_fides_key=row["system_fides_key"],
        system_name=row["system_name"],
        declaration_id=row["declaration_id"],
        declaration_name=row["declaration_name"],
        data_use=row["data_use"],
        data_use_name=row["data_use_name"],
        data_categories=list(row["data_categories"] or []),
    )
    return build_context(db, target)


def _chat_message_from_row(row: dict) -> QuestionnaireChatMessage:
    return QuestionnaireChatMessage(
        text=row["text"],
        is_bot_message=row["is_bot_message"],
        sender_email=row["sender_email"],
        sender_display_name=row["sender_display_name"],
        timestamp=row["timestamp"].isoformat() if row["timestamp"] else None,
        question_index=row["question_index"],
    )


def _start_chat(db: Session, request: StartChatRequest) -> StartChatResponse:
    """Open or resume the session and ask its current question.

    Nothing-pending is a real, distinct branch, not a degenerate case of
    the general one: a session whose question list is already empty is
    advanced straight to `completed` and NO bot message is written. The
    alternative — phrasing "the current question" when there is none —
    has nothing to phrase, so the only two honest options are an empty
    prompt (confusing: the officer sees a chat box with no question in it)
    or a placeholder invented for the occasion (this module is not
    permitted to invent content chat_llm.py wasn't asked to produce). An
    empty transcript on a session the caller can see is complete is
    neither.
    """
    session = open_or_resume(db, request.assessment_id, request.include_question_ids)
    question = current_question(db, session)

    if question is None:
        session = advance(db, session)
        return StartChatResponse(
            questionnaire_id=session.id,
            assessment_id=session.assessment_id,
            messages=[],
            total_questions=len(session.question_ids),
        )

    context = _context_for(db, session.assessment_id)
    text = phrase_question(question, context)
    record_message(
        db, session.id, text, is_bot=True, question_index=session.current_question_index
    )

    messages = [_chat_message_from_row(row) for row in transcript(db, session.id)]
    return StartChatResponse(
        questionnaire_id=session.id,
        assessment_id=session.assessment_id,
        messages=messages,
        total_questions=len(session.question_ids),
    )


def _reply(db: Session, request: ChatReplyRequest, created_by: str) -> ChatReplyResponse:
    """Persist the officer's message, judge it, and respond.

    Answered: record_answer files it verbatim, advance() moves the
    session on, and the FIRST bot message echoes the recorded text back —
    the task-2 review's ruling, not optional (see this module's own
    docstring). The second bot message is whatever comes next: the
    following question, phrased, or the closing message if that was the
    last one.

    Not answered: nothing is filed. The same question is re-asked — a
    fresh phrase_question call, not a cached repeat of the earlier
    prompt, so the officer is not stuck reading the identical sentence
    twice if the model can phrase it differently the second time.

    Every bot text this turn produces is persisted through
    record_message, in order, and then read back via transcript() rather
    than assembled by hand: transcript() is the one place `timestamp`
    actually gets a value (clock_timestamp(), stamped by the INSERT
    itself), and re-deriving that here would either invent a timestamp or
    leave it null for no reason.
    """
    session = _session_by_id(db, request.questionnaire_id)
    question = current_question(db, session)

    record_message(
        db,
        session.id,
        request.message_text,
        is_bot=False,
        sender_email=created_by,
        question_index=session.current_question_index if question is not None else None,
    )

    # (text, question_index) pairs, in the order they must be persisted and
    # returned. Built up before the single write loop below so the "read
    # back the tail of the transcript" step has an exact count to slice on.
    bot_turns: list[tuple[str, int | None]] = []

    if question is None:
        # The session was already complete before this message arrived (a
        # stray extra reply after the last question was answered). Nothing
        # to judge it against; just say so.
        bot_turns.append((_SESSION_COMPLETE_MESSAGE, None))
    elif judge_reply(question, request.message_text):
        answered_index = session.current_question_index
        record_answer(db, session, request.message_text, created_by)
        session = advance(db, session)
        bot_turns.append(
            (f'Recorded as your answer: "{request.message_text}"', answered_index)
        )
        next_question = current_question(db, session)
        if next_question is not None:
            context = _context_for(db, session.assessment_id)
            bot_turns.append(
                (phrase_question(next_question, context), session.current_question_index)
            )
        else:
            bot_turns.append((_SESSION_COMPLETE_MESSAGE, None))
    else:
        # Non-responsive: file nothing, re-ask the same question. D-CHAT-1 —
        # only the officer's own words become an answer, never a model's
        # guess at one.
        context = _context_for(db, session.assessment_id)
        bot_turns.append(
            (phrase_question(question, context), session.current_question_index)
        )

    for text, question_index in bot_turns:
        record_message(db, session.id, text, is_bot=True, question_index=question_index)

    rows = transcript(db, session.id)
    bot_messages = [_chat_message_from_row(row) for row in rows[-len(bot_turns) :]]

    return ChatReplyResponse(
        bot_messages=bot_messages,
        status=session.status,
        answered_questions=session.current_question_index,
        total_questions=len(session.question_ids),
    )


@privacycare_chat_router.post(
    "/start",
    dependencies=[Security(verify_oauth_client, scopes=[SYSTEM_READ])],
    response_model=StartChatResponse,
)
def start_questionnaire_chat(
    request: StartChatRequest, *, db: Session = Depends(get_db)
) -> StartChatResponse:
    """Open or resume the officer's questionnaire session for one
    assessment and return its first (or next) question already asked.

    No `client` parameter: unlike `reply`, this route files no officer
    content — only a bot message chat_llm.py produced — so there is no
    authorship to attribute. The SYSTEM_READ scope check still runs, via
    the route's own `dependencies=`, the same shape get_assessment_task
    (api/tasks.py) uses for a route with nothing to write on the caller's
    behalf.
    """
    try:
        response = _start_chat(db, request)
    except LookupError:
        raise HTTPException(
            status_code=status_codes.HTTP_404_NOT_FOUND,
            detail=f"No assessment with id {request.assessment_id}",
        )
    db.commit()
    return response


@privacycare_chat_router.post(
    "/reply",
    dependencies=[Security(verify_oauth_client, scopes=[SYSTEM_READ])],
    response_model=ChatReplyResponse,
)
def reply_to_questionnaire_chat(
    request: ChatReplyRequest,
    *,
    db: Session = Depends(get_db),
    client: ClientDetail = Security(verify_oauth_client, scopes=[SYSTEM_READ]),
) -> ChatReplyResponse:
    """One turn of the conversation: the officer's message in, the
    resulting bot message(s) out.

    `request.assessment_id` is read nowhere in this function — see
    ChatReplyRequest's own docstring for why it is optional on the wire at
    all. The questionnaire is found by `questionnaire_id` alone
    (_session_by_id), and authorship for both the chat_message row and any
    answer_version this turn files comes from the AUTHENTICATED client,
    never the request body — the same rule update_answer (api/tasks.py's
    sibling in api/assessments.py) already holds itself to.
    """
    created_by = _created_by_from_client(client)
    try:
        response = _reply(db, request, created_by)
    except LookupError:
        raise HTTPException(
            status_code=status_codes.HTTP_404_NOT_FOUND,
            detail=f"No questionnaire with id {request.questionnaire_id}",
        )
    db.commit()
    return response


@privacycare_chat_router.get(
    "/messages/{questionnaire_id}",
    dependencies=[Security(verify_oauth_client, scopes=[SYSTEM_READ])],
    response_model=List[QuestionnaireChatMessage],
)
def get_questionnaire_chat_messages(
    questionnaire_id: str, *, db: Session = Depends(get_db)
) -> List[QuestionnaireChatMessage]:
    """The conversation so far, oldest first — exactly what
    getQuestionnaireChatMessages (privacy-assessments.slice.ts) types its
    query as: `build.query<QuestionnaireChatMessage[], string>`, a bare
    array, not an envelope. No wrapper is invented here to "tidy" that
    shape; the slice is the actual contract, not the TS interface file in
    isolation, and it declares an array.

    `transcript()` alone cannot distinguish "no such questionnaire" from
    "this questionnaire exists but nothing has been said yet" — its SQL is
    a plain WHERE questionnaire_id = ... with no existence check, so an
    unknown id and a genuinely empty session both come back as `[]`. Those
    are different situations (one is a 404, the other a 200 with an empty
    list) and must not collapse into each other, so _session_by_id's own
    existence check runs first and its LookupError is what actually maps
    to 404 — the same shape start/reply already use.

    No `client` parameter: this is a pure read, nothing is filed on the
    caller's behalf, so there is no authorship to attribute — same
    reasoning as start_questionnaire_chat's own docstring above.
    """
    try:
        session = _session_by_id(db, questionnaire_id)
    except LookupError:
        raise HTTPException(
            status_code=status_codes.HTTP_404_NOT_FOUND,
            detail=f"No questionnaire with id {questionnaire_id}",
        )
    return [_chat_message_from_row(row) for row in transcript(db, session.id)]
