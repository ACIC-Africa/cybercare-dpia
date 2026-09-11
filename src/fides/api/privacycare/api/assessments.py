# Read endpoints for the DPIA engine.
from fastapi import Security

from fides.api.oauth.utils import verify_oauth_client
from fides.api.privacycare.api.router import privacycare_router
from fides.common.scope_registry import SYSTEM_READ


@privacycare_router.get(
    "",
    dependencies=[Security(verify_oauth_client, scopes=[SYSTEM_READ])],
)
def list_assessments() -> dict:
    # Body lands in Task 3.
    return {"items": [], "total": 0, "page": 1, "size": 0, "pages": 0}


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
)
def get_assessment(assessment_id: str) -> dict:
    return {}


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
