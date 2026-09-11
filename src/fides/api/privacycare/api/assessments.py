# Read endpoints for the DPIA engine.
from typing import List

import sqlalchemy
from fastapi import Depends, HTTPException, Security, status
from fastapi_pagination import Page, Params, paginate
from sqlalchemy.orm import Session

from fides.api.deps import get_db
from fides.api.oauth.utils import verify_oauth_client
from fides.api.privacycare.api.router import privacycare_router
from fides.api.privacycare.api.schemas import (
    AssessmentResponse,
    AssessmentSummaryResponse,
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
    ORDER BY name, version
    """
)

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


def _list_assessments(db: Session):
    return db.execute(_ASSESSMENT_SQL).mappings().all()


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
    def as_str(value):
        return value.isoformat() if hasattr(value, "isoformat") else value

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
        created_at=as_str(row["created_at"]),
        updated_at=as_str(row["updated_at"]),
    )


@privacycare_router.get(
    "",
    dependencies=[Security(verify_oauth_client, scopes=[SYSTEM_READ])],
    response_model=Page[AssessmentResponse],
)
def list_assessments(
    *, db: Session = Depends(get_db), params: Params = Depends()
) -> Page[AssessmentResponse]:
    rows = [_assessment_to_response(r) for r in _list_assessments(db)]
    return paginate(rows, params)


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
    response_model=List[TemplateResponse],
)
def list_templates(*, db: Session = Depends(get_db)) -> List[TemplateResponse]:
    return _list_templates(db)


@privacycare_router.get(
    "/{assessment_id}",
    dependencies=[Security(verify_oauth_client, scopes=[SYSTEM_READ])],
    response_model=AssessmentResponse,
)
def get_assessment(
    assessment_id: str, *, db: Session = Depends(get_db)
) -> AssessmentResponse:
    row = next(
        (r for r in _list_assessments(db) if r["id"] == assessment_id), None
    )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No assessment with id {assessment_id}",
        )
    return _assessment_to_response(row)


@privacycare_router.get(
    "/{assessment_id}/questions",
    dependencies=[Security(verify_oauth_client, scopes=[SYSTEM_READ])],
)
def get_questions(assessment_id: str) -> list:
    return []


@privacycare_router.get(
    "/{assessment_id}/evidence",
    dependencies=[Security(verify_oauth_client, scopes=[SYSTEM_READ])],
)
def get_evidence(assessment_id: str) -> list:
    return []
