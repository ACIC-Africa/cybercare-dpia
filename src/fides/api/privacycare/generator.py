"""The coverage policy: what each question's expected_coverage produces.

assessment_question ships two columns that together ARE Ethyca's generation
design, expressed in data rather than code:

  expected_coverage  how much of the answer the Fides record can supply
  fides_sources      the exact dotted paths that supply it

Live distribution across the 376 shipped questions: none 243, partial 89,
full 44. This module reads those two columns; it invents no policy of its
own.

  full     -> answer deterministically from the resolved sources.
              answer_source="system", answer_status="complete".
  partial  -> the record holds part of the answer. Ask the model, through
              the gateway, with the resolved facts as its only ground truth.
              answer_source="ai_analysis". (Task 4.)
  none     -> the record cannot answer it. Write NOTHING and leave it for a
              human; the detail route already renders an unanswered question
              as needs_input.
"""
from dataclasses import dataclass
from datetime import datetime, timezone

import sqlalchemy
from loguru import logger
from sqlalchemy.orm import Session

from fides.api.privacycare.api.answers import write_answer
from fides.api.privacycare.context import resolve_source

# Every generated answer_version's created_by. answer_version.created_by is
# free text, and a DPIA's whole value is that every answer names its author
# — so a machine-drafted answer names the machine, rather than borrowing the
# identity of whoever pressed Generate. A DPO reviewing the assessment can
# then tell at a glance which answers still need a human's eyes.
GENERATOR_AUTHOR = "privacycare-generator"


@dataclass(frozen=True)
class QuestionDraft:
    answer_text: str
    answer_status: str
    answer_source: str
    evidence: dict


_QUESTIONS_FOR_ASSESSMENT_SQL = sqlalchemy.text(
    "SELECT q.id, q.question_key, q.question_text, q.guidance, "
    "       q.expected_coverage, q.fides_sources "
    "FROM assessment_question q "
    "JOIN privacy_assessment pa ON pa.template_id = q.template_id "
    "WHERE pa.id = :assessment_id "
    "ORDER BY q.group_order, q.question_order, q.id"
)


def _evidence_item(
    source_key: str, value: str, citation_number: int
) -> dict:
    """One EvidenceItem, shaped to the schema the UI already reads.

    Fields and their optionality come from EvidenceItem in
    api/schemas.py, which was matched field-for-field against
    clients/admin-ui/src/features/privacy-assessments/types.ts in plan 03.
    citation_number is required-but-nullable there; we always supply it.
    """
    root = source_key.split(".", 1)[0]
    field_name = source_key.split(".", 1)[1] if "." in source_key else source_key
    return {
        "id": f"ev_{citation_number}_{source_key.replace('.', '_')}",
        "type": "system",
        "value": value,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "field_name": field_name,
        "source_key": source_key,
        "source_type": root,
        "citation_number": citation_number,
    }


def _resolved_sources(question: dict, context: dict) -> list[tuple[str, str]]:
    """(source_key, rendered_value) for every source that actually resolves.

    resolve_source returns None for an absent fact, and absence is the whole
    signal here: a source that does not resolve contributes neither text nor
    evidence, because a DPIA answer must not assert a fact the record does
    not hold.
    """
    pairs = []
    for source_key in question["fides_sources"] or []:
        value = resolve_source(context, source_key)
        if value is not None:
            pairs.append((source_key, value))
    return pairs


def _label(source_key: str) -> str:
    """'privacy_declaration.data_use' -> 'Data use'."""
    field = source_key.split(".", 1)[-1]
    return field.replace("_", " ").capitalize()


def draft_from_context(
    question: dict, context: dict, *, citation_start: int = 1
) -> QuestionDraft | None:
    """The deterministic half of the policy. Returns None to write nothing.

    Only `full` produces a draft here. `partial` returns None in this task —
    its LLM branch lands in the next one — and `none` returns None by
    design.

    A `full` question whose sources do not resolve also returns None.
    expected_coverage is a claim about what the record CAN supply; when the
    record turns out not to hold the fact, the honest outcome is an
    unanswered question, not a confident answer assembled from nothing.
    """
    if question["expected_coverage"] != "full":
        return None

    resolved = _resolved_sources(question, context)
    if not resolved:
        return None

    lines = [f"{_label(key)}: {value}" for key, value in resolved]
    items = [
        _evidence_item(key, value, citation_start + offset)
        for offset, (key, value) in enumerate(resolved)
    ]
    return QuestionDraft(
        answer_text="\n".join(lines),
        answer_status="complete",
        answer_source="system",
        evidence={"items": items},
    )


def answer_questions(
    db: Session,
    assessment_id: str,
    context: dict,
    *,
    use_llm: bool,
    model: str | None,
) -> int:
    """Draft and persist an answer for every question the record can answer.

    Returns the number of answers written. The caller owns the transaction —
    this function never commits, matching every other core in this package.

    Citation numbers run continuously across the whole assessment rather
    than restarting per question: they are rendered as [1], [2] … in the
    exported DPIA, and two answers both claiming [1] would make the report's
    references ambiguous.

    use_llm and model are unused here — this task implements only the
    deterministic `full`/`none` branches. They are declared now, not added
    later, because the `partial` branch (task 4) needs them and changing a
    public signature one task after every call site adopted it would mean
    editing every call site twice instead of once.
    """
    questions = (
        db.execute(_QUESTIONS_FOR_ASSESSMENT_SQL, {"assessment_id": assessment_id})
        .mappings()
        .all()
    )

    written = 0
    next_citation = 1
    for question in questions:
        draft = draft_from_context(dict(question), context, citation_start=next_citation)
        if draft is None:
            continue
        write_answer(
            db,
            assessment_id,
            question["id"],
            draft.answer_text,
            GENERATOR_AUTHOR,
            answer_status=draft.answer_status,
            answer_source=draft.answer_source,
            change_type="ai_generated",
            evidence=draft.evidence,
        )
        next_citation += len(draft.evidence["items"])
        written += 1

    logger.debug(
        "PrivacyCare generation drafted {} of {} questions for assessment {}",
        written,
        len(questions),
        assessment_id,
    )
    return written
