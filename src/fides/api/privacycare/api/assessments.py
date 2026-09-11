# Read endpoints for the DPIA engine.
import sqlalchemy
from fastapi import Depends, HTTPException, Security, status
from fastapi_pagination import Page, Params, paginate
from sqlalchemy.orm import Session

from fides.api.deps import get_db
from fides.api.oauth.utils import verify_oauth_client
from fides.api.privacycare.api.router import privacycare_router
from fides.api.privacycare.api.schemas import AssessmentResponse
from fides.common.scope_registry import SYSTEM_READ

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
)
def assessment_summary() -> dict:
    return {}


@privacycare_router.get(
    "/templates",
    dependencies=[Security(verify_oauth_client, scopes=[SYSTEM_READ])],
)
def list_templates() -> list:
    return []


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
