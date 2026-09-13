# The questionnaire chat session core.
#
# Generation (generator.py) answers what the customer's record can support
# and leaves the rest — commonly most of a template's questions on a real
# system. This module is how a data protection officer finishes the DPIA by
# answering those in conversation. The result is filed with a regulator, so
# the standard this module holds itself to is: the officer's own words are
# what gets filed, verbatim. This module never generates answer text — that
# is generator.py's job, and record_answer below writes through write_answer
# exactly the way a human's edit does.
#
# Same conventions as answers.py and generator.py: raw SQL via
# sqlalchemy.text() with bound parameters, a FOR UPDATE lock on the parent
# assessment before any read-then-write, write_answer as the only writer of
# answer_version. `questionnaire` and `chat_message` are Ethyca-authored
# tables (verified against the live schema — see the task brief); this
# module writes rows into them and never alters their schema.
import json
import uuid

import sqlalchemy
from sqlalchemy.orm import Session

from dataclasses import dataclass

from fides.api.privacycare.api.answers import write_answer

# The three values fides.api.models.questionnaire.QuestionnaireStatus
# defines, and the three labels the live `questionnairestatus` pg_enum
# accepts (verified against the schema: it IS a native Postgres enum,
# unlike privacy_assessment_task.status which is a plain varchar). Named
# here, not left as literals, so tests/privacycare/test_vocabularies.py can
# pin this set against all three authorities — the database, Ethyca's
# Python, and the shipped admin UI's QuestionnaireSessionStatus.
QUESTIONNAIRE_STATUSES = frozenset({"in_progress", "completed", "stopped"})


@dataclass(frozen=True)
class ChatSession:
    """An in-memory handle on one questionnaire row.

    Not the ORM model (see this module's no-ORM-coupling convention above)
    and not a re-query of the row on every access: the question list
    selected at session-start time is carried on the dataclass itself,
    sourced once from questionnaire.provider_context, because that
    selection must stay stable across the whole conversation even as
    answers are written underneath it (see pending_question_ids' docstring
    for why re-deriving it mid-session would be wrong).
    """

    id: str
    assessment_id: str
    status: str
    current_question_index: int
    question_ids: list[str]


# Locks the parent assessment row FOR UPDATE before any read-then-write
# below, the same pattern write_answer uses (see answers.py's
# _lock_assessment_and_get_template_id) and for the same reason: two
# concurrent open_or_resume/advance calls for the same assessment_id would
# otherwise race on current_question_index or each insert their own
# questionnaire row. `name` is carried through so a freshly-started
# questionnaire gets a human-legible title without a second query.
_LOCK_ASSESSMENT_SQL = sqlalchemy.text(
    "SELECT name FROM privacy_assessment WHERE id = :assessment_id FOR UPDATE"
)

# "One open questionnaire per assessment": the most recently created
# in_progress row, if any. `completed`/`stopped` rows are deliberately
# excluded — those are past attempts, not something to resume into.
_FIND_OPEN_QUESTIONNAIRE_SQL = sqlalchemy.text(
    "SELECT id, status, current_question_index, provider_context "
    "FROM questionnaire "
    "WHERE assessment_id = :assessment_id AND status = 'in_progress' "
    "ORDER BY created_at DESC LIMIT 1"
)

# Unanswered means answer_status != 'complete' — the SAME definition
# answered_count (assessments.py's _question_group_response) and
# completeness (answers.py's _COMPLETE_ANSWERS_SQL) use. A NULL
# answer_status (no answer_version at all) and an explicit 'partial' both
# count as pending: a partial machine draft IS offered for review here — it
# is exactly the kind of answer a human should confirm — while a 'complete'
# one is not asked again. If this definition ever drifted from the other
# two, a DPO would see one number on the detail screen and a different set
# of questions in the chat.
_PENDING_QUESTION_IDS_SQL = sqlalchemy.text(
    "SELECT q.id "
    "FROM assessment_question q "
    "JOIN privacy_assessment pa ON pa.template_id = q.template_id "
    "LEFT JOIN assessment_answer a "
    "       ON a.question_id = q.id AND a.assessment_id = pa.id "
    "LEFT JOIN answer_version av ON av.id = a.current_version_id "
    "WHERE pa.id = :assessment_id "
    "  AND (av.answer_status IS NULL OR av.answer_status != 'complete') "
    "ORDER BY q.group_order, q.requirement_key, q.question_order, q.id"
)

# provider_context is JSONB and free-form (see the module docstring above);
# {"question_ids": [...]} is the one key this module writes into it. Stored
# at session-start so the selection is stable even if answers change
# underneath mid-session — see pending_question_ids' docstring.
_INSERT_QUESTIONNAIRE_SQL = sqlalchemy.text(
    "INSERT INTO questionnaire "
    "(id, assessment_id, title, status, current_question_index, provider_context) "
    "VALUES (:id, :assessment_id, :title, 'in_progress', 0, "
    " CAST(:provider_context AS JSONB))"
)

_QUESTION_BY_ID_SQL = sqlalchemy.text(
    "SELECT id, question_key, question_text, guidance, "
    "       requirement_key, requirement_title "
    "FROM assessment_question WHERE id = :question_id"
)

# timestamp is NOT NULL with no column default (verified against the live
# schema) and is stamped with clock_timestamp(), not now(): now() is
# transaction-start, and a chat session sends several messages across the
# same request/transaction — now() would stamp every message in a batch
# with the identical instant, destroying the ordering transcript() exists
# to return. clock_timestamp() records the moment of each individual write.
_INSERT_MESSAGE_SQL = sqlalchemy.text(
    "INSERT INTO chat_message "
    "(id, questionnaire_id, timestamp, sender_email, sender_display_name, "
    " text, is_bot_message, question_index) "
    "VALUES (:id, :questionnaire_id, clock_timestamp(), :sender_email, "
    " :sender_display_name, :text, :is_bot_message, :question_index)"
)

_TRANSCRIPT_SQL = sqlalchemy.text(
    "SELECT id, timestamp, sender_email, sender_display_name, text, "
    "       is_bot_message, question_index "
    "FROM chat_message WHERE questionnaire_id = :questionnaire_id "
    "ORDER BY timestamp, id"
)

_UPDATE_PROGRESS_SQL = sqlalchemy.text(
    "UPDATE questionnaire SET current_question_index = :idx WHERE id = :id"
)

# completed_at uses clock_timestamp(), not now() — now() is transaction-start
# and would stamp a completion time earlier than the work that just happened
# in this same transaction (record_answer's write_answer call above it). That
# exact bug was found and fixed in tasks.py's _SET_TASK_STATUS_SQL for the
# analogous privacy_assessment_task.updated_at; the same reasoning applies
# here verbatim.
_COMPLETE_QUESTIONNAIRE_SQL = sqlalchemy.text(
    "UPDATE questionnaire "
    "SET current_question_index = :idx, status = 'completed', "
    "    completed_at = clock_timestamp() "
    "WHERE id = :id"
)


def _lock_assessment_and_get_name(db: Session, assessment_id: str) -> str | None:
    """Lock the parent privacy_assessment row FOR UPDATE and return its
    name, or raise LookupError if the assessment does not exist.

    Called by open_or_resume and advance before any other read — see
    _LOCK_ASSESSMENT_SQL's comment for why. Re-acquiring the lock a second
    time in the same transaction does not block (already held by this
    transaction), matching write_answer's equivalent helper.
    """
    row = db.execute(_LOCK_ASSESSMENT_SQL, {"assessment_id": assessment_id}).first()
    if row is None:
        raise LookupError(f"No assessment with id {assessment_id}")
    return row[0]


def pending_question_ids(
    db: Session, assessment_id: str, include_question_ids: list[str] | None = None
) -> list[str]:
    """Question ids on this assessment's template whose current answer is
    not 'complete', in template order (group_order, requirement_key,
    question_order, id — the same ordering _QUESTION_SQL in assessments.py
    presents questions to a human in).

    When include_question_ids is given, the result is narrowed to that
    explicit subset (still template-ordered, still pending-only) — a
    caller choosing to run the session over a specific set of questions
    rather than every pending one. A question named in
    include_question_ids that is already 'complete' does not reappear:
    this function's whole contract is "still needs an answer", and an
    explicit subset does not override that.

    Read-only: does not lock the assessment row. It has no write to race
    against on its own — open_or_resume is what pairs a read of this with
    a write, and that function takes the lock.
    """
    rows = (
        db.execute(_PENDING_QUESTION_IDS_SQL, {"assessment_id": assessment_id})
        .scalars()
        .all()
    )
    if include_question_ids is not None:
        allowed = set(include_question_ids)
        return [qid for qid in rows if qid in allowed]
    return list(rows)


def open_or_resume(
    db: Session, assessment_id: str, include_question_ids: list[str] | None = None
) -> ChatSession:
    """Return the assessment's open questionnaire session, starting one if
    none exists.

    One open questionnaire per assessment (D-CHAT-5): two open sessions
    would race on current_question_index and file answers against the
    wrong questions. The parent assessment row is locked FOR UPDATE before
    the read-then-write below — the same read-then-write shape, and the
    same fix, as write_answer's races (see answers.py's fix-round-1
    comment): the lock serializes any two callers touching the same
    assessment_id, so the second one blocks here until the first commits
    and then re-reads the now-existing row, rather than both inserting
    their own questionnaire.

    A brand-new session's question list is computed once, from
    pending_question_ids, and stored in provider_context — never
    recomputed on resume, so a question answered mid-session does not
    silently vanish out from under the conversation that is already asking
    it.
    """
    name = _lock_assessment_and_get_name(db, assessment_id)

    existing = (
        db.execute(_FIND_OPEN_QUESTIONNAIRE_SQL, {"assessment_id": assessment_id})
        .mappings()
        .first()
    )
    if existing is not None:
        provider_context = existing["provider_context"] or {}
        return ChatSession(
            id=existing["id"],
            assessment_id=assessment_id,
            status=existing["status"],
            current_question_index=existing["current_question_index"],
            question_ids=list(provider_context.get("question_ids", [])),
        )

    question_ids = pending_question_ids(db, assessment_id, include_question_ids)
    questionnaire_id = f"qnr_{uuid.uuid4().hex[:8]}"
    db.execute(
        _INSERT_QUESTIONNAIRE_SQL,
        {
            "id": questionnaire_id,
            "assessment_id": assessment_id,
            "title": name or "Questionnaire",
            "provider_context": json.dumps({"question_ids": question_ids}),
        },
    )
    return ChatSession(
        id=questionnaire_id,
        assessment_id=assessment_id,
        status="in_progress",
        current_question_index=0,
        question_ids=question_ids,
    )


def current_question(db: Session, session: ChatSession) -> dict | None:
    """The question the session is currently on, or None once the session
    has moved past the last one in its list (including a completed
    session, whose current_question_index is always out of range by
    construction — see advance).
    """
    if session.current_question_index >= len(session.question_ids):
        return None
    question_id = session.question_ids[session.current_question_index]
    row = db.execute(_QUESTION_BY_ID_SQL, {"question_id": question_id}).mappings().first()
    return dict(row) if row is not None else None


def record_message(
    db: Session,
    questionnaire_id: str,
    text: str,
    *,
    is_bot: bool,
    sender_email: str | None = None,
    sender_display_name: str | None = None,
    question_index: int | None = None,
) -> None:
    """Append one chat_message row. Never commits — the caller's route
    owns the transaction, same convention as write_answer.
    """
    db.execute(
        _INSERT_MESSAGE_SQL,
        {
            "id": f"cm_{uuid.uuid4().hex[:8]}",
            "questionnaire_id": questionnaire_id,
            "sender_email": sender_email,
            "sender_display_name": sender_display_name,
            "text": text,
            "is_bot_message": is_bot,
            "question_index": question_index,
        },
    )


def transcript(db: Session, questionnaire_id: str) -> list[dict]:
    """Every message in this questionnaire, oldest first."""
    rows = db.execute(_TRANSCRIPT_SQL, {"questionnaire_id": questionnaire_id}).mappings().all()
    return [dict(row) for row in rows]


def record_answer(
    db: Session, session: ChatSession, answer_text: str, created_by: str
) -> None:
    """Write the officer's own words, verbatim, as the answer to the
    session's current question (D-CHAT-1, D-CHAT-3).

    Writes through write_answer — the only writer of answer_version — with
    answer_status="complete", answer_source="user_input",
    change_type="human_edited" always: a human just typed this in
    conversation, so it is filed exactly the way a human's edit through the
    detail screen is (write_answer's own defaults). Nothing about the text
    is altered; a `partial` machine draft offered for review and confirmed
    verbatim by the officer is recorded as their answer, not the
    generator's, because a human read it and stands behind it now.
    """
    if session.current_question_index >= len(session.question_ids):
        raise LookupError(
            f"questionnaire {session.id} has no current question to answer"
        )
    question_id = session.question_ids[session.current_question_index]
    write_answer(
        db,
        session.assessment_id,
        question_id,
        answer_text,
        created_by,
        answer_status="complete",
        answer_source="user_input",
        change_type="human_edited",
    )


def advance(db: Session, session: ChatSession) -> ChatSession:
    """Move the session to its next question, completing it once the last
    one is passed.

    Locks the parent assessment row FOR UPDATE first, same as
    open_or_resume — this is a read-then-write of the questionnaire row
    (session.current_question_index, read off the ChatSession passed in,
    then written), and two concurrent advances on the same assessment must
    not race on it any more than two concurrent open_or_resume calls may.

    completed_at is stamped with clock_timestamp(), not now() — see
    _COMPLETE_QUESTIONNAIRE_SQL's comment.
    """
    _lock_assessment_and_get_name(db, session.assessment_id)

    next_index = session.current_question_index + 1
    if next_index >= len(session.question_ids):
        db.execute(_COMPLETE_QUESTIONNAIRE_SQL, {"id": session.id, "idx": next_index})
        status = "completed"
    else:
        db.execute(_UPDATE_PROGRESS_SQL, {"id": session.id, "idx": next_index})
        status = session.status

    return ChatSession(
        id=session.id,
        assessment_id=session.assessment_id,
        status=status,
        current_question_index=next_index,
        question_ids=session.question_ids,
    )
