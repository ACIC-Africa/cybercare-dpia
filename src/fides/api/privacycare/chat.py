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
from dataclasses import dataclass

import sqlalchemy
from loguru import logger
from sqlalchemy.orm import Session

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

# Scoped to the assessment's CURRENT template, not a bare lookup by id.
#
# The session's question_ids are frozen at session-start (see open_or_resume)
# and the template underneath them can move — assessment_template is uniquely
# keyed on (assessment_type, version, revision) and AssessmentStatus carries
# an `outdated` value whose whole meaning is template drift. A frozen id can
# therefore end up naming a question that is no longer on the assessment's
# template, and write_answer's _require_question_in_template — which compares
# against exactly this join — would raise QuestionNotInTemplateError (a
# ValueError, so an uncaught 500 on the route, on every reply, forever).
# Resolving through the same predicate write_answer enforces means
# current_question returns None for that entry and skip_unresolvable_questions
# moves past it, instead of the route discovering the mismatch at write time.
_QUESTION_BY_ID_SQL = sqlalchemy.text(
    "SELECT q.id, q.question_key, q.question_text, q.guidance, "
    "       q.requirement_key, q.requirement_title "
    "FROM assessment_question q "
    "JOIN privacy_assessment pa ON pa.template_id = q.template_id "
    "WHERE q.id = :question_id AND pa.id = :assessment_id"
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

# The turn's ONE authoritative read of questionnaire progress, issued by
# begin_turn immediately after the parent assessment row is locked.
#
# There is deliberately no FOR UPDATE clause here, and the name no longer
# claims one. This statement is serialised by the privacy_assessment row
# lock begin_turn takes on the line above it (_lock_assessment_and_get_name
# — the same lock write_answer takes), not by a lock of its own. The
# previous name, _CURRENT_INDEX_FOR_UPDATE_SQL, asserted a row lock that was
# not in the text, and the whole-range review was right that a name lying
# about a concurrency guarantee is how the skip-a-question defect that
# begin_turn now closes got written in the first place.
#
# status is read here alongside the index, in the SAME statement: advance
# used to carry status off the handed-in ChatSession — a value read before
# any lock — while carefully re-reading the index. Two halves of one read
# disagreeing is exactly the defect begin_turn exists to remove, so both
# halves come from this one row.
_CURRENT_PROGRESS_SQL = sqlalchemy.text(
    "SELECT current_question_index, status FROM questionnaire WHERE id = :id"
)

# How many of THIS SESSION's questions are actually answered — a count, not
# the session's cursor position. Identical in definition to
# _COMPLETE_ANSWERS_SQL (api/answers.py's completeness numerator) and to
# answered_count (_question_group_response in api/assessments.py): an answer
# counts only when its question is on the assessment's current template AND
# its CURRENT version's answer_status is exactly 'complete'. The one
# addition is the question_id = ANY(...) narrowing to the session's frozen
# question list, because a session reports progress through its own
# questions, not the whole assessment's.
#
# ChatReplyResponse.answered_questions used to report
# current_question_index. A cursor equals a count only when nothing was ever
# skipped or re-asked out of order, and it already misreported with no
# concurrency at all: start's nothing-pending branch advances an empty
# question list, so the next reply announced "1 of 0" answered. The number a
# DPO reads as "how much of my DPIA is done" is counted from answer_version,
# never inferred from a pointer.
_ANSWERED_IN_SESSION_SQL = sqlalchemy.text(
    "SELECT COUNT(*) FROM assessment_answer a "
    "JOIN answer_version av ON av.id = a.current_version_id "
    "JOIN assessment_question q ON q.id = a.question_id "
    "JOIN privacy_assessment pa "
    "  ON pa.id = a.assessment_id AND pa.template_id = q.template_id "
    "WHERE a.assessment_id = :assessment_id "
    "  AND a.question_id = ANY(:question_ids) "
    "  AND av.answer_status = 'complete'"
)

# Has a bot turn for this question index already been written? A `start`
# against an already-open session must return the transcript it finds, not
# append a second copy of the question the officer is already looking at
# (and pay for a gateway completion to phrase it). The transcript is the
# audit artifact for a document filed with a regulator; a page refresh is a
# read and must not grow it.
_QUESTION_ASKED_SQL = sqlalchemy.text(
    "SELECT 1 FROM chat_message "
    "WHERE questionnaire_id = :questionnaire_id "
    "  AND is_bot_message "
    "  AND question_index = :question_index "
    "LIMIT 1"
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

    Called by open_or_resume, begin_turn and advance before any other read
    — see _LOCK_ASSESSMENT_SQL's comment for why. Re-acquiring the lock a second
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


def current_question_id(session: ChatSession) -> str | None:
    """The id the session's cursor currently names, or None once the cursor
    has moved past the last entry in the frozen list.

    Deliberately separate from current_question, and deliberately needs no
    database: "the cursor is past the end" and "this entry did not resolve
    to a question" are two different situations that current_question alone
    reports identically (both None), and a caller that cannot tell them
    apart routes an unresolvable entry into the session-is-complete branch
    — which files nothing, advances nothing, and tells the officer the
    questionnaire is finished forever while the row sits at in_progress.
    A caller holding both answers can say: id is None -> genuinely past the
    end, complete; id is not None but current_question is None -> this
    entry is bad, skip it and move on (skip_unresolvable_questions).
    """
    # Only the upper bound used to be guarded. questionnaire.
    # current_question_index carries no CHECK constraint — it is an Ethyca
    # table we do not alter — so a negative value from any other writer would
    # index the frozen list FROM THE END: the answer lands on the last
    # question and the cursor then resets to 0, re-walking the whole
    # questionnaire. Same class as the concurrency defect this module already
    # shipped once. Treating it as "nothing current" makes it visible and
    # inert rather than quietly wrong.
    if not 0 <= session.current_question_index < len(session.question_ids):
        return None
    return session.question_ids[session.current_question_index]


def current_question(db: Session, session: ChatSession) -> dict | None:
    """The question the session is currently on, or None.

    None means one of two things and the caller must distinguish them with
    current_question_id — see that function's docstring. Either the cursor
    is past the last entry in the list (including a completed session,
    whose current_question_index is always out of range by construction —
    see advance), or the entry at the cursor does not resolve to a question
    on this assessment's current template (deleted, or drifted off the
    template — see _QUESTION_BY_ID_SQL).
    """
    question_id = current_question_id(session)
    if question_id is None:
        return None
    row = (
        db.execute(
            _QUESTION_BY_ID_SQL,
            {"question_id": question_id, "assessment_id": session.assessment_id},
        )
        .mappings()
        .first()
    )
    return dict(row) if row is not None else None


def skip_unresolvable_questions(db: Session, session: ChatSession) -> ChatSession:
    """Advance past any question_ids entry that no longer resolves, logging
    each one, and return the session sitting on the first entry that does
    (or past the end, if none do).

    A session's question_ids are frozen at session-start and the questions
    underneath them are not: an entry can name a question since deleted, or
    one that has drifted off the assessment's current template, and
    provider_context is free-form JSONB on an Ethyca-authored table that
    this app is not the only thing that may ever write. Every one of those
    is a question the officer will never be asked — which is a defect in a
    legal document, but a recoverable one. Stalling the whole session on it
    is not: before this existed, an unresolvable entry made every reply
    answer "the questionnaire is now complete" while the row stayed
    in_progress at the same index, with no way forward through the route
    that owns it.

    Logged at WARNING, not swallowed: the officer keeps moving, and the
    operator gets the questionnaire id and the question id that vanished.

    Bounded by the length of the list — every iteration moves the cursor
    forward by one and the loop stops at the end of it.
    """
    while True:
        question_id = current_question_id(session)
        if question_id is None:
            return session
        if current_question(db, session) is not None:
            return session
        logger.warning(
            "PrivacyCare chat skipped question {} at index {} of questionnaire "
            "{}: it does not resolve to a question on assessment {}'s current "
            "template. The officer will not be asked it.",
            question_id,
            session.current_question_index,
            session.id,
            session.assessment_id,
        )
        session = advance(db, session, from_index=session.current_question_index)


def begin_turn(db: Session, session: ChatSession) -> ChatSession:
    """Lock the parent assessment FOR UPDATE and return `session` carrying
    the questionnaire's STORED index and status, re-read under that lock.

    This is the one authoritative read of a turn, and it is the fix for the
    defect the whole-range review found. `reply` used to read the index
    unlocked (_session_by_id), judge the officer's message against the
    question at that index, and only then take a lock — deep inside
    record_answer -> write_answer. So two in-flight replies both read index
    N; the first filed N and advanced to N+1; the second filed against its
    stale N and then advanced from the stored N+1 to N+2. Question N+1 was
    never asked, N carried two versions, and the session reported itself
    complete. Half of one read-then-write had been corrected and half left
    stale, which is worse than the luck it replaced.

    The lock is taken where the TURN begins, not where the increment
    happens. Everything downstream — which question is asked, which
    question the answer is filed against, and which index is advanced from
    — derives from the single row this reads, so the second reply blocks
    here, re-reads N+1, and answers question N+1.

    The lock is a plain Postgres row lock held until the caller's
    transaction ends, and re-acquiring one this transaction already holds
    does not block (see answers.py's _lock_assessment_and_get_template_id),
    so open_or_resume -> begin_turn -> write_answer in one request is three
    no-op re-acquisitions of the same lock, not three waits.
    """
    _lock_assessment_and_get_name(db, session.assessment_id)
    row = db.execute(_CURRENT_PROGRESS_SQL, {"id": session.id}).first()
    if row is None:
        raise LookupError(f"No questionnaire with id {session.id}")
    return ChatSession(
        id=session.id,
        assessment_id=session.assessment_id,
        status=row[1],
        current_question_index=row[0],
        question_ids=session.question_ids,
    )


def answered_count(db: Session, session: ChatSession) -> int:
    """How many of this session's questions actually have a complete answer.

    A COUNT over answer_version, not the session's cursor position — see
    _ANSWERED_IN_SESSION_SQL for why the two are not the same number and
    why this one is what a DPO is shown.
    """
    if not session.question_ids:
        return 0
    return (
        db.execute(
            _ANSWERED_IN_SESSION_SQL,
            {
                "assessment_id": session.assessment_id,
                "question_ids": list(session.question_ids),
            },
        ).scalar()
        or 0
    )


def question_already_asked(
    db: Session, questionnaire_id: str, question_index: int
) -> bool:
    """Has a bot turn already been written for this question index? See
    _QUESTION_ASKED_SQL.
    """
    return (
        db.execute(
            _QUESTION_ASKED_SQL,
            {"questionnaire_id": questionnaire_id, "question_index": question_index},
        ).first()
        is not None
    )


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
    db: Session,
    session: ChatSession,
    answer_text: str,
    created_by: str,
    *,
    question_index: int,
) -> None:
    """Write the officer's own words, verbatim, as the answer to the
    question at `question_index` (D-CHAT-1, D-CHAT-3).

    question_index is a REQUIRED keyword argument and is not defaulted from
    the session, on purpose. This function and advance are the two halves of
    one read-then-write; when each decided its own index they could — and
    did — disagree, filing an answer against one question and advancing from
    another, skipping a question in between. Neither half reads the stored
    index any more: begin_turn reads it once, under the lock, and hands the
    same value to both. record_answer_and_advance below is the intended way
    to call either of them.

    Writes through write_answer — the only writer of answer_version — with
    answer_status="complete", answer_source="user_input",
    change_type="human_edited" always: a human just typed this in
    conversation, so it is filed exactly the way a human's edit through the
    detail screen is (write_answer's own defaults). Nothing about the text
    is altered; a `partial` machine draft offered for review and confirmed
    verbatim by the officer is recorded as their answer, not the
    generator's, because a human read it and stands behind it now.
    """
    if question_index >= len(session.question_ids):
        raise LookupError(
            f"questionnaire {session.id} has no question at index "
            f"{question_index} to answer"
        )
    question_id = session.question_ids[question_index]
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


def advance(db: Session, session: ChatSession, *, from_index: int) -> ChatSession:
    """Move the session on from `from_index`, completing it once the last
    question is passed.

    from_index is a REQUIRED keyword argument — see record_answer's
    docstring for why neither half of the record-and-advance pair is
    allowed to decide the index for itself. begin_turn (or open_or_resume,
    which takes the same lock and reads the same row) is where that value
    comes from.

    The status carried into the non-completing branch comes off `session`,
    which for the same reason must be a session whose status was read under
    the lock — begin_turn and open_or_resume are the two functions that
    guarantee that, and every call site goes through one of them. advance
    used to re-read the index carefully and then take status off a
    handed-in object that could be arbitrarily stale, which is the same
    species of half-corrected read as the defect above.

    Re-acquires the parent assessment lock, which is a no-op when the
    caller already holds it and the only protection when (as in a bare
    skip_unresolvable_questions call) nothing else does.

    The new index is clamped to len(question_ids): one past the last entry
    is what current_question reads as "past the end", and nothing is served
    by storing a cursor further out than the list it indexes. An empty
    question list therefore completes at index 0 rather than at 1.

    completed_at is stamped with clock_timestamp(), not now() — see
    _COMPLETE_QUESTIONNAIRE_SQL's comment.
    """
    _lock_assessment_and_get_name(db, session.assessment_id)

    next_index = min(from_index + 1, len(session.question_ids))
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


def record_answer_and_advance(
    db: Session, session: ChatSession, answer_text: str, created_by: str
) -> ChatSession:
    """File the officer's answer and move the session on — one lock, one
    read, both writes.

    This is the whole point of the module's concurrency story, so it is one
    function rather than two calls a caller sequences: the index the answer
    is filed against and the index that is advanced from are the SAME
    value, read once by begin_turn under the parent-assessment lock. Two
    in-flight replies cannot straddle it — the second blocks in begin_turn
    until the first commits, then re-reads and answers the question the
    first advanced to.

    begin_turn is called here as well as by the route, deliberately. The
    route needs it earlier — before it decides which question to phrase and
    judge against — and repeating it here costs one no-op lock
    re-acquisition and one row read inside a transaction that already holds
    the lock, in exchange for this function being correct on its own terms
    rather than on a caller's discipline.
    """
    session = begin_turn(db, session)
    index = session.current_question_index
    record_answer(db, session, answer_text, created_by, question_index=index)
    return advance(db, session, from_index=index)
