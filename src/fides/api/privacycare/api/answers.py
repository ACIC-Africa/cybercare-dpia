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
import uuid

import sqlalchemy
from pydantic import BaseModel
from sqlalchemy.orm import Session


class QuestionNotInTemplateError(ValueError):
    """Raised when a question does not belong to the assessment's template.

    Rule 4 (task brief): writing an answer for a foreign question is a
    client error, not a silent no-op. A naive UPDATE/INSERT scoped by a join
    across assessment_question<->privacy_assessment would just match zero
    rows for a foreign or nonexistent question_id — no error, no row, no
    trace. This module checks explicitly and raises instead.
    """


_ASSESSMENT_TEMPLATE_SQL = sqlalchemy.text(
    "SELECT template_id FROM privacy_assessment WHERE id = :assessment_id"
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

_INSERT_ANSWER_VERSION_SQL = sqlalchemy.text(
    "INSERT INTO answer_version "
    "(id, answer_id, version_number, answer_text, answer_status, "
    " answer_source, change_type, created_by) "
    "VALUES (:id, :answer_id, :version_number, :answer_text, :answer_status, "
    " :answer_source, :change_type, :created_by)"
)

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

_COMPLETE_ANSWERS_SQL = sqlalchemy.text(
    "SELECT COUNT(*) FROM assessment_answer a "
    "JOIN answer_version av ON av.id = a.current_version_id "
    "WHERE a.assessment_id = :assessment_id AND av.answer_status = 'complete'"
)

_UPDATE_COMPLETENESS_SQL = sqlalchemy.text(
    "UPDATE privacy_assessment SET completeness = :completeness "
    "WHERE id = :assessment_id"
)


class AnswerWriteResult(BaseModel):
    """What a write produced — enough for a caller (task 2's route) to build
    an API response without re-querying."""

    answer_id: str
    version_id: str
    version_number: int
    answer_text: str
    answer_status: str
    answer_source: str
    change_type: str
    created_by: str | None


def _require_question_in_template(
    db: Session, assessment_id: str, question_id: str
) -> None:
    assessment_row = db.execute(
        _ASSESSMENT_TEMPLATE_SQL, {"assessment_id": assessment_id}
    ).first()
    if assessment_row is None:
        raise LookupError(f"No assessment with id {assessment_id}")

    question_row = db.execute(
        _QUESTION_TEMPLATE_SQL, {"question_id": question_id}
    ).first()
    if question_row is None or question_row[0] != assessment_row[0]:
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
) -> AnswerWriteResult:
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
    """
    _require_question_in_template(db, assessment_id, question_id)

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
    answer_status = "complete"
    answer_source = "user_input"
    change_type = "human_edited"

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
        },
    )
    # Rule 1: repoint the handle at the new version. The row just inserted
    # above at `version_number - 1` (if any) is never touched — it stays
    # readable via answer_version.answer_id, just no longer "current".
    db.execute(
        _REPOINT_CURRENT_VERSION_SQL,
        {"version_id": version_id, "answer_id": answer_id},
    )

    return AnswerWriteResult(
        answer_id=answer_id,
        version_id=version_id,
        version_number=version_number,
        answer_text=answer_text,
        answer_status=answer_status,
        answer_source=answer_source,
        change_type=change_type,
        created_by=created_by,
    )


def recompute_completeness(db: Session, assessment_id: str) -> float:
    """Recompute privacy_assessment.completeness as
    (complete answers) / (total questions on the assessment's template),
    write it, and return it.

    Rule 3: this MUST use the same complete-only definition as
    answered_count in assessments.py's _question_group_response — see
    _COMPLETE_ANSWERS_SQL's comment above. If the two definitions ever
    diverge, the detail screen and this write's response tell a DPO two
    different numbers for the same assessment.
    """
    assessment_row = db.execute(
        _ASSESSMENT_TEMPLATE_SQL, {"assessment_id": assessment_id}
    ).first()
    if assessment_row is None:
        raise LookupError(f"No assessment with id {assessment_id}")

    total = db.execute(_TOTAL_QUESTIONS_SQL, {"assessment_id": assessment_id}).scalar()
    complete = db.execute(
        _COMPLETE_ANSWERS_SQL, {"assessment_id": assessment_id}
    ).scalar()
    completeness = (complete / total) if total else 0.0

    db.execute(
        _UPDATE_COMPLETENESS_SQL,
        {"completeness": completeness, "assessment_id": assessment_id},
    )
    return completeness
