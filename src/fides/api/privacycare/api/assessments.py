# Read endpoints for the DPIA engine, plus (task 2) the one write endpoint:
# PUT .../{assessment_id}/questions/{question_id}, which answers a single
# question. The write itself is delegated entirely to
# fides.api.privacycare.api.answers (task 1's versioned answer-write core,
# raw SQL, same no-ORM-coupling convention as this file) — this module's
# job for that route is HTTP shape only: auth, 404 mapping, and assembling
# the response envelope from what the write left behind.
import sqlalchemy
from fastapi import Depends, HTTPException, Security, status
from fastapi_pagination import Page, Params, paginate
from loguru import logger
from pydantic import ValidationError
from sqlalchemy.orm import Session

from fides.api.deps import get_db
from fides.api.models.client import ClientDetail
from fides.api.oauth.utils import verify_oauth_client
from fides.api.privacycare.api.answers import (
    QuestionNotInTemplateError,
    recompute_completeness,
    write_answer,
)
from fides.api.privacycare.api.identity import _created_by_from_client
from fides.api.privacycare.api.router import privacycare_router
from fides.api.privacycare.api.schemas import (
    AnswerUpdate,
    AssessmentEvidenceResponse,
    AssessmentGroupResponse,
    AssessmentMetadata,
    AssessmentQuestionResponse,
    AssessmentResponse,
    AssessmentSummaryResponse,
    BulkUpdateAnswersRequest,
    BulkUpdateAnswersResponse,
    DeletePrivacyAssessmentResponse,
    EvidenceItem,
    PrivacyAssessmentDetailResponse,
    QuestionGroup,
    TemplateResponse,
    UpdateAnswerRequest,
    UpdateAnswerResponse,
    UpdatePrivacyAssessmentRequest,
    template_key,
)
from fides.common.scope_registry import SYSTEM_READ

# Status values that make an assessment "open" work — matches AssessmentStatus
# in src/fides/api/models/privacy_assessment.py (Ethyca-authored; not imported
# here to avoid coupling this read-only module to that ORM setup). Mirrors the
# segmentForAssessment()/isOpen logic in
# clients/admin-ui/src/mocks/privacy-assessments/compute-summary.ts.
_OPEN_STATUSES = {"in_progress", "outdated"}
_UNCATEGORIZED_GROUP_KEY = "__uncategorized__"

_TEMPLATE_SQL = sqlalchemy.text(
    """
    SELECT id, version, name, assessment_type, region, authority,
           legal_reference, description, is_active
    FROM assessment_template
    ORDER BY name, version, id
    """
)

# No tenant/organisation filter below — by design, per spec decision D14
# (docs/superpowers/specs/2026-09-11-privacycare-design.md): "PrivacyCare
# deploys SINGLE-TENANT — one instance per client." This query is correct
# ONLY because the database backing it holds exactly one client's data.
# If PrivacyCare is ever deployed multi-tenant against a shared database,
# this query leaks every client's assessments to every other client —
# nothing in the SQL itself would stop it.
_ASSESSMENT_SQL = sqlalchemy.text(
    """
    SELECT pa.id, pa.template_id, t.name AS template_name, pa.name, pa.status,
           pa.completeness, pa.risk_level, pa.system_fides_key, pa.system_name,
           pa.declaration_id, pa.declaration_name, pa.data_use, pa.data_use_name,
           pa.data_categories, pa.created_by, pa.created_at, pa.updated_at
    FROM privacy_assessment pa
    LEFT JOIN assessment_template t ON t.id = pa.template_id
    ORDER BY pa.created_at DESC NULLS LAST, pa.id
    """
)


# No tenant/organisation filter below — same D14 basis as _ASSESSMENT_SQL
# above: correct only because this deployment is single-tenant (one
# PrivacyCare instance per client). A shared-database deployment would let
# this join return another client's questions/answers for any assessment id.
_QUESTION_SQL = sqlalchemy.text(
    """
    SELECT q.id, q.requirement_key, q.requirement_title,
           q.question_key, q.question_text, q.guidance, q.question_order,
           q.required, q.fides_sources, q.expected_coverage, a.id AS answer_id,
           av.answer_status, av.answer_text, av.answer_source, av.confidence,
           av.evidence, av.created_by AS answer_created_by,
           av.created_at AS answer_created_at, av.updated_at AS answer_updated_at
    FROM assessment_question q
    JOIN privacy_assessment pa ON pa.template_id = q.template_id
    LEFT JOIN assessment_answer a
           ON a.question_id = q.id AND a.assessment_id = pa.id
    LEFT JOIN answer_version av ON av.id = a.current_version_id
    WHERE pa.id = :assessment_id
    ORDER BY q.group_order, q.requirement_key, q.question_order, q.id
    """
)

# Joins `privacy_assessment` to its template (for assessment_type, which lives
# on assessment_template — not on privacy_assessment itself) and carries
# privacy_assessment_task_id through unjoined, since _metadata_for below needs
# the bare id to decide whether a generation task exists at all before
# querying privacy_assessment_task. No tenant/organisation filter — same D14
# basis as _ASSESSMENT_SQL above.
_ASSESSMENT_DETAIL_SQL = sqlalchemy.text(
    """
    SELECT pa.id, pa.template_id, t.name AS template_name, pa.name, pa.status,
           pa.completeness, pa.risk_level, pa.system_fides_key, pa.system_name,
           pa.declaration_id, pa.declaration_name, pa.data_use, pa.data_use_name,
           pa.data_categories, pa.created_by, pa.created_at, pa.updated_at,
           t.assessment_type, pa.privacy_assessment_task_id
    FROM privacy_assessment pa
    LEFT JOIN assessment_template t ON t.id = pa.template_id
    WHERE pa.id = :assessment_id
    """
)

# Used by update_answer (task 2) to read back the assessment's status after
# a write — UpdateAnswerResponse.status is the ASSESSMENT's status, not the
# answer's (see UpdateAnswerResponse's own docstring in schemas.py). No
# tenant/organisation filter — same D14 basis as _ASSESSMENT_SQL above.
_ASSESSMENT_STATUS_SQL = sqlalchemy.text(
    "SELECT status FROM privacy_assessment WHERE id = :assessment_id"
)

# AssessmentMetadata's three fields all live on privacy_assessment_task,
# reached via privacy_assessment.privacy_assessment_task_id (see the plan's
# field-by-field source table). Metadata is None when that id is null —
# handled by _metadata_for before this query ever runs.
_TASK_SQL = sqlalchemy.text(
    """
    SELECT created_at, use_llm, llm_model
    FROM privacy_assessment_task
    WHERE id = :task_id
    """
)

# `assessment_answer` carries no status/text/evidence of its own (verified
# against the live schema) — everything content-bearing lives on the
# `answer_version` a given answer's `current_version_id` points at, so every
# evidence read has to join through it. `evidence`/`source_references` are
# JSONB NOT NULL columns whose server_default is the empty object '{}';
# excluding that exact default is how we tell "no evidence recorded" apart
# from "evidence recorded but genuinely empty" without a nullability check
# the column doesn't offer.
#
# No tenant/organisation filter below either — same D14 basis as
# _ASSESSMENT_SQL/_QUESTION_SQL above: correct only because this deployment
# is single-tenant. If that ever changes, this is evidence — the exact
# regulatory record a DPIA audit exists to protect — leaking across clients.
_EVIDENCE_SQL = sqlalchemy.text(
    """
    SELECT a.question_id, av.evidence, av.source_references,
           av.answer_source, av.created_by, av.created_at, av.updated_at
    FROM assessment_answer a
    JOIN answer_version av ON av.id = a.current_version_id
    WHERE a.assessment_id = :assessment_id
      AND av.evidence IS NOT NULL
      AND av.evidence != '{}'::jsonb
    ORDER BY a.question_id, av.created_at
    """
)


def _list_assessments(db: Session):
    return db.execute(_ASSESSMENT_SQL).mappings().all()


def _questions_for(db: Session, assessment_id: str) -> list[dict]:
    groups: list[dict] = []
    index: dict = {}
    for row in db.execute(_QUESTION_SQL, {"assessment_id": assessment_id}).mappings():
        key = row["requirement_key"]
        if key not in index:
            index[key] = {
                "requirement_key": key,
                "requirement_title": row["requirement_title"],
                # No "group_order" here (fix round 2): it was carried into
                # this dict and never read — q.group_order already does its
                # one job in _QUESTION_SQL's ORDER BY, which orders `groups`
                # correctly before this loop ever runs (a column need not be
                # SELECTed to be used in ORDER BY).
                "questions": [],
            }
            groups.append(index[key])
        index[key]["questions"].append(
            {
                "id": row["id"],
                "question_key": row["question_key"],
                "question_text": row["question_text"],
                "guidance": row["guidance"],
                "question_order": row["question_order"],
                "required": row["required"],
                "fides_sources": row["fides_sources"],
                "expected_coverage": row["expected_coverage"],
                "answer_id": row["answer_id"],
                "answer_status": row["answer_status"],
                "answer_text": row["answer_text"],
                "answer_source": row["answer_source"],
                "confidence": row["confidence"],
                "evidence": row["evidence"],
                "answer_created_by": row["answer_created_by"],
                "answer_created_at": row["answer_created_at"],
                "answer_updated_at": row["answer_updated_at"],
            }
        )
    return groups


def _question_by_id(db: Session, assessment_id: str, question_id: str) -> dict | None:
    """Find one question (in its raw, _question_response-shaped dict form)
    within an assessment, or None if it isn't there.

    Deliberately reuses _questions_for rather than adding a second,
    near-duplicate SQL query filtered by question_id: _questions_for's
    query is already the single source of truth for this row shape (every
    key _question_response reads), so a hand-rolled twin here could drift
    from it silently the next time that query's column list changes. This
    dataset (one template's questions) is small enough that scanning it in
    Python costs nothing worth trading that safety for.
    """
    for group in _questions_for(db, assessment_id):
        for q in group["questions"]:
            if q["id"] == question_id:
                return q
    return None


def _as_str(value):
    return value.isoformat() if hasattr(value, "isoformat") else value


def _evidence_items_from_payload(
    payload,
    *,
    assessment_id: str,
    question_id: str,
    created_at=None,
    updated_at=None,
) -> list:
    # Shared by BOTH evidence-shaping paths (fix round 2 finding): this file
    # used to validate the `/evidence` endpoint's payload against EvidenceItem
    # (via this function's predecessor) while `_question_response` injected
    # the exact same raw `answer_version.evidence` JSONB unvalidated, typed
    # `List[dict]`. AssessmentDetail.tsx feeds the evidence drawer from the
    # detail response's per-question `evidence` field, and
    # EvidenceCardGroup.tsx calls `item.field_name!.replace(...)` on what it
    # gets — a payload missing a field EvidenceItem requires reached the
    # browser as a runtime crash instead of being caught here. Both paths now
    # call this one function, so a malformed payload is skipped identically
    # (same logged warning) no matter which path it arrived on.
    #
    # Fix round 1 (task 3, coordinator ruling): this function used to treat
    # the whole payload as ONE EvidenceItem, on the theory that the column's
    # '{}' default implied one-object-per-answer_version. That theory was an
    # inference from a column default with zero observed data behind it, and
    # plan 05's generator supplies the fact that breaks it: a single
    # `full`-coverage answer legitimately cites two or more fides_sources
    # paths (e.g. privacy_declaration.data_use AND data_use.name), and one
    # EvidenceItem per version cannot represent that. The generator writes
    # {"items": [...]} — still a JSON *object*, so '{}' (no "items" key)
    # keeps meaning "no evidence recorded", and the existing
    # `av.evidence != '{}'::jsonb` exclusion in _EVIDENCE_SQL is untouched.
    # A payload with no "items" key is still accepted as a single bare
    # EvidenceItem-shaped object — the only shape ever actually written
    # before this task — so a hand-written or legacy row is not silently
    # dropped.
    #
    # Each candidate item is validated independently by
    # _validated_evidence_item: one bad item is skipped (logged), never
    # discarding its siblings or failing the whole endpoint.
    if not isinstance(payload, dict):
        logger.warning(
            "Skipped evidence for assessment {} question {}: evidence "
            "was not a JSON object (got {})",
            assessment_id,
            question_id,
            type(payload).__name__,
        )
        return []

    if "items" in payload:
        candidates = payload["items"]
        if not isinstance(candidates, list):
            logger.warning(
                "Skipped evidence for assessment {} question {}: "
                "'items' was not a JSON array (got {})",
                assessment_id,
                question_id,
                type(candidates).__name__,
            )
            return []
    else:
        candidates = [payload]

    items = []
    for candidate in candidates:
        item = _validated_evidence_item(
            candidate,
            assessment_id=assessment_id,
            question_id=question_id,
            created_at=created_at,
            updated_at=updated_at,
        )
        if item is not None:
            items.append(item)
    return items


def _validated_evidence_item(
    candidate,
    *,
    assessment_id: str,
    question_id: str,
    created_at=None,
    updated_at=None,
) -> "EvidenceItem | None":
    # One candidate object -> one validated EvidenceItem, or None (logged).
    # Split out of _evidence_items_from_payload so that function's job is
    # purely "find the candidates" and this one's is purely "validate one".
    if not isinstance(candidate, dict):
        logger.warning(
            "Skipped evidence item for assessment {} question {}: item "
            "was not a JSON object (got {})",
            assessment_id,
            question_id,
            type(candidate).__name__,
        )
        return None
    missing_fields = [f for f in ("id", "type") if f not in candidate]
    if missing_fields:
        logger.warning(
            "Skipped evidence for assessment {} question {}: missing "
            "required field(s) {}",
            assessment_id,
            question_id,
            missing_fields,
        )
        return None
    try:
        return EvidenceItem(
            id=candidate["id"],
            type=candidate["type"],
            value=candidate.get("value"),
            created_at=candidate.get("created_at")
            or _as_str(created_at)
            or _as_str(updated_at),
            field_name=candidate.get("field_name"),
            source_key=candidate.get("source_key"),
            source_type=candidate.get("source_type"),
            citation_number=candidate.get("citation_number"),
            data=candidate.get("data"),
        )
    except ValidationError as exc:
        logger.warning(
            "Skipped evidence for assessment {} question {}: failed "
            "EvidenceItem validation ({})",
            assessment_id,
            question_id,
            exc,
        )
        return None


def _evidence_items_for(db: Session, assessment_id: str) -> list[dict]:
    items: list[dict] = []
    for row in db.execute(_EVIDENCE_SQL, {"assessment_id": assessment_id}).mappings():
        # Fix round 1 (task 3): one row can now yield MULTIPLE evidence
        # items — a {"items": [...]} payload citing two or more
        # fides_sources paths — not just one.
        for item in _evidence_items_from_payload(
            row["evidence"],
            assessment_id=assessment_id,
            question_id=row["question_id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        ):
            # Returned as a dict, not the EvidenceItem instance itself: this
            # function's callers (_evidence_for -> AssessmentEvidenceResponse)
            # and the tests exercising it treat `items` as plain mappings.
            # The validation this function exists to guarantee already
            # happened in _validated_evidence_item above.
            items.append(item.model_dump())
    return items


def _evidence_for(db: Session, assessment_id: str) -> dict:
    items = _evidence_items_for(db, assessment_id)
    return {
        "assessment_id": assessment_id,
        "total_count": len(items),
        "items": items,
    }


def _question_response(q: dict, assessment_id: str) -> AssessmentQuestionResponse:
    # THE NAMING TRAP: `id` is the display label QuestionCard.tsx:43 renders
    # ("{question.id}. {question.question_text}"), sourced from
    # assessment_question.question_key. `question_id` is the real identifier
    # QuestionCard.tsx:26 passes as questionId, sourced from
    # assessment_question.id. Do not swap them.
    #
    # answer_status/answer_source/answer_text are required, non-nullable
    # fields in the TS contract, but a question with no assessment_answer row
    # yet has no answer_version to join — av.* comes back None from the SQL's
    # LEFT JOIN. "needs_input"/"system" are not invented values: they are the
    # exact column defaults AnswerVersion.answer_status/answer_source declare
    # (fides.api.models.privacy_assessment, not imported here per this file's
    # existing no-ORM-coupling convention) — the same defaults a real row
    # would carry before anyone touched it. answer_text has no column
    # default, so "" is the empty-string analogue for a required str field.
    #
    # evidence: List[EvidenceItem], required (fix round 2: was List[dict],
    # unvalidated — see _evidence_items_from_payload above). answer_version.
    # evidence is a single JSONB *object* (verified against the live schema),
    # holding a {"items": [...]} container (fix round 1, task 3 — see that
    # function's docstring for why a single-EvidenceItem-per-version shape
    # was wrong), with server_default '{}' meaning "answered, no evidence
    # attached" — same exact-default exclusion already used in
    # _evidence_items_for. An unanswered question (no av row at all) has
    # evidence=None. Both cases collapse to the empty list; a genuinely
    # populated, valid container yields every item it validly holds — one
    # or many; an invalid item is dropped by the shared normaliser, loudly
    # (logged), not silently, without discarding its siblings.
    evidence = q["evidence"]
    items = (
        _evidence_items_from_payload(
            evidence,
            assessment_id=assessment_id,
            question_id=q["id"],
            created_at=q["answer_created_at"],
            updated_at=q["answer_updated_at"],
        )
        if evidence
        else []
    )
    return AssessmentQuestionResponse(
        id=q["question_key"],
        question_id=q["id"],
        question_text=q["question_text"],
        guidance=q["guidance"],
        required=q["required"],
        fides_sources=list(q["fides_sources"] or []),
        expected_coverage=q["expected_coverage"],
        answer_text=q["answer_text"] or "",
        answer_status=q["answer_status"] or "needs_input",
        answer_source=q["answer_source"] or "system",
        confidence=q["confidence"],
        evidence=items,
        # No database source (Plus computes these) — empty forms, not
        # omitted. See the plan's field-by-field source table.
        missing_data=[],
        sme_prompt=None,
    )


def _question_group_response(group: dict, assessment_id: str) -> QuestionGroup:
    questions = [_question_response(q, assessment_id) for q in group["questions"]]
    # Definition, checked against the UI rather than assumed (fix round 1,
    # narrowed further in fix round 2): answered_count/total_count is a
    # COMPLETION-PROGRESS indicator, not a "has this been touched" tally.
    # QuestionGroupPanel.tsx renders `isGroupCompleted = answeredCount ===
    # totalCount` to switch a group's tag between the literal strings
    # "Completed" and "Pending", and shows the raw fraction as "Fields:
    # {answeredCount}/{totalCount}".
    #
    # Fix round 1 correctly excluded "needs_input" (AssessmentDetail.tsx
    # filters that exact status into `needsInputIds`, the set still awaiting
    # a person) but left "partial" counted as answered. Fix round 2: read
    # AnswerStatusTags.tsx, the component that actually renders a question's
    # status. It gives COMPLETE its own branch — a plain source-label tag,
    # no tooltip, no caveat. PARTIAL is rendered by the *fallback* branch
    # (same code path as NEEDS_INPUT would take if it weren't COMPLETE)
    # wrapped in a Tooltip whose text is "This answer can be automatically
    # derived if you populate: ..." or "...if the relevant field is
    # populated" — i.e. the UI's own copy says a partial answer is NOT yet
    # what it should be. Two "partial" questions in a group of two must not
    # read 2/2 under a "Completed" tag while both cards inside still show
    # that tooltip: that is the exact overstatement-of-completeness failure
    # mode fix round 1 existed to prevent, just for a different status value.
    # So: "answered" means the LEFT JOIN to answer_version resolved AND that
    # answer's status is exactly "complete" — "partial", "needs_input", and
    # "no answer at all" do not count.
    answered_count = sum(
        1 for q in group["questions"] if q["answer_status"] == "complete"
    )
    # last_updated_at/last_updated_by (fix round 2): these DO have a source —
    # answer_version.updated_at/.created_by, already selected by _QUESTION_SQL
    # as answer_updated_at/answer_created_by. Derive both from whichever
    # question in the group has the most recently updated current answer
    # version; a group with no answered questions at all has neither.
    latest_answer = max(
        (q for q in group["questions"] if q["answer_updated_at"] is not None),
        key=lambda q: q["answer_updated_at"],
        default=None,
    )
    return QuestionGroup(
        # QuestionGroup.id and .requirement_key are the same value per the
        # plan's field table — both source from assessment_question.requirement_key.
        id=group["requirement_key"],
        title=group["requirement_title"],
        requirement_key=group["requirement_key"],
        questions=questions,
        answered_count=answered_count,
        total_count=len(questions),
        # No source in the OSS schema — always null for now.
        risk_level=None,
        last_updated_at=_as_str(latest_answer["answer_updated_at"])
        if latest_answer
        else None,
        last_updated_by=latest_answer["answer_created_by"] if latest_answer else None,
    )


def _metadata_for(db: Session, task_id: str | None) -> AssessmentMetadata | None:
    if task_id is None:
        return None
    row = db.execute(_TASK_SQL, {"task_id": task_id}).mappings().first()
    if row is None:
        # A dangling privacy_assessment_task_id (task row deleted after the
        # assessment pointed at it) is treated the same as "no task" rather
        # than raising — this endpoint's job is the assessment, not enforcing
        # the task table's referential integrity.
        return None
    if row["created_at"] is None:
        # Fix round 2 (500 risk): AssessmentMetadata.generation_timestamp is
        # `string` in the TS contract — required AND non-nullable, no `?`,
        # no `| null` (features/privacy-assessments/types.ts). Making it
        # Optional here would fix the crash but break that contract (and
        # test_assessment_metadata_optionality_matches_the_shipped_contract),
        # so it stays required. privacy_assessment_task.created_at IS
        # DB-nullable (verified against the live migration) even though
        # every normal write path server_defaults it to now() — a task row
        # that genuinely carries no created_at has no generation event to
        # report, so it is treated the same as "no task" (same precedent as
        # the dangling-id case above) rather than raising on serialisation
        # or inventing a timestamp value.
        logger.warning(
            "privacy_assessment_task {} has a null created_at; omitting "
            "metadata rather than violating AssessmentMetadata's required "
            "generation_timestamp",
            task_id,
        )
        return None
    return AssessmentMetadata(
        generation_timestamp=_as_str(row["created_at"]),
        model_used=row["llm_model"],
        use_llm=row["use_llm"],
    )


def _assessment_detail(
    db: Session, assessment_id: str
) -> PrivacyAssessmentDetailResponse:
    row = (
        db.execute(_ASSESSMENT_DETAIL_SQL, {"assessment_id": assessment_id})
        .mappings()
        .first()
    )
    if row is None:
        raise LookupError(f"No assessment with id {assessment_id}")
    question_groups = [
        _question_group_response(g, assessment_id)
        for g in _questions_for(db, assessment_id)
    ]
    return PrivacyAssessmentDetailResponse(
        id=row["id"],
        template_id=row["template_id"],
        template_name=row["template_name"],
        name=row["name"],
        status=row["status"],
        completeness=row["completeness"],
        risk_level=row["risk_level"],
        system_fides_key=row["system_fides_key"],
        system_name=row["system_name"],
        declaration_id=row["declaration_id"],
        declaration_name=row["declaration_name"],
        data_use=row["data_use"],
        data_use_name=row["data_use_name"],
        data_categories=list(row["data_categories"] or []),
        created_by=row["created_by"],
        created_at=_as_str(row["created_at"]),
        updated_at=_as_str(row["updated_at"]),
        assessment_type=row["assessment_type"],
        question_groups=question_groups,
        # The questionnaire is a commercial chat feature with no OSS table.
        questionnaire=None,
        metadata=_metadata_for(db, row["privacy_assessment_task_id"]),
    )


def _list_templates(db: Session) -> list[TemplateResponse]:
    return [
        TemplateResponse(
            id=r["id"],
            key=template_key(
                r["name"], r["id"]
            ),  # id fallback: never emit an empty key
            version=r["version"],
            name=r["name"],
            assessment_type=r["assessment_type"],
            region=r["region"],
            authority=r["authority"],
            legal_reference=r["legal_reference"],
            description=r["description"],
            is_active=r["is_active"],
        )
        for r in db.execute(_TEMPLATE_SQL).mappings().all()
    ]


def _segment_for_row(row) -> str:
    # Port of segmentForAssessment() in compute-summary.ts. status values are
    # AssessmentStatus ("in_progress" | "completed" | "outdated" |
    # "generating"); risk_level values are RiskLevel ("high" | "medium" |
    # "low") — both verified against src/fides/api/models/privacy_assessment.py.
    status_value = row["status"]
    if status_value == "completed":
        return "completed"
    if status_value == "generating":
        return "pending"
    if status_value in _OPEN_STATUSES:
        return "risk" if row["risk_level"] == "high" else "open"
    # Unexpected status value: the TS switch is exhaustive over the 4-member
    # enum and would fail to compile on a 5th; there is no such compile-time
    # guarantee here, so fall back to "open" rather than raising in
    # production on a value this module doesn't recognise.
    return "open"


def _summary(db: Session) -> dict:
    # Port of computeSummary() in
    # clients/admin-ui/src/mocks/privacy-assessments/compute-summary.ts — the
    # shipped reference implementation for AssessmentSummaryResponse. Fix
    # round 1: the original draft invented a {total, by_status,
    # by_risk_level} shape instead of reading this contract; every field
    # below (total, by_segment, blocked_groups, owners) is derivable from
    # columns _ASSESSMENT_SQL already selects (status, risk_level, data_use,
    # data_use_name, created_by), so nothing here is a stand-in.
    rows = _list_assessments(db)
    by_segment = {"completed": 0, "pending": 0, "open": 0, "risk": 0}
    groups: dict = {}
    owners: dict = {}
    total = 0

    for row in rows:
        total += 1
        by_segment[_segment_for_row(row)] += 1

        group_key = row["data_use"] or _UNCATEGORIZED_GROUP_KEY
        group = groups.setdefault(
            group_key,
            {
                "name": row["data_use_name"] or "Uncategorized",
                "outdated_count": 0,
                "high_risk_count": 0,
                "total_count": 0,
            },
        )
        group["total_count"] += 1
        if row["risk_level"] == "high":
            group["high_risk_count"] += 1
        is_outdated = row["status"] == "outdated"
        if is_outdated:
            group["outdated_count"] += 1

        if row["status"] in _OPEN_STATUSES and row["created_by"]:
            owner = owners.setdefault(
                row["created_by"],
                {
                    "owner": row["created_by"],
                    "open_count": 0,
                    "outdated_count": 0,
                },
            )
            owner["open_count"] += 1
            if is_outdated:
                owner["outdated_count"] += 1

    blocked_groups = [
        g
        for g in groups.values()
        if g["outdated_count"] > 0 or g["high_risk_count"] > 0
    ]
    blocked_groups.sort(
        key=lambda g: g["outdated_count"] + g["high_risk_count"], reverse=True
    )
    owners_list = sorted(owners.values(), key=lambda o: o["open_count"], reverse=True)

    return {
        "total": total,
        "by_segment": by_segment,
        "blocked_groups": blocked_groups,
        "owners": owners_list,
    }


def _assessment_to_response(row) -> AssessmentResponse:
    return AssessmentResponse(
        id=row["id"],
        template_id=row["template_id"],
        template_name=row["template_name"],
        name=row["name"],
        status=row["status"],
        completeness=row["completeness"],
        risk_level=row["risk_level"],
        system_fides_key=row["system_fides_key"],
        system_name=row["system_name"],
        declaration_id=row["declaration_id"],
        declaration_name=row["declaration_name"],
        data_use=row["data_use"],
        data_use_name=row["data_use_name"],
        data_categories=list(row["data_categories"] or []),
        created_by=row["created_by"],
        created_at=_as_str(row["created_at"]),
        updated_at=_as_str(row["updated_at"]),
    )


def _grouped_assessments(
    db: Session, status: str | None = None
) -> list[AssessmentGroupResponse]:
    # Groups the client's assessments by data_use, as the list page renders them.
    # Order is deterministic: named data uses alphabetically, the null group last.
    # `status` filters against privacy_assessment.status (AssessmentStatus in
    # features/privacy-assessments/types.ts: in_progress, completed, outdated,
    # generating — confirmed against that file and the assessmentstatus
    # Postgres enum, xx_2026_02_05_..._add_privacy_assessment_schema.py +
    # xx_2026_05_04_..._add_generating_to_assessmentstatus.py). Filtering in
    # Python against already-fetched rows means an unrecognised value simply
    # matches nothing — it comes back as an empty result, not an error.
    rows = _list_assessments(db)
    if status is not None:
        rows = [r for r in rows if r["status"] == status]
    groups: dict = {}
    for row in rows:
        key = row["data_use"]
        if key not in groups:
            groups[key] = {
                "data_use": key,
                "data_use_name": row["data_use_name"],
                "systems": set(),
                "assessments": [],
            }
        groups[key]["assessments"].append(_assessment_to_response(row))
        if row["system_fides_key"]:
            groups[key]["systems"].add(row["system_fides_key"])
    ordered = sorted(groups.items(), key=lambda kv: (kv[0] is None, kv[0] or ""))
    return [
        AssessmentGroupResponse(
            data_use=g["data_use"],
            data_use_name=g["data_use_name"],
            system_count=len(g["systems"]),
            assessments=g["assessments"],
        )
        for _, g in ordered
    ]


@privacycare_router.get(
    "",
    dependencies=[Security(verify_oauth_client, scopes=[SYSTEM_READ])],
    response_model=Page[AssessmentGroupResponse],
)
# `page`/`size` here paginate GROUPS (one item per data_use), not individual
# assessments — this route used to page assessments before task 2 introduced
# grouping, so the same params now mean something different to a caller.
def list_assessments(
    *,
    db: Session = Depends(get_db),
    params: Params = Depends(),
    status: str | None = None,
) -> Page[AssessmentGroupResponse]:
    return paginate(_grouped_assessments(db, status=status), params)


@privacycare_router.get(
    "/summary",
    dependencies=[Security(verify_oauth_client, scopes=[SYSTEM_READ])],
    response_model=AssessmentSummaryResponse,
)
def assessment_summary(*, db: Session = Depends(get_db)) -> AssessmentSummaryResponse:
    return AssessmentSummaryResponse(**_summary(db))


@privacycare_router.get(
    "/templates",
    dependencies=[Security(verify_oauth_client, scopes=[SYSTEM_READ])],
    response_model=Page[TemplateResponse],
)
def list_templates(
    *, db: Session = Depends(get_db), params: Params = Depends()
) -> Page[TemplateResponse]:
    # The admin-UI's RTK slice types this response as Page_TemplateResponse_
    # and GenerateAssessmentsModal reads templatesData?.items — a bare list
    # left the modal with nothing to render. paginate() here matches
    # list_assessments() above, which the UI already reads correctly.
    rows = _list_templates(db)
    return paginate(rows, params)


@privacycare_router.get(
    "/{assessment_id}",
    dependencies=[Security(verify_oauth_client, scopes=[SYSTEM_READ])],
    response_model=PrivacyAssessmentDetailResponse,
)
def get_assessment(
    assessment_id: str, *, db: Session = Depends(get_db)
) -> PrivacyAssessmentDetailResponse:
    try:
        return _assessment_detail(db, assessment_id)
    except LookupError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No assessment with id {assessment_id}",
        )


@privacycare_router.get(
    "/{assessment_id}/evidence",
    dependencies=[Security(verify_oauth_client, scopes=[SYSTEM_READ])],
    response_model=AssessmentEvidenceResponse,
)
def get_evidence(
    assessment_id: str, *, db: Session = Depends(get_db)
) -> AssessmentEvidenceResponse:
    return AssessmentEvidenceResponse(**_evidence_for(db, assessment_id))


def _update_answer(
    db: Session,
    assessment_id: str,
    question_id: str,
    answer_text: str,
    created_by: str | None,
) -> UpdateAnswerResponse:
    """Write one answer and assemble the envelope update_answer (below)
    hands back to the DPO's browser.

    Delegates the actual writes entirely to task 1's core — write_answer
    and recompute_completeness (fides.api.privacycare.api.answers) — this
    function's only job is building UpdateAnswerResponse from what those
    two left behind. write_answer already validated question_id belongs to
    assessment_id's template (or raised) before this function ever calls
    _question_by_id, so that lookup cannot legitimately come back None.

    Propagates write_answer's LookupError/QuestionNotInTemplateError
    unchanged rather than catching them here — update_answer (the actual
    HTTP route) is what maps those to 404, the same split this file already
    uses between _assessment_detail (raises LookupError) and get_assessment
    (catches it).

    Never commits — same "caller owns the transaction" convention as
    write_answer/recompute_completeness themselves. That keeps this
    function safely testable against a rolled-back transaction the same
    way _assessment_detail/_evidence_for already are; update_answer commits
    once, after this returns successfully.
    """
    write_answer(db, assessment_id, question_id, answer_text, created_by)
    completeness = recompute_completeness(db, assessment_id)
    q = _question_by_id(db, assessment_id, question_id)
    if q is None:
        # Fix round 3 (whole-range review, finding 4): an ENFORCED invariant,
        # not an asserted one. The argument below — write_answer already
        # validated this question against the assessment's template, so the
        # lookup cannot legitimately come back None — is sound today. If it
        # ever stops being sound, the un-guarded failure mode was a TypeError
        # on q["evidence"] surfacing as a 500 on a SUCCESSFUL write that had
        # already appended an answer_version: the DPO's answer saved, the
        # DPO's browser shown a crash. That is the worst response shape this
        # surface can produce. LookupError is the one the route already maps
        # to the 404 that says, truthfully, "that question is not there".
        raise LookupError(
            f"question {question_id} vanished from assessment {assessment_id} "
            f"between the write and the read-back"
        )
    assessment_status = db.execute(
        _ASSESSMENT_STATUS_SQL, {"assessment_id": assessment_id}
    ).scalar()
    return UpdateAnswerResponse(
        question=_question_response(q, assessment_id),
        completeness=completeness,
        status=assessment_status,
    )


@privacycare_router.put(
    "/{assessment_id}/questions/{question_id}",
    # Copied exactly from every read route above:
    # dependencies=[Security(verify_oauth_client, scopes=[SYSTEM_READ])].
    # test_api_registration.py's
    # test_every_assessment_route_requires_verify_oauth_client_with_system_read
    # asserts this literally, for every /plus/privacy-assessments route
    # regardless of HTTP method — this module has no
    # privacy-assessment-specific scope to reach for (Fides OSS's scope
    # registry has none), so SYSTEM_READ is the blanket stand-in the read
    # routes already established, not a scope this route independently
    # chose.
    dependencies=[Security(verify_oauth_client, scopes=[SYSTEM_READ])],
    response_model=UpdateAnswerResponse,
)
def update_answer(
    assessment_id: str,
    question_id: str,
    request: UpdateAnswerRequest,
    *,
    db: Session = Depends(get_db),
    # A second Security(...) call, same function and scopes as the
    # dependencies=[...] entry above (FastAPI's dependency cache collapses
    # matching calls within one request, so this does not re-verify the
    # token twice) — captured as a parameter, unlike every read route
    # above, so client.user_id is reachable below. That is the same
    # accessor Ethyca's own write endpoints use for this exact purpose
    # (e.g. privacy_request_endpoints.py's reviewed_by=client.user_id,
    # imported_by=client.user_id): the linked FidesUser id when this token
    # belongs to a human's personal client, None for a system-to-system API
    # client with no linked user or the root client (see
    # _created_by_from_client below for both cases and why neither is
    # rejected).
    client: ClientDetail = Security(verify_oauth_client, scopes=[SYSTEM_READ]),
) -> UpdateAnswerResponse:
    """PUT one answer. created_by comes from the AUTHENTICATED PRINCIPAL
    (_created_by_from_client(client)) above, never from `request` —
    UpdateAnswerRequest carries only answer_text, precisely so a client
    cannot forge authorship in the answer_version audit trail.

    A thin HTTP shell around _update_answer: its only three jobs are (1)
    resolving a never-null author from the authenticated client, (2)
    mapping LookupError (unknown assessment) and QuestionNotInTemplateError
    (unknown/foreign question) to 404 — both are the same client-facing
    fact, "that thing is not there", and neither may escape as a 500 — and
    (3) committing once _update_answer has succeeded.
    """
    created_by = _created_by_from_client(client)
    try:
        response = _update_answer(
            db, assessment_id, question_id, request.answer_text, created_by
        )
    except (LookupError, QuestionNotInTemplateError):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No question {question_id} on assessment {assessment_id}",
        )
    db.commit()
    return response


def _all_questions_response(
    db: Session, assessment_id: str
) -> list[AssessmentQuestionResponse]:
    """Every question on the assessment, flattened out of _questions_for's
    per-group grouping.

    Used by the bulk-answer route's `questions` field, which carries the
    assessment's FULL question set — not just the entries a given batch
    touched. That decision came from checking how the response is actually
    consumed rather than assuming: privacy-assessments.slice.ts's
    bulkUpdateAssessmentAnswers mutation declares no
    onQueryStarted/updateQueryData handler of its own (unlike two other
    mutations in that same file, which do) — it only invalidatesTags
    `{type: "Privacy Assessment", id}` / "Privacy Assessment", the same
    pattern updatePrivacyAssessment already uses. That triggers RTK Query
    to refetch getAssessment, and AssessmentDetail.tsx reads its questions
    exclusively off that query's `assessment.question_groups`
    (useBulkUpdateAssessmentAnswersMutation itself is exported from the
    slice but not called from any shipped component yet — there is no live
    consumer of this field today). So nothing currently reads
    BulkUpdateAnswersResponse.questions to patch state directly. But the
    contract still requires the field to be a real, correctly-shaped
    AssessmentQuestion[], and the one shape that can never regress into the
    brief's named failure mode — a wholesale question-list replacement
    blanking out every untouched question — is the full set. A subset
    would only ever be safe by accident (only ever correct for as long as
    no consumer does exactly what this same slice's sibling mutations
    already do).
    """
    return [
        _question_response(q, assessment_id)
        for group in _questions_for(db, assessment_id)
        for q in group["questions"]
    ]


def _bulk_update_answers(
    db: Session,
    assessment_id: str,
    answers: list[AnswerUpdate],
    created_by: str | None,
) -> BulkUpdateAnswersResponse:
    """Write a batch of answers as ONE atomic unit and assemble the envelope
    bulk_update_answers (below) hands back to the DPO's browser.

    Atomicity — the whole batch rolls back on any bad entry, never a
    partial apply. A partially-applied batch that reports success is the
    worst outcome for a document a regulator will read: the DPO believes
    all N answers saved, some did, and nothing tells them which. Every
    write_answer call below takes write_answer's own FOR UPDATE lock on
    the SAME parent privacy_assessment row (see
    _lock_assessment_and_get_template_id in api/answers.py) — that already
    serializes this whole function against any concurrent writer touching
    this assessment, so this function adds no second lock and never locks
    any other row (locking several assessments in one transaction would be
    a deadlock hazard, and this function only ever touches one).

    What the lock does NOT give us for free is atomicity of the BATCH
    itself: without more, a QuestionNotInTemplateError raised on entry N
    would leave entries 1..N-1's writes sitting uncommitted-but-VISIBLE in
    this same transaction — nothing rolls them back just because a later
    entry failed, and a caller that forgot to abort the whole request
    would commit them anyway. `db.begin_nested()` opens a SAVEPOINT before
    the loop; on any exception escaping the `with` block (including
    write_answer's QuestionNotInTemplateError/LookupError), SQLAlchemy
    rolls back to that SAVEPOINT automatically, undoing every write this
    batch made so far — while leaving the assessment-row lock untouched.
    (The lock survives for a plainer reason than nesting: Postgres does not
    release row locks on ROLLBACK TO SAVEPOINT at all. It is held until the
    outer transaction ends, whether it was first acquired inside the
    savepoint region or before it.) The exception then
    propagates to bulk_update_answers (the route), which maps it to 404
    and never calls db.commit().

    An empty `answers` list is a no-op, not an error: the loop below simply
    never executes (updated_count comes back 0), and the response still
    reflects the assessment's current, unchanged completeness/status/
    questions.

    Completeness is recomputed ONCE, after the whole batch — not per
    answer. Recomputing per-write would still be correct but wasteful (N
    redundant COUNT(*) scans for a batch of size N, all reading the exact
    same rows for everything but the write each one is meant to reflect);
    a single recompute after the loop already reflects every write the
    batch made.
    """
    with db.begin_nested():
        for answer in answers:
            write_answer(
                db, assessment_id, answer.question_id, answer.answer_text, created_by
            )

    completeness = recompute_completeness(db, assessment_id)
    questions = _all_questions_response(db, assessment_id)
    assessment_status = db.execute(
        _ASSESSMENT_STATUS_SQL, {"assessment_id": assessment_id}
    ).scalar()
    return BulkUpdateAnswersResponse(
        updated_count=len(answers),
        completeness=completeness,
        status=assessment_status,
        questions=questions,
    )


@privacycare_router.put(
    "/{assessment_id}/questions",
    # Same blanket SYSTEM_READ dependency as every other route in this
    # module — see update_answer's own comment above for why (no
    # privacy-assessment-specific scope exists yet; this is being handled
    # separately and this route's scope must not change independently of
    # that).
    dependencies=[Security(verify_oauth_client, scopes=[SYSTEM_READ])],
    response_model=BulkUpdateAnswersResponse,
)
def bulk_update_answers(
    assessment_id: str,
    request: BulkUpdateAnswersRequest,
    *,
    db: Session = Depends(get_db),
    client: ClientDetail = Security(verify_oauth_client, scopes=[SYSTEM_READ]),
) -> BulkUpdateAnswersResponse:
    """PUT a batch of answers at once. created_by comes from the
    AUTHENTICATED PRINCIPAL (_created_by_from_client(client)), never from
    `request` — same reasoning as update_answer above: BulkUpdateAnswersRequest
    carries only question_id/answer_text pairs, precisely so a client
    cannot forge authorship in the answer_version audit trail.

    A thin HTTP shell around _bulk_update_answers: its only three jobs are
    (1) resolving a never-null author from the authenticated client, (2)
    mapping LookupError (unknown assessment) and QuestionNotInTemplateError
    (any unknown/foreign question_id in the batch) to 404 — _bulk_update_answers
    has already rolled back every write the batch attempted by the time
    either exception reaches here, so there is nothing left to undo, only
    to report — and (3) committing once _bulk_update_answers has succeeded.
    """
    created_by = _created_by_from_client(client)
    try:
        response = _bulk_update_answers(db, assessment_id, request.answers, created_by)
    except (LookupError, QuestionNotInTemplateError):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No assessment {assessment_id}, or one of its questions, was found",
        )
    db.commit()
    return response


# --- PUT/DELETE .../{assessment_id} (task 4: update and delete the
# assessment itself, not its answers) ---
#
# Only these three columns are user-editable via this route — everything
# else on privacy_assessment (system_fides_key, data_use, created_by, the
# generation-task link, ...) is either set at creation time or derived, and
# has no field on UpdatePrivacyAssessmentRequest to carry it. This set is
# also what makes the f-string SET clause in _update_assessment below safe:
# every key it can ever interpolate as a bare SQL identifier comes from
# UpdatePrivacyAssessmentRequest.model_fields, a closed, code-defined set —
# never a request-supplied string.
# Existence check AND parent row lock in one statement — the same FOR UPDATE
# discipline every other write path on this surface uses (see
# _lock_assessment_and_get_template_id in api/answers.py, and
# _LOCK_ASSESSMENT_SQL's comment there for the three races it closes).
#
# Used by both task-4 core functions, for different halves of what it
# returns:
#
# - _update_assessment's empty-body branch wants the LOCK (fix round 3,
#   whole-range review, finding 8: that branch used to do two unlocked reads
#   — this existence check, then _ASSESSMENT_DETAIL_SQL for the response — so
#   under READ COMMITTED a concurrent answer write could commit between them
#   and the returned AssessmentResponse could mix pre-write and post-write
#   state, e.g. a stale completeness beside a fresh updated_at. It was the
#   one place this surface's own locking discipline was not applied).
#
# - _delete_assessment wants the NAME as well, for its warning, and the lock
#   is what makes the version count it takes next TRUTHFUL rather than
#   approximate: without it a concurrent write_answer could append a version
#   between the COUNT and the DELETE, and the log line would under-report
#   what was destroyed by exactly the rows nobody can now go and look at.
_LOCK_ASSESSMENT_ROW_SQL = sqlalchemy.text(
    "SELECT name FROM privacy_assessment WHERE id = :assessment_id FOR UPDATE"
)

_UPDATABLE_ASSESSMENT_FIELDS = {"name", "status", "risk_level"}


def _update_assessment(
    db: Session, assessment_id: str, updates: dict
) -> AssessmentResponse:
    """Partial update of privacy_assessment's three editable fields.

    `updates` must already be UpdatePrivacyAssessmentRequest.model_dump(
    exclude_unset=True) — ONLY the keys the caller actually sent. A field
    absent from `updates` is left completely alone: this function never
    writes a column it wasn't explicitly told to. update_assessment (the
    route) is the only caller and is what enforces that contract; this
    function trusts its input the same way _bulk_update_answers trusts
    write_answer's caller-owns-the-transaction convention.

    An empty `updates` (a body with all three fields omitted) is a
    deliberate no-op WRITE, not a no-op existence check: the row is still
    looked up — under the same FOR UPDATE row lock every other path here
    holds across its reads (fix round 3, finding 8) — and its current,
    unchanged state is returned. Same "still 404s an unknown id, still
    returns real state for a known one" precedent as
    _bulk_update_answers's empty-batch handling.

    updated_at is bumped explicitly (`, updated_at = now()`) because this is
    raw SQL via sqlalchemy.text(), not an ORM UPDATE — PrivacyAssessment's
    `onupdate=func.now()` (fides.api.db.base_class.Base) is a SQLAlchemy
    ORM-level Column default that only fires through the ORM's own UPDATE
    path; a raw db.execute(text(...)) bypasses it entirely, the same bypass
    already documented for created_at elsewhere in this module. (Contrast
    recompute_completeness in api/answers.py, which does NOT bump
    updated_at — that write is a derived, timestamp-agnostic side-effect of
    an answer edit; this one is a direct edit of a user-facing field, where
    "when was this assessment last changed" is exactly what a DPO reading
    the record would expect to move.)

    Raises LookupError if assessment_id does not exist — mapped to 404 by
    update_assessment, the same split every other core function in this
    module uses.
    """
    # Not an assert: `python -O` strips asserts, and this is the ONLY thing
    # standing between a caller-supplied key and an identifier interpolated
    # straight into the SET clause below. Values are bound; column names
    # cannot be. UpdatePrivacyAssessmentRequest already closes the set, so
    # this should be unreachable — which is exactly why it must not be the
    # kind of guard that vanishes under an optimisation flag.
    unknown_fields = set(updates) - _UPDATABLE_ASSESSMENT_FIELDS
    if unknown_fields:
        raise ValueError(
            f"_update_assessment received field(s) outside "
            f"_UPDATABLE_ASSESSMENT_FIELDS: {sorted(unknown_fields)}"
        )

    if updates:
        set_clause = ", ".join(f"{field} = :{field}" for field in updates)
        result = db.execute(
            sqlalchemy.text(
                f"UPDATE privacy_assessment SET {set_clause}, updated_at = now() "
                "WHERE id = :assessment_id"
            ),
            {**updates, "assessment_id": assessment_id},
        )
        if result.rowcount == 0:
            raise LookupError(f"No assessment with id {assessment_id}")
    else:
        # Fix round 3 (finding 8): _LOCK_ASSESSMENT_ROW_SQL, not the
        # lock-free _ASSESSMENT_STATUS_SQL this used to use. The UPDATE in
        # the branch above takes the row lock implicitly, as its first
        # statement, and holds it across the detail read below; this branch
        # writes nothing, so nothing was holding that lock for it and its
        # two reads could straddle a concurrent commit. Same statement,
        # same LookupError, one row lock more.
        exists = db.execute(
            _LOCK_ASSESSMENT_ROW_SQL, {"assessment_id": assessment_id}
        ).first()
        if exists is None:
            raise LookupError(f"No assessment with id {assessment_id}")

    # _ASSESSMENT_DETAIL_SQL's column list is a strict superset of what
    # _assessment_to_response reads (it additionally selects
    # assessment_type/privacy_assessment_task_id, for the detail-response
    # caller above) — reused here rather than a fourth near-duplicate
    # single-row query, same "don't hand-roll a query this file already
    # has" reasoning as _question_by_id's own docstring.
    row = (
        db.execute(_ASSESSMENT_DETAIL_SQL, {"assessment_id": assessment_id})
        .mappings()
        .first()
    )
    return _assessment_to_response(row)


@privacycare_router.put(
    "/{assessment_id}",
    # Same blanket SYSTEM_READ dependency as every other route in this
    # module — see update_answer's own comment for why (no
    # privacy-assessment-specific scope exists yet; being handled
    # separately, and this route's scope must not change independently of
    # that).
    dependencies=[Security(verify_oauth_client, scopes=[SYSTEM_READ])],
    # PrivacyAssessmentResponse (the type the admin-UI's
    # updatePrivacyAssessment mutation actually declares — checked directly
    # against privacy-assessments.slice.ts) narrows AssessmentResponse's
    # `status`/`risk_level` to required-nullable rather than genuinely
    # optional. AssessmentResponse is reused anyway, per this task's own
    # brief ("AssessmentResponse already exists in schemas.py. Reuse it; do
    # not define a second") and the precedent already set by
    # AssessmentGroupResponse.assessments (typed `PrivacyAssessmentResponse[]`
    # in TS, `List[AssessmentResponse]` here) — the same acceptable
    # looseness this codebase already ships, not a new one this route
    # introduces.
    response_model=AssessmentResponse,
)
def update_assessment(
    assessment_id: str,
    request: UpdatePrivacyAssessmentRequest,
    *,
    db: Session = Depends(get_db),
    # Fix round 3 (whole-range review, MAJOR finding 3): the two task-4
    # routes were the only writers on this surface that captured no
    # authenticated principal at all. Same second-Security(...) shape as
    # update_answer/bulk_update_answers above (FastAPI's dependency cache
    # collapses matching calls within one request, so the token is not
    # verified twice).
    client: ClientDetail = Security(verify_oauth_client, scopes=[SYSTEM_READ]),
) -> AssessmentResponse:
    """PUT the assessment itself: name/status/risk_level, partial update.

    privacy_assessment still carries no "who last edited the metadata"
    column to populate (created_by is set once, at generation time, and this
    route never touches it), so there is nowhere to PERSIST an actor. But
    "nowhere to persist it" is not a reason to know nothing: flipping status
    to `completed` is the single most consequential assertion in this
    document — the claim that the §31 assessment was done — and it was being
    recorded with no actor anywhere, while a one-word answer edit is fully
    attributed. The log line below is what can exist without adding a column
    to an Ethyca-authored table. Info, not warning: this changes a field, it
    destroys nothing (contrast _delete_assessment).

    The actor comes from _created_by_from_client(client) — the authenticated
    principal, never `request` — the same never-null derivation every answer
    write uses.

    request.model_dump(exclude_unset=True) is the exclude-absent-fields
    mechanism itself: only keys the caller actually sent reach
    _update_assessment. An unknown assessment_id maps LookupError to 404,
    same convention as every other route in this module.
    """
    updated_by = _created_by_from_client(client)
    updates = request.model_dump(exclude_unset=True)
    try:
        response = _update_assessment(db, assessment_id, updates)
    except LookupError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No assessment with id {assessment_id}",
        )
    logger.info(
        "PrivacyCare DPIA updated by {}: assessment {} field(s) {}",
        updated_by,
        assessment_id,
        sorted(updates),
    )
    db.commit()
    return response


# Counted BEFORE the delete — afterwards there is nothing left to count.
_CASCADED_VERSION_COUNT_SQL = sqlalchemy.text(
    "SELECT COUNT(*) FROM answer_version av "
    "JOIN assessment_answer a ON a.id = av.answer_id "
    "WHERE a.assessment_id = :assessment_id"
)


def _delete_assessment(db: Session, assessment_id: str, deleted_by: str) -> None:
    """Hard delete of the privacy_assessment row itself.

    `deleted_by` is REQUIRED, with no default, and is used for one thing:
    the warning below. Fix round 3 (whole-range review, MAJOR finding 3):
    this was the one write path on the whole surface that named nobody and
    left no trace. Every answer write derives a never-null author via
    _created_by_from_client into a DB-NOT-NULL column; this — the call that
    destroys all of them at once — derived nothing, and there is no
    "deleted_by" column on an Ethyca-authored table for it to derive into.
    A required parameter is what stops a future caller from performing an
    untraceable delete by omission; the log line is the only record that can
    exist without adding a column to their schema.

    HARD delete, not soft: privacy_assessment carries no deleted_at column
    (verified against the live schema — see the FK/column dump quoted in
    this task's report) and privacy_assessment is an Ethyca-authored table.
    Adding one would put this app's Alembic chain into their schema —
    exactly what fides.api.privacycare.migrations.include_object exists to
    keep separate, the same "do not add a constraint/column to their
    table" boundary write_answer's own module docstring already draws for
    assessment_answer/answer_version. So: implement what the schema
    supports. Whether that schema SHOULD have offered a soft delete for a
    regulatory record is a real, separate question — recorded plainly in
    this task's report, not resolved silently here.

    Every FK from assessment_answer -> privacy_assessment, and from
    answer_version -> assessment_answer, is ON DELETE CASCADE (verified
    against the live schema — quoted in the report): deleting this one row
    removes every assessment_answer and answer_version row for it as an
    atomic part of THIS statement, inside the same transaction, not as a
    separate step this function has to orchestrate. There is no
    partial-delete/orphan case for THIS pair of relationships to handle —
    Postgres's own cascade already makes it atomic. (`questionnaire` cascades
    the same way, off a table this OSS repo does not otherwise touch.)

    Raises LookupError if assessment_id does not exist, mapped to 404 by
    delete_assessment (the route) — same convention as every other core
    function in this module. A second delete of the same id (nothing left
    to match) raises the same LookupError the same way, which is exactly
    how "deleting twice 404s the second time" falls out of the existence
    check below with no extra state to track. That check is now the locking
    SELECT rather than the DELETE's rowcount: it has to run first anyway to
    read the name and count the versions, and reading the row under FOR
    UPDATE answers "is it there" and "nobody may add versions to it between
    my count and my delete" in one statement.

    WHAT THE WARNING RECORDS, and why it is warning-level rather than info:
    this is not one row. Every FK in the chain is ON DELETE CASCADE, so the
    count below is the number of answer_version rows — the entire
    append-only history this module exists to build — that this single
    statement destroys irreversibly. Under Kenya's DPA 2019 §31 that history
    IS the artifact proving the assessment happened. Nothing else in the
    system will record that it is gone, or who made it go.
    """
    row = db.execute(
        _LOCK_ASSESSMENT_ROW_SQL, {"assessment_id": assessment_id}
    ).first()
    if row is None:
        raise LookupError(f"No assessment with id {assessment_id}")
    name = row[0]

    version_count = db.execute(
        _CASCADED_VERSION_COUNT_SQL, {"assessment_id": assessment_id}
    ).scalar()

    logger.warning(
        "PrivacyCare DPIA DELETED by {}: assessment {} ({!r}); the cascade "
        "destroys {} answer_version row(s) — the whole append-only answer "
        "history for this assessment, irrecoverably",
        deleted_by,
        assessment_id,
        name,
        version_count,
    )

    db.execute(
        sqlalchemy.text("DELETE FROM privacy_assessment WHERE id = :assessment_id"),
        {"assessment_id": assessment_id},
    )


@privacycare_router.delete(
    "/{assessment_id}",
    # Same blanket SYSTEM_READ dependency as every other route in this
    # module — see update_answer's own comment for why.
    dependencies=[Security(verify_oauth_client, scopes=[SYSTEM_READ])],
    response_model=DeletePrivacyAssessmentResponse,
)
def delete_assessment(
    assessment_id: str,
    *,
    db: Session = Depends(get_db),
    # Fix round 3 (whole-range review, MAJOR finding 3): see
    # update_assessment's own comment. This route is the one that matters
    # most — it is the only call on this surface that destroys evidence, and
    # it used to name nobody.
    client: ClientDetail = Security(verify_oauth_client, scopes=[SYSTEM_READ]),
) -> DeletePrivacyAssessmentResponse:
    """DELETE the assessment. See _delete_assessment's docstring for the
    hard-vs-soft-delete decision, the FK cascade this relies on, and what
    the warning it emits records.

    A thin HTTP shell, same shape as every write route in this module: (1)
    resolves the actor from the authenticated principal via
    _created_by_from_client — never from the request, the same never-null
    derivation every answer write uses — (2) maps _delete_assessment's
    LookupError to 404, and (3) commits only once the delete has actually
    succeeded.

    The scope on this route is deliberately NOT changed here. That a
    SYSTEM_READ token can reach a destructive call is a real problem and is
    escalated separately; changing it independently of that escalation is
    what every other route's scope comment in this module already refuses to
    do. What this route can fix on its own is the absence of any trace,
    which is what the actor above is for.
    """
    deleted_by = _created_by_from_client(client)
    try:
        _delete_assessment(db, assessment_id, deleted_by)
    except LookupError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No assessment with id {assessment_id}",
        )
    db.commit()
    # The warning _delete_assessment emits fires BEFORE this commit, which
    # is the safe direction to fail: a rolled-back delete leaves a log
    # claiming a destruction that did not happen (misleading, but the data
    # is still there), whereas logging only after the commit would let a
    # crash in between destroy the whole §31 history with no trace at all.
    # Over-logging beats under-logging when the subject is evidence
    # destruction. This second line is what distinguishes the two cases
    # after the fact: its absence next to a DELETED warning means the
    # transaction did not commit.
    logger.warning(
        "PrivacyCare DPIA delete COMMITTED by {}: assessment {}",
        deleted_by,
        assessment_id,
    )
    return DeletePrivacyAssessmentResponse(id=assessment_id)
