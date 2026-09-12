# Read endpoints for the DPIA engine.
import sqlalchemy
from fastapi import Depends, HTTPException, Security, status
from fastapi_pagination import Page, Params, paginate
from loguru import logger
from sqlalchemy.orm import Session

from fides.api.deps import get_db
from fides.api.oauth.utils import verify_oauth_client
from fides.api.privacycare.api.router import privacycare_router
from fides.api.privacycare.api.schemas import (
    AssessmentEvidenceResponse,
    AssessmentGroupResponse,
    AssessmentMetadata,
    AssessmentQuestionResponse,
    AssessmentResponse,
    AssessmentSummaryResponse,
    PrivacyAssessmentDetailResponse,
    QuestionGroup,
    TemplateResponse,
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
    SELECT q.id, q.requirement_key, q.requirement_title, q.group_order,
           q.question_key, q.question_text, q.guidance, q.question_order,
           q.required, q.fides_sources, q.expected_coverage, a.id AS answer_id,
           av.answer_status, av.answer_text, av.answer_source, av.confidence,
           av.evidence
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
    for row in db.execute(
        _QUESTION_SQL, {"assessment_id": assessment_id}
    ).mappings():
        key = row["requirement_key"]
        if key not in index:
            index[key] = {
                "requirement_key": key,
                "requirement_title": row["requirement_title"],
                "group_order": row["group_order"],
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
            }
        )
    return groups


def _as_str(value):
    return value.isoformat() if hasattr(value, "isoformat") else value


def _evidence_items_for(db: Session, assessment_id: str) -> list[dict]:
    # `answer_version.evidence` has no writer in this repo yet (that lands in
    # plan 04) and the live `fides` database has zero answer_version rows, so
    # there is no observed payload to shape this against. Its column default
    # is a JSON *object* (not an array), which matches one EvidenceItem per
    # answer_version rather than a list of them — so each qualifying row is
    # treated as a single EvidenceItem-shaped payload. A payload that lacks
    # the two fields EvidenceItem requires (`id`, `type`) is skipped rather
    # than papered over with invented values.
    #
    # Fix round 1: this skip was silent — a malformed `evidence` object just
    # vanished, with nothing to say so. Evidence is the thing a DPIA audit
    # asks for; once plan 04's writer exists, a shape bug there would make
    # evidence disappear from every assessment with no error, no log, no
    # count. Skipping is still correct (one bad row must not fail the whole
    # endpoint), but it must be loud, not silent.
    items: list[dict] = []
    for row in db.execute(
        _EVIDENCE_SQL, {"assessment_id": assessment_id}
    ).mappings():
        payload = row["evidence"]
        if not isinstance(payload, dict):
            logger.warning(
                "Skipped evidence for assessment {} question {}: evidence "
                "was not a JSON object (got {})",
                assessment_id,
                row["question_id"],
                type(payload).__name__,
            )
            continue
        missing_fields = [f for f in ("id", "type") if f not in payload]
        if missing_fields:
            logger.warning(
                "Skipped evidence for assessment {} question {}: missing "
                "required field(s) {}",
                assessment_id,
                row["question_id"],
                missing_fields,
            )
            continue
        items.append(
            {
                "id": payload["id"],
                "type": payload["type"],
                "value": payload.get("value"),
                "created_at": payload.get("created_at")
                or _as_str(row["created_at"])
                or _as_str(row["updated_at"]),
                "field_name": payload.get("field_name"),
                "source_key": payload.get("source_key"),
                "source_type": payload.get("source_type"),
                "citation_number": payload.get("citation_number"),
                "data": payload.get("data"),
            }
        )
    return items


def _evidence_for(db: Session, assessment_id: str) -> dict:
    items = _evidence_items_for(db, assessment_id)
    return {
        "assessment_id": assessment_id,
        "total_count": len(items),
        "items": items,
    }


def _question_response(q: dict) -> AssessmentQuestionResponse:
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
    # evidence: List[dict], required. answer_version.evidence is a single
    # JSONB *object* (verified against the live schema; see _evidence_items_for
    # above), not an array, with server_default '{}' meaning "answered, no
    # evidence attached" — same exact-default exclusion already used there.
    # An unanswered question (no av row at all) has evidence=None. Both cases
    # collapse to the empty list; a genuinely populated object is wrapped as
    # the list's single element.
    evidence = q["evidence"]
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
        evidence=[evidence] if evidence else [],
        # No database source (Plus computes these) — empty forms, not
        # omitted. See the plan's field-by-field source table.
        missing_data=[],
        sme_prompt=None,
    )


def _question_group_response(group: dict) -> QuestionGroup:
    questions = [_question_response(q) for q in group["questions"]]
    # Definition, checked against the UI rather than assumed (fix round 1):
    # answered_count/total_count is a COMPLETION-PROGRESS indicator, not a
    # "has this been touched" tally. QuestionGroupPanel.tsx renders
    # `isGroupCompleted = answeredCount === totalCount` to switch a group's
    # tag between "Completed" and "Pending", and shows the raw fraction as
    # "Fields: {answeredCount}/{totalCount}". AssessmentDetail.tsx treats
    # AnswerStatus.NEEDS_INPUT as explicitly outstanding — it filters
    # `allQuestions` by that exact status to build `needsInputIds`, the set
    # sent back out for more input via Slack/Teams. Counting a needs_input
    # answer as "answered" would show a group as Completed while a question
    # inside it is still, by its own status, waiting on someone — an
    # overstatement of completeness this is a regulatory (DPIA) record.
    # So: "answered" here means the LEFT JOIN to answer_version resolved
    # (a current answer exists) AND that answer's status is not
    # "needs_input" — i.e. "complete" or "partial" count, "needs_input"
    # and "no answer at all" do not.
    answered_count = sum(
        1
        for q in group["questions"]
        if q["answer_status"] is not None and q["answer_status"] != "needs_input"
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
        last_updated_at=None,
        last_updated_by=None,
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
    return AssessmentMetadata(
        generation_timestamp=_as_str(row["created_at"]),
        model_used=row["llm_model"],
        use_llm=row["use_llm"],
    )


def _assessment_detail(db: Session, assessment_id: str) -> PrivacyAssessmentDetailResponse:
    row = (
        db.execute(_ASSESSMENT_DETAIL_SQL, {"assessment_id": assessment_id})
        .mappings()
        .first()
    )
    if row is None:
        raise LookupError(f"No assessment with id {assessment_id}")
    question_groups = [
        _question_group_response(g) for g in _questions_for(db, assessment_id)
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
            key=template_key(r["name"], r["id"]),  # id fallback: never emit an empty key
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
    owners_list = sorted(
        owners.values(), key=lambda o: o["open_count"], reverse=True
    )

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
    "/{assessment_id}/questions",
    dependencies=[Security(verify_oauth_client, scopes=[SYSTEM_READ])],
)
def get_questions(assessment_id: str, *, db: Session = Depends(get_db)) -> list:
    return _questions_for(db, assessment_id)


@privacycare_router.get(
    "/{assessment_id}/evidence",
    dependencies=[Security(verify_oauth_client, scopes=[SYSTEM_READ])],
    response_model=AssessmentEvidenceResponse,
)
def get_evidence(
    assessment_id: str, *, db: Session = Depends(get_db)
) -> AssessmentEvidenceResponse:
    return AssessmentEvidenceResponse(**_evidence_for(db, assessment_id))
