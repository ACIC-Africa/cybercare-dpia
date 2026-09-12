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
from fides.api.privacycare.llm import DEFAULT_MODEL, GatewayUnavailable, complete

# Every generated answer_version's created_by. answer_version.created_by is
# free text, and a DPIA's whole value is that every answer names its author
# — so a machine-drafted answer names the machine, rather than borrowing the
# identity of whoever pressed Generate. A DPO reviewing the assessment can
# then tell at a glance which answers still need a human's eyes.
GENERATOR_AUTHOR = "privacycare-generator"

# The model is given one way to say "I cannot answer this from the record".
# Without it the only options are a guess or an empty string, and a guessed
# answer in a DPIA is worse than an unanswered question: the unanswered one
# gets routed to a human, the guess gets filed with the regulator.
NEEDS_INPUT_SENTINEL = "NEEDS_INPUT"

# Identifies this feature in the gateway's egress audit. PrivacyCare is
# itself a system processing personal data, so its own LLM traffic is
# evidence for its own ROPA entry.
GENERATION_CALLER = "dpia-generation"

_GENERATION_SYSTEM_PROMPT = f"""You are assisting a data protection officer who is \
completing a Data Protection Impact Assessment.

Answer the question using ONLY the facts in the PROCESSING ACTIVITY RECORD \
supplied with it. Those facts are the whole of what is known.

Rules:
- Never state a fact about this organisation that is not in the record.
- Do not speculate about the organisation's intentions, safeguards or practices.
- If the record does not contain enough information to answer, reply with \
exactly {NEEDS_INPUT_SENTINEL} and nothing else.
- Write two or three sentences of plain prose. No preamble, no headings, no \
bullet points, no restatement of the question.

This answer will be read by a regulator. An honest {NEEDS_INPUT_SENTINEL} is \
always better than a plausible guess."""


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


def _generation_prompt(question: dict, resolved: list[tuple[str, str]]) -> str:
    facts = "\n".join(f"- {_label(key)}: {value}" for key, value in resolved)
    guidance = question.get("guidance")
    guidance_block = f"\n\nGUIDANCE FOR THE ASSESSOR:\n{guidance}" if guidance else ""
    return (
        f"PROCESSING ACTIVITY RECORD:\n{facts}{guidance_block}\n\n"
        f"QUESTION:\n{question['question_text']}"
    )


def draft_with_llm(
    question: dict,
    context: dict,
    *,
    model: str | None,
    citation_start: int = 1,
) -> QuestionDraft | None:
    """Ask the model to draft a `partial`-coverage answer. None writes nothing.

    Returns None in four cases, all of which leave the question for a human:
      - the question is not `partial`;
      - no fides_source resolves, so there is nothing to ground an answer in
        and calling the model would spend budget to invite a hallucination;
      - the model declines with the sentinel, or returns nothing usable;
      - the gateway refuses or is unreachable.

    That last case is deliberately NOT retried against a provider SDK.
    fides.api.privacycare.llm exists so that every prompt is redacted before
    egress and audited after; a fallback path would be the exact bypass it
    was written to prevent. A DPIA platform that leaks personal data to a
    model is indefensible to the officer it is sold to, and an unanswered
    question is a recoverable outcome.

    answer_status is "partial", never "complete": the record supplied only
    part of the answer by the question's own expected_coverage, and a
    machine draft has not been seen by the DPO who signs the assessment.
    "partial" is excluded from completeness and answered_count (plan 03b),
    so a generated draft never makes an assessment look finished.
    """
    if question["expected_coverage"] != "partial":
        return None

    resolved = _resolved_sources(question, context)
    if not resolved:
        return None

    try:
        reply = complete(
            GENERATION_CALLER,
            [{"role": "user", "content": _generation_prompt(question, resolved)}],
            model=model or DEFAULT_MODEL,
            max_tokens=512,
            system=_GENERATION_SYSTEM_PROMPT,
        )
    except GatewayUnavailable as exc:
        logger.warning(
            "PrivacyCare generation left question {} unanswered: {}",
            question["question_key"],
            exc,
        )
        return None

    answer_text = (reply or "").strip()
    if not answer_text or answer_text == NEEDS_INPUT_SENTINEL:
        return None

    items = [
        _evidence_item(key, value, citation_start + offset)
        for offset, (key, value) in enumerate(resolved)
    ]
    for item in items:
        # The facts are still the record's; what the model contributed is
        # the prose. Typing the evidence "ai_analysis" is what tells a
        # reviewer this answer was drafted rather than read off.
        item["type"] = "ai_analysis"

    return QuestionDraft(
        answer_text=answer_text,
        answer_status="partial",
        answer_source="ai_analysis",
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

    use_llm gates the `partial` branch: when False, only the deterministic
    `full` branch runs and no call reaches the gateway. model is passed
    through to draft_with_llm.
    """
    questions = (
        db.execute(_QUESTIONS_FOR_ASSESSMENT_SQL, {"assessment_id": assessment_id})
        .mappings()
        .all()
    )

    written = 0
    next_citation = 1
    for question in questions:
        q = dict(question)
        draft = draft_from_context(q, context, citation_start=next_citation)
        if draft is None and use_llm:
            draft = draft_with_llm(q, context, model=model, citation_start=next_citation)
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
