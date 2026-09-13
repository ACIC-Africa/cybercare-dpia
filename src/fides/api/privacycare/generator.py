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
from dataclasses import dataclass, field
from datetime import datetime, timezone

import sqlalchemy
from loguru import logger
from sqlalchemy.orm import Session

from fides.api.privacycare.api.answers import write_answer
from fides.api.privacycare.context import UNSUPPORTED_SOURCE_ROOTS, resolve_source
from fides.api.privacycare.llm import DEFAULT_MODEL, GatewayUnavailable, complete

# Every generated answer_version's created_by. answer_version.created_by is
# free text, and a DPIA's whole value is that every answer names its author
# — so a machine-drafted answer names the machine, rather than borrowing the
# identity of whoever pressed Generate. A DPO reviewing the assessment can
# then tell at a glance which answers still need a human's eyes.
GENERATOR_AUTHOR = "privacycare-generator"

# Every expected_coverage value the policy below branches on. Named so it can
# be pinned against the shipped questions themselves: the column is a plain
# varchar with no check constraint, so a fourth level added upstream would
# fall through every branch and silently generate nothing for those questions,
# with no error and no log. See tests/privacycare/test_vocabularies.py.
HANDLED_COVERAGE_LEVELS = frozenset({"full", "partial", "none"})

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
    # The fides_sources this question asked for that the record could not
    # supply. Carried on the draft AND stored inside the evidence payload
    # (see _evidence_payload) so the gap travels with the answer rather
    # than living only in a log line.
    missing_data: list[str] = field(default_factory=list)


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


def _unresolved_sources(
    question: dict, resolved: list[tuple[str, str]]
) -> list[str]:
    """The source keys this question asked for that the record did not hold.

    Named separately from _resolved_sources because the ABSENCE is a fact a
    DPIA has to carry: a question the record answers half of must not be
    indistinguishable, in the artifact or in the completeness percentage,
    from one it answers in full.

    Sources on an UNSUPPORTED_SOURCE_ROOTS root are excluded. missing_data
    is rendered by AnswerStatusTags.tsx as data the answer "can be
    automatically derived if you populate" — which is true of a declaration
    field the customer left blank, and false of privacy_notice / policy /
    connection / privacy_experience / fides, which name Fides subsystems
    PrivacyCare phase 1 does not operate at all. Listing those would send a
    DPO off to populate records that would change nothing (25 of the 89
    `partial` questions cite at least one). What the customer CAN fix stays;
    what only we can fix is our backlog, not their to-do list, and
    unresolvable_roots() is where that gap is counted.
    """
    answered = {key for key, _ in resolved}
    return [
        source_key
        for source_key in question["fides_sources"] or []
        if source_key not in answered
        and source_key.split(".", 1)[0] not in UNSUPPORTED_SOURCE_ROOTS
    ]


def _evidence_payload(items: list[dict], missing: list[str]) -> dict:
    """The answer_version.evidence JSONB object.

    {"items": [...]} is the shape plan 05 already writes and
    _evidence_items_from_payload already reads. "missing_data" is a SIBLING
    key, added only when there is a gap, so an answer with none keeps the
    exact payload it had before and the '{}'-means-no-evidence convention in
    _EVIDENCE_SQL is untouched. It is a JSON key, not a column: the
    answer_version table is Ethyca's, and its evidence column is ours to
    fill. _question_response reads the key back into
    AssessmentQuestionResponse.missing_data, which the UI already declares
    and which was hardcoded [] until now.
    """
    payload: dict = {"items": items}
    if missing:
        payload["missing_data"] = list(missing)
    return payload


# The subject each fides_sources root speaks about, in the words a DPO uses.
#
# A source key is `root.field`, and the ROOT is the subject the fact belongs
# to. Labelling with the field alone made `system.name` and
# `privacy_declaration.name` both render as "Name", so a prompt read
# "- Name: Acme CRM / - Name: Marketing outreach" with nothing saying which
# was the system and which the processing activity. That collision is on the
# OPENING question of dpia_1_1, cpra_1_1 and cnil_1_1 — 7 of the 89 `partial`
# questions — and its failure shape is the worst available: the model
# attributes a fact to the wrong subject while the evidence items, which do
# keep source_key, still cite correctly. A wrong statement wearing a correct
# citation.
#
# Mapped rather than derived so the name is the one a DPO would use
# ("Processing activity", not "Privacy declaration"), and so a root added to
# assessment_question later cannot silently reintroduce the collision: an
# unmapped root falls back to the root itself, which still distinguishes it.
# Covers all nine roots live in assessment_question.fides_sources, including
# the five in UNSUPPORTED_SOURCE_ROOTS that resolve_source never answers
# today — labelling them costs nothing and means supporting one later is not
# also a labelling change.
_SOURCE_ROOT_LABELS = {
    "system": "System",
    "privacy_declaration": "Processing activity",
    "data_use": "Data use",
    "data_category": "Data category",
    "privacy_notice": "Privacy notice",
    "privacy_experience": "Privacy experience",
    "policy": "Policy",
    "connection": "Integration",
    "fides": "Fides configuration",
}


def _label(source_key: str) -> str:
    """'system.name' -> 'System name'; 'privacy_declaration.name' -> 'Processing activity name'.

    Two different source keys must never produce the same label: this text is
    both the model's only way to tell one subject from another in the
    `partial` prompt AND the answer text a regulator reads on the `full`
    path. test_no_shipped_question_has_two_sources_with_the_same_label holds
    that guarantee across every question in the database.
    """
    root, _, field = source_key.partition(".")
    prefix = _SOURCE_ROOT_LABELS.get(root, root.replace("_", " ").capitalize())
    if not field:
        return prefix
    return f"{prefix} {field.replace('_', ' ')}"


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

    A `full` question whose sources resolve only PARTLY is answered, but as
    "partial", not "complete", and the unresolved keys are recorded in the
    evidence payload's missing_data. Writing it "complete" used to make an
    answer built from half its sources indistinguishable from a finished one:
    the missing line was simply absent (a reader cannot tell "the record was
    silent" from "the question never asked"), missing_data was hardcoded []
    in the API response, and the answer counted toward the completeness
    percentage the DPO signs off. "partial" is excluded from completeness and
    answered_count (plan 03b), so only an all-sources-resolved answer now
    raises that number.
    """
    if question["expected_coverage"] != "full":
        return None

    resolved = _resolved_sources(question, context)
    if not resolved:
        return None

    missing = _unresolved_sources(question, resolved)
    lines = [f"{_label(key)}: {value}" for key, value in resolved]
    items = [
        _evidence_item(key, value, citation_start + offset)
        for offset, (key, value) in enumerate(resolved)
    ]
    return QuestionDraft(
        answer_text="\n".join(lines),
        answer_status="complete" if not missing else "partial",
        answer_source="system",
        evidence=_evidence_payload(items, missing),
        missing_data=missing,
    )


def _generation_prompt(question: dict, resolved: list[tuple[str, str]]) -> str:
    facts = "\n".join(f"- {_label(key)}: {value}" for key, value in resolved)
    guidance = question.get("guidance")
    guidance_block = f"\n\nGUIDANCE FOR THE ASSESSOR:\n{guidance}" if guidance else ""
    return (
        f"PROCESSING ACTIVITY RECORD:\n{facts}{guidance_block}\n\n"
        f"QUESTION:\n{question['question_text']}"
    )


# Trailing characters a model adds to a one-word reply. Stripped before the
# sentinel is compared so "NEEDS_INPUT." is the decline it plainly is.
_TRAILING_PUNCTUATION = " \t\r\n.!?:;,-–—…\"\'`)]}*"

# Both-ends decoration a model may wrap the sentinel in. Superset of
# _TRAILING_PUNCTUATION so a single strip() handles either side.
_SENTINEL_DECORATION = _TRAILING_PUNCTUATION + "*`'\"-–—[](){}<>#: \t"


def _is_decline(reply: str, sentinel: str = NEEDS_INPUT_SENTINEL) -> bool:
    """Did the model reply with its one sanctioned sentinel, decoration and all?

    `sentinel` defaults to NEEDS_INPUT_SENTINEL (this module's own use, in
    draft_with_llm below) but is a parameter, not a hardcoded name, so the
    same decoration-stripping can be shared rather than re-derived: this is
    the shape chat_llm.judge_reply reuses verbatim for NOT_ANSWERED, because
    a model that wraps ITS sentinel in "**...**" or a trailing full stop
    will do the same to any other one-word sentinel it is asked to return.

    The sentinel is the ONLY thing standing between the gateway and a filed
    DPIA answer: there is no post-hoc grounding check and no confidence
    gate. Exact equality after strip() was therefore too narrow — a reply of
    "NEEDS_INPUT." or "NEEDS_INPUT — the record does not state a retention
    period" fell through to the write and was stored as an
    answer_status="partial" answer whose entire text is the refusal,
    carrying real ai_analysis evidence citing genuine fides_sources. In an
    exported DPIA that reads as a cited, drafted answer.

    Two shapes count as a decline: the sentinel alone once decoration is
    removed from EITHER end, and a reply that BEGINS with the sentinel
    (whatever follows is the model explaining itself, which the prompt asked
    it not to do but which does not make the decline less of one).

    Deliberately NOT "contains the sentinel": an answer that genuinely
    reports a gap — "the record names no retention period, so this needs
    input from the DPO" — is a real answer and must be filed as one.
    Over-broadening here would throw away work the model did correctly. The
    boundary is checked rather than assumed: the character after the
    sentinel must not be alphanumeric, so a hypothetical NEEDS_INPUTS is not
    read as a decline with a stray S.
    """
    stripped = reply.strip()
    if not stripped:
        return True
    # Decoration is stripped from BOTH ends. Stripping only the right left
    # the same defect in mirror position: "**NEEDS_INPUT**", a quoted
    # "NEEDS_INPUT", a backticked one, and "- NEEDS_INPUT" in a bulleted
    # reply all fell through and were filed as cited answers whose entire
    # text is the refusal. Models decorate; the sentinel has to survive it.
    stripped = stripped.strip(_SENTINEL_DECORATION)
    if not stripped:
        return True
    # Shape 1: the sentinel alone, once decoration is removed.
    if stripped == sentinel:
        return True
    # Shape 2: the sentinel, then the model explaining itself. Only counts
    # when the sentinel ends where a word ends — see the docstring.
    if not stripped.startswith(sentinel):
        return False
    rest = stripped[len(sentinel) :]
    return not (rest[0].isalnum() or rest[0] == "_")


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
    if _is_decline(answer_text):
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

    # Same gap signal as the deterministic path: what the record could not
    # supply travels with the answer, so the UI's missing_data field says
    # which facts a human still has to bring.
    missing = _unresolved_sources(question, resolved)
    return QuestionDraft(
        answer_text=answer_text,
        answer_status="partial",
        answer_source="ai_analysis",
        evidence=_evidence_payload(items, missing),
        missing_data=missing,
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
