# The versioned answer-write core for the DPIA engine.
#
# `assessment_answer` is a stable handle per (assessment, question) and holds
# no content of its own (verified against the live schema — see
# fides/api/privacycare/api/assessments.py's own note on this). All content —
# text, status, source, confidence, evidence — lives on `answer_version`, one
# immutable row per edit. This module's whole job is: append a new
# answer_version, repoint the handle's current_version_id at it, and never
# touch a prior version's row. That append-only chain is the audit trail a
# DPIA regulator asks for; an UPDATE of an existing answer_version would
# destroy it.
#
# Same no-ORM-coupling convention as assessments.py: raw SQL via
# sqlalchemy.text(), not the Ethyca-authored ORM models in
# fides.api.models.privacy_assessment. IDs are generated here rather than
# relying on FidesBase.generate_uuid's Column default, because that default
# only fires on an ORM-level INSERT — a raw db.execute(text(...)) bypasses it
# — matching the "aa_"/"av_" prefix convention already used by
# tests/privacycare/test_api_assessments.py's _seed_answer_with_evidence.
import json
import uuid

import sqlalchemy
from sqlalchemy.orm import Session


class QuestionNotInTemplateError(ValueError):
    """Raised when a question does not belong to the assessment's template.

    Rule 4 (task brief): writing an answer for a foreign question is a
    client error, not a silent no-op. A naive UPDATE/INSERT scoped by a join
    across assessment_question<->privacy_assessment would just match zero
    rows for a foreign or nonexistent question_id — no error, no row, no
    trace. This module checks explicitly and raises instead.
    """


# Fix round 1 (coordinator review, MAJOR finding): write_answer used to be
# read-then-write in three unlocked places —
#   1. SELECT assessment_answer handle -> INSERT: two concurrent writes for
#      the same (assessment, question) could each see no handle and both
#      insert one, breaking the one-handle rule.
#   2. SELECT MAX(version_number) -> INSERT at max+1: two concurrent writes
#      to the same answer could read the same max and collide on the same
#      version_number, corrupting the audit trail's ordering.
#   3. recompute_completeness's read-then-write of
#      privacy_assessment.completeness: a classic lost update between two
#      concurrent recomputes.
# `assessment_answer`/`answer_version` are Ethyca-authored tables (per this
# module's own no-ORM-coupling convention above) — adding a unique
# constraint there would put this app's Alembic chain into their schema,
# exactly what fides.api.privacycare.migrations.include_object exists to
# keep separate. Instead, every write/recompute takes a FOR UPDATE row lock
# on the PARENT privacy_assessment row before any read. That serializes any
# two transactions touching the same assessment_id — the second blocks here
# until the first commits and releases the lock, then re-reads fresh state —
# which closes all three races at once without touching either table's
# schema.
_LOCK_ASSESSMENT_SQL = sqlalchemy.text(
    "SELECT template_id FROM privacy_assessment WHERE id = :assessment_id FOR UPDATE"
)

_QUESTION_TEMPLATE_SQL = sqlalchemy.text(
    "SELECT template_id FROM assessment_question WHERE id = :question_id"
)

_FIND_ANSWER_HANDLE_SQL = sqlalchemy.text(
    "SELECT id FROM assessment_answer "
    "WHERE assessment_id = :assessment_id AND question_id = :question_id"
)

_INSERT_ANSWER_HANDLE_SQL = sqlalchemy.text(
    "INSERT INTO assessment_answer (id, assessment_id, question_id) "
    "VALUES (:id, :assessment_id, :question_id)"
)

_MAX_VERSION_NUMBER_SQL = sqlalchemy.text(
    "SELECT COALESCE(MAX(version_number), 0) FROM answer_version "
    "WHERE answer_id = :answer_id"
)

# `evidence` is JSONB and a bound text parameter has no implicit cast to
# JSONB in Postgres, so the CAST is required — the same reason
# _seed_answer_with_evidence in test_api_assessments.py already casts it.
_INSERT_ANSWER_VERSION_SQL = sqlalchemy.text(
    "INSERT INTO answer_version "
    "(id, answer_id, version_number, answer_text, answer_status, "
    " answer_source, change_type, created_by, evidence) "
    "VALUES (:id, :answer_id, :version_number, :answer_text, :answer_status, "
    " :answer_source, :change_type, :created_by, CAST(:evidence AS JSONB))"
)

# The three PG enums answer_version's columns are typed against (verified
# against pg_enum on the live database). Validated here rather than left to
# Postgres because this function's newest caller is a Celery task: a
# DataError raised inside a worker surfaces as an opaque task failure with
# the bad value buried in a driver traceback, while a ValueError raised here
# names the parameter at the call site that chose it.
_ANSWER_STATUSES = frozenset({"complete", "partial", "needs_input"})
_ANSWER_SOURCES = frozenset({"system", "ai_analysis", "user_input", "team_input"})
_CHANGE_TYPES = frozenset({"ai_generated", "human_edited", "approved", "rejected"})

_REPOINT_CURRENT_VERSION_SQL = sqlalchemy.text(
    "UPDATE assessment_answer SET current_version_id = :version_id "
    "WHERE id = :answer_id"
)

# completeness's definition must be IDENTICAL to answered_count's in
# _question_group_response (fides/api/privacycare/api/assessments.py): an
# answer counts only when its CURRENT version's answer_status is exactly
# "complete". That definition was settled in plan 03b by reading
# AnswerStatusTags.tsx directly (see assessments.py's fix-round-2 comment on
# _question_group_response) — "partial" gets the same non-complete fallback
# tag AnswerStatusTags.tsx uses for "needs_input", both wrapped in copy that
# says the answer isn't final yet. If this query's definition of "complete"
# ever drifted from that one, the detail screen (answered_count) and this
# write's response (completeness) would tell a DPO two different numbers for
# the same assessment.
_TOTAL_QUESTIONS_SQL = sqlalchemy.text(
    "SELECT COUNT(*) FROM assessment_question q "
    "JOIN privacy_assessment pa ON pa.template_id = q.template_id "
    "WHERE pa.id = :assessment_id"
)

# Fix round 3 (whole-range review, MAJOR finding): this numerator used to
# be
#     SELECT COUNT(*) FROM assessment_answer a
#     JOIN answer_version av ON av.id = a.current_version_id
#     WHERE a.assessment_id = :assessment_id AND av.answer_status = 'complete'
# — identical to answered_count on the STATUS predicate, but not on the ROW
# SET. It never joined assessment_question and never mentioned template_id,
# while both _TOTAL_QUESTIONS_SQL (the denominator) above and _QUESTION_SQL
# (answered_count's source, in api/assessments.py) scope their rows to the
# assessment's CURRENT template. So any assessment_answer row whose question
# is not on that template counted in the numerator, contributed nothing to
# the denominator, and was invisible to answered_count — apples over
# oranges, and `completeness: float` is unbounded, so >100% was
# representable and would render on the detail screen next to "Fields: 0/8".
#
# That state is not hypothetical: assessment_template is uniquely keyed on
# (assessment_type, version, revision) and AssessmentStatus carries an
# `outdated` value whose whole meaning is template drift, so an assessment's
# template_id moving relative to its existing answers is a first-class
# concept here. The four routes in this module's range cannot create it
# (_require_question_in_template blocks every write), but the generation
# path writing against a prior template version lands in exactly this shape.
#
# The two JOINs below scope the numerator the same way the denominator
# already is: an answer counts only if its question is on the assessment's
# current template AND its CURRENT version's answer_status is exactly
# "complete". Pinned by test_recompute_completeness_ignores_answers_whose_
# question_is_not_on_the_template in tests/privacycare/test_answers.py.
_COMPLETE_ANSWERS_SQL = sqlalchemy.text(
    "SELECT COUNT(*) FROM assessment_answer a "
    "JOIN answer_version av ON av.id = a.current_version_id "
    "JOIN assessment_question q ON q.id = a.question_id "
    "JOIN privacy_assessment pa "
    "  ON pa.id = a.assessment_id AND pa.template_id = q.template_id "
    "WHERE a.assessment_id = :assessment_id AND av.answer_status = 'complete'"
)

_UPDATE_COMPLETENESS_SQL = sqlalchemy.text(
    "UPDATE privacy_assessment SET completeness = :completeness "
    "WHERE id = :assessment_id"
)


# Fix round 3 (whole-range review, finding 7): write_answer used to return an
# AnswerWriteResult model whose docstring said it existed so a caller "can
# build an API response without re-querying". It was DELETED rather than
# wired up, because no caller could ever have honoured that intent: both
# production callers build an AssessmentQuestionResponse, whose 14 fields
# (question_text, guidance, evidence, missing_data, sme_prompt, ...) come
# from assessment_question and are not knowable from a write to
# answer_version. _update_answer's _question_by_id re-query is therefore not
# a caller ignoring an available shortcut — it is the only way to produce
# the response, and AnswerWriteResult was a tested-but-unused production
# path documenting an intent the code could not follow.
#
# What it cost to remove: the tests that read `result.version_id` as a
# handle now read current_version_id back out of the database instead (see
# _current_version/_versions in tests/privacycare/test_answers.py), which is
# a stronger assertion for an append-only audit trail anyway — it pins what
# was PERSISTED rather than what the function reported.


def _lock_assessment_and_get_template_id(db: Session, assessment_id: str) -> str:
    """Lock the parent privacy_assessment row FOR UPDATE and return its
    template_id, or raise LookupError if the assessment does not exist.

    Called first, before any other read, by both write_answer and
    recompute_completeness — see _LOCK_ASSESSMENT_SQL's comment for why. The
    lock is a plain Postgres row lock: re-acquiring it a second time in the
    same transaction (e.g. write_answer, then recompute_completeness called
    right after in the same request) does not block — it's already held by
    this transaction — so it is safe for either function to call this
    unconditionally regardless of what the caller already holds.

    Raising here (rather than proceeding) matters as much as the lock
    itself: writing or recomputing against a nonexistent assessment_id would
    otherwise insert/update orphaned rows with nothing to ever read them
    back.
    """
    row = db.execute(_LOCK_ASSESSMENT_SQL, {"assessment_id": assessment_id}).first()
    if row is None:
        raise LookupError(f"No assessment with id {assessment_id}")
    return row[0]


def _require_question_in_template(
    db: Session, assessment_id: str, question_id: str, template_id: str
) -> None:
    question_row = db.execute(
        _QUESTION_TEMPLATE_SQL, {"question_id": question_id}
    ).first()
    if question_row is None or question_row[0] != template_id:
        raise QuestionNotInTemplateError(
            f"question {question_id} does not belong to assessment "
            f"{assessment_id}'s template"
        )


def write_answer(
    db: Session,
    assessment_id: str,
    question_id: str,
    answer_text: str,
    created_by: str | None,
    *,
    answer_status: str = "complete",
    answer_source: str = "user_input",
    change_type: str = "human_edited",
    evidence: dict | None = None,
) -> None:
    """Append a new answer_version for (assessment_id, question_id) and
    repoint the assessment_answer handle at it. Never overwrites a prior
    version — see the module docstring.

    answer_status decision (the one thing the request contract can't tell
    us — it carries only answer_text): read QuestionCard.tsx and
    AssessmentDetail.tsx. QuestionCard.tsx's handleSave posts
    `{ answer_text: newAnswer }` only — UpdateAnswerRequest
    (clients/admin-ui/.../types.ts) has no status field at all, and the
    component renders no status control (AnswerStatusTags there is a
    read-only Tag, never an input). AssessmentDetail.tsx never sets status
    either; the closest thing to a status signal it computes is
    `isComplete = allQuestions.every(q => q.answer_text.trim().length > 0)`
    — a text-presence check, not an answer_status read, and it drives the
    page-level "Completed"/"In progress" banner, not a per-answer status
    the save request could carry. So: the UI gives NO status control and NO
    inferrable per-answer status signal for a human-typed save. Per the
    brief's explicit fallback, this defaults to AnswerStatus.complete — "a
    person typed an answer" — rather than being derived from any UI
    behaviour. This is a DEFAULT, not a finding; pinned by
    test_write_answer_defaults_status_to_complete in
    tests/privacycare/test_answers.py.

    change_type/answer_source are NOT part of that decision — they are
    fixed by the task brief itself for a human-typed save: change_type is
    always "human_edited", answer_source is always "user_input".

    Returns nothing. See the fix-round-3 comment above AnswerWriteResult's
    former definition for why the result model was deleted rather than
    consumed: no caller could build its response from it.

    Concurrency (fix round 1): the very first thing this function does is
    lock the parent assessment row FOR UPDATE — see
    _lock_assessment_and_get_template_id — before the handle lookup, the
    version-number lookup, or any write below. The caller owns the
    transaction (this function never commits); the lock is held until the
    caller's transaction ends.

    The four keyword-only parameters exist for the generation path (plan
    05). Their DEFAULTS are the human-typed save this function was written
    for and must not change: plan 04 settled answer_status="complete" from
    QuestionCard.tsx (which posts only answer_text and renders no status
    control), and the brief fixed answer_source="user_input" /
    change_type="human_edited" for a person's edit. They are keyword-only
    so that no positional call site can supply one by accident, and so that
    reading any call tells you immediately whether it is a human save or a
    generated one.

    Generation writes through THIS function rather than its own INSERT so
    that a DPO correcting a machine draft appends version 2 of the same
    answer. One writer means the row lock, the one-handle rule and the
    version numbering cannot drift between the two paths.
    """
    for name, value, allowed in (
        ("answer_status", answer_status, _ANSWER_STATUSES),
        ("answer_source", answer_source, _ANSWER_SOURCES),
        ("change_type", change_type, _CHANGE_TYPES),
    ):
        if value not in allowed:
            raise ValueError(f"{name}={value!r} is not one of {sorted(allowed)}")

    template_id = _lock_assessment_and_get_template_id(db, assessment_id)
    _require_question_in_template(db, assessment_id, question_id, template_id)

    existing = db.execute(
        _FIND_ANSWER_HANDLE_SQL,
        {"assessment_id": assessment_id, "question_id": question_id},
    ).first()
    if existing is not None:
        answer_id = existing[0]
    else:
        # Rule 2: first write creates the one handle for this pair; every
        # later write (the `existing is not None` branch above) reuses it.
        answer_id = f"aa_{uuid.uuid4().hex[:8]}"
        db.execute(
            _INSERT_ANSWER_HANDLE_SQL,
            {
                "id": answer_id,
                "assessment_id": assessment_id,
                "question_id": question_id,
            },
        )

    max_version = db.execute(_MAX_VERSION_NUMBER_SQL, {"answer_id": answer_id}).scalar()
    version_number = (max_version or 0) + 1
    version_id = f"av_{uuid.uuid4().hex[:8]}"

    db.execute(
        _INSERT_ANSWER_VERSION_SQL,
        {
            "id": version_id,
            "answer_id": answer_id,
            "version_number": version_number,
            "answer_text": answer_text,
            "answer_status": answer_status,
            "answer_source": answer_source,
            "change_type": change_type,
            "created_by": created_by,
            "evidence": json.dumps(evidence or {}),
        },
    )
    # Rule 1: repoint the handle at the new version. The row just inserted
    # above at `version_number - 1` (if any) is never touched — it stays
    # readable via answer_version.answer_id, just no longer "current".
    db.execute(
        _REPOINT_CURRENT_VERSION_SQL,
        {"version_id": version_id, "answer_id": answer_id},
    )


def recompute_completeness(db: Session, assessment_id: str) -> float:
    """Recompute privacy_assessment.completeness as
    100 * (complete answers) / (total questions on the assessment's
    template), rounded to one decimal place, write it, and return it.

    UNIT (fix round 1, coordinator review, MAJOR finding — thank you to the
    reporter): this is a 0-100 PERCENTAGE, not a 0.0-1.0 fraction. It used to
    be the bare fraction, undetected because every test in this package
    that checked a value asserted against pytest.approx(<fraction>) rather
    than against the consumer. clients/admin-ui/src/features/
    privacy-assessments/AssessmentCard.tsx does `Math.round(completeness)}%`
    and feeds `percent={completeness}` to a progress bar — both already
    assume 0-100. Every fully-answered assessment was rendering "1%" (or,
    for partial completion, silently rounding to "0%") regardless of actual
    progress. `PrivacyAssessmentTask.progress` (fides/api/models/
    privacy_assessment.py) computes the analogous task-level number as
    `round((completed_count / total_count) * 100, 1)` — this mirrors that
    exactly, so the one Ethyca-authored percentage field already in this
    schema and this one use the same convention.

    Rule 3: this MUST use the same complete-only definition as
    answered_count in assessments.py's _question_group_response — see
    _COMPLETE_ANSWERS_SQL's comment above. If the two definitions ever
    diverge, the detail screen and this write's response tell a DPO two
    different numbers for the same assessment.

    Concurrency (fix round 1): this is a read-then-write of
    privacy_assessment.completeness on its own (race 3 in
    _LOCK_ASSESSMENT_SQL's comment), so it takes the same FOR UPDATE lock
    itself rather than trusting a caller to hold one. That makes this
    function correct both call shapes it's actually used in: called right
    after write_answer in the same transaction (re-acquiring a lock this
    transaction already holds — a no-op, not a block), and called on its
    own with no pre-existing lock (e.g. a standalone recompute/reconciliation
    pass), where this is the only thing protecting it.
    """
    _lock_assessment_and_get_template_id(db, assessment_id)

    total = db.execute(_TOTAL_QUESTIONS_SQL, {"assessment_id": assessment_id}).scalar()
    complete = db.execute(
        _COMPLETE_ANSWERS_SQL, {"assessment_id": assessment_id}
    ).scalar()
    completeness = round((complete / total) * 100, 1) if total else 0.0

    db.execute(
        _UPDATE_COMPLETENESS_SQL,
        {"completeness": completeness, "assessment_id": assessment_id},
    )
    return completeness
