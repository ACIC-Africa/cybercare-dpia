"""Generation task routes: queue a run, then watch it."""
import uuid

import sqlalchemy
from fastapi import Depends, Security
from loguru import logger
from sqlalchemy.orm import Session

from fides.api.deps import get_db
from fides.api.models.client import ClientDetail
from fides.api.oauth.utils import verify_oauth_client
from fides.api.privacycare.api.assessments import _created_by_from_client
from fides.api.privacycare.api.router import privacycare_router
from fides.api.privacycare.api.schemas import (
    CreateAssessmentTaskRequest,
    CreateAssessmentTaskResponse,
)
from fides.api.privacycare.tasks import GENERATION_QUEUE, generate_assessments
from fides.common.scope_registry import SYSTEM_READ

_INSERT_TASK_SQL = sqlalchemy.text(
    "INSERT INTO privacy_assessment_task "
    "(id, action_type, status, celery_id, assessment_types, "
    " system_fides_keys, created_by, use_llm, llm_model, high_risk_only) "
    "VALUES (:id, 'generate', 'pending', :celery_id, :assessment_types, "
    " :system_fides_keys, :created_by, :use_llm, :llm_model, :high_risk_only)"
)


def _create_task(
    db: Session, request: CreateAssessmentTaskRequest, created_by: str
) -> tuple[str, str]:
    """Write the task row. Returns (task_row_id, celery_id).

    The Celery id is generated HERE, before the INSERT, rather than taken
    from the AsyncResult afterwards. privacy_assessment_task.celery_id is
    NOT NULL, so the row cannot be written after queueing; and the row must
    exist before the worker starts, so it cannot be written after either.
    Generating the id ourselves and passing it to apply_async(task_id=...)
    removes the ordering problem entirely — no placeholder value, and no
    follow-up UPDATE that a fast worker could outrun.
    """
    task_row_id = f"pat_{uuid.uuid4().hex[:12]}"
    celery_id = str(uuid.uuid4())
    db.execute(
        _INSERT_TASK_SQL,
        {
            "id": task_row_id,
            "celery_id": celery_id,
            "assessment_types": list(request.assessment_types),
            "system_fides_keys": (
                list(request.system_fides_keys)
                if request.system_fides_keys
                else None
            ),
            "created_by": created_by,
            "use_llm": request.use_llm,
            "llm_model": request.model,
            "high_risk_only": request.high_risk_only,
        },
    )
    return task_row_id, celery_id


@privacycare_router.post(
    "",
    # Same blanket SYSTEM_READ dependency as every other route in this
    # module — see update_answer's own comment for why.
    dependencies=[Security(verify_oauth_client, scopes=[SYSTEM_READ])],
    response_model=CreateAssessmentTaskResponse,
)
def create_privacy_assessment(
    request: CreateAssessmentTaskRequest,
    *,
    db: Session = Depends(get_db),
    client: ClientDetail = Security(verify_oauth_client, scopes=[SYSTEM_READ]),
) -> CreateAssessmentTaskResponse:
    """Queue a DPIA generation run.

    Returns as soon as the job is queued — generation over a real estate
    takes minutes, and the UI polls GET /tasks for progress rather than
    holding a request open.

    The commit happens BEFORE apply_async, and the order matters: a worker
    can pick the message up the instant it is published, and an uncommitted
    row means the worker raises "No privacy_assessment_task with id ..." and
    the run is lost before it starts.
    """
    created_by = _created_by_from_client(client)
    task_row_id, celery_id = _create_task(db, request, created_by)
    db.commit()

    generate_assessments.apply_async(
        args=[task_row_id], task_id=celery_id, queue=GENERATION_QUEUE
    )
    logger.info(
        "PrivacyCare generation queued by {}: task {} ({} type(s), use_llm={})",
        created_by,
        task_row_id,
        len(request.assessment_types),
        request.use_llm,
    )

    return CreateAssessmentTaskResponse(
        task_id=task_row_id,
        status="pending",
        message="Assessment generation has been queued.",
    )
