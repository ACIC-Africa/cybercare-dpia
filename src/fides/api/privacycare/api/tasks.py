"""Generation task routes: queue a run, then watch it."""
import uuid
from typing import Optional

import sqlalchemy
from fastapi import Depends, HTTPException, Security
from fastapi import status as status_codes
from fastapi_pagination import Page, Params, paginate
from loguru import logger
from sqlalchemy.orm import Session

from fides.api.deps import get_db
from fides.api.models.client import ClientDetail
from fides.api.oauth.utils import verify_oauth_client
from fides.api.privacycare.api.router import privacycare_router
from fides.api.privacycare.api.schemas import (
    AssessmentTaskResponse,
    AssessmentTaskSystemInfo,
    CreateAssessmentTaskRequest,
    CreateAssessmentTaskResponse,
)
from fides.api.privacycare.tasks import GENERATION_QUEUE, generate_assessments
from fides.common.scope_registry import SYSTEM_READ

# `status` is both a query-parameter name on GET /tasks below and the name
# assessments.py imports from fastapi for HTTP status codes. Importing it
# here as `status_codes` means the query parameter (a plain str) can never
# shadow the module the 404 branch needs — a shadowed `status` would only
# raise `AttributeError: 'str' object has no attribute 'HTTP_404_NOT_FOUND'`
# on the 404 path, which is the path least likely to be exercised by hand.

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
    # Imported here, not at module level: a top-level
    # `from ...assessments import _created_by_from_client` would force
    # api.assessments to import (and bind ALL its routes, including GET
    # /{assessment_id}) the instant this module is imported — before this
    # module's own GET /tasks route is defined. router.register() relies on
    # importing api.tasks before api.assessments to get /tasks registered
    # first; a module-level import here would silently defeat that.
    from fides.api.privacycare.api.assessments import _created_by_from_client

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


_TASK_COLUMNS = (
    "id, action_type, status, total_count, completed_count, message, "
    "assessment_types, system_fides_keys, created_by, use_llm, llm_model, "
    "high_risk_only, created_at, updated_at"
)

_TASK_BY_ID_SQL = sqlalchemy.text(
    f"SELECT {_TASK_COLUMNS} FROM privacy_assessment_task WHERE id = :task_id"
)

_ASSESSMENT_IDS_SQL = sqlalchemy.text(
    "SELECT id FROM privacy_assessment "
    "WHERE privacy_assessment_task_id = :task_id ORDER BY name, id"
)

_SYSTEM_NAMES_SQL = sqlalchemy.text(
    "SELECT fides_key, name FROM ctl_systems WHERE fides_key = ANY(:fides_keys)"
)


def _progress(total_count: int, completed_count: int) -> float:
    """Reproduces PrivacyAssessmentTask.progress (Ethyca's model, line 134):
    round((completed / total) * 100, 1), zero when total is zero.

    Derived rather than stored — there is no progress column. Reproducing
    the formula exactly matters because the same number is rendered by
    AssessmentTaskStatusIndicator.tsx as a percentage; a second definition
    that rounded differently would make two screens disagree about one run.
    """
    if not total_count:
        return 0.0
    return round((completed_count / total_count) * 100, 1)


def _task_response(db: Session, row) -> AssessmentTaskResponse:
    assessment_ids = list(
        db.execute(_ASSESSMENT_IDS_SQL, {"task_id": row["id"]}).scalars().all()
    )

    systems = None
    if row["system_fides_keys"]:
        keys = list(row["system_fides_keys"])
        named = {
            r["fides_key"]: r["name"]
            for r in db.execute(_SYSTEM_NAMES_SQL, {"fides_keys": keys})
            .mappings()
            .all()
        }
        # Every requested key is returned, named or not. A system deleted
        # after the run must not vanish from the task's record of what it
        # was asked to assess.
        systems = [
            AssessmentTaskSystemInfo(fides_key=key, name=named.get(key))
            for key in keys
        ]

    return AssessmentTaskResponse(
        id=row["id"],
        action_type=row["action_type"],
        status=row["status"],
        total_count=row["total_count"],
        completed_count=row["completed_count"],
        progress=_progress(row["total_count"], row["completed_count"]),
        message=row["message"],
        assessment_types=list(row["assessment_types"] or []),
        system_fides_keys=(
            list(row["system_fides_keys"]) if row["system_fides_keys"] else None
        ),
        systems=systems,
        created_by=row["created_by"],
        use_llm=row["use_llm"],
        llm_model=row["llm_model"],
        high_risk_only=row["high_risk_only"],
        assessment_ids=assessment_ids,
        created_at=row["created_at"].isoformat() if row["created_at"] else None,
        updated_at=row["updated_at"].isoformat() if row["updated_at"] else None,
    )


def _task_detail(db: Session, task_id: str) -> AssessmentTaskResponse:
    row = db.execute(_TASK_BY_ID_SQL, {"task_id": task_id}).mappings().first()
    if row is None:
        raise LookupError(f"No assessment task with id {task_id}")
    return _task_response(db, row)


def _list_tasks(
    db: Session, *, status: Optional[str] = None
) -> list[AssessmentTaskResponse]:
    """Newest first. AssessmentTaskStatusIndicator.tsx scans the list for an
    active task and reads the most recent completed one for its toast, so
    oldest-first would make it watch a run that finished last week.
    """
    sql = f"SELECT {_TASK_COLUMNS} FROM privacy_assessment_task WHERE TRUE"
    query_params: dict = {}
    if status:
        sql += " AND status = :status"
        query_params["status"] = status
    sql += " ORDER BY created_at DESC, id DESC"

    rows = db.execute(sqlalchemy.text(sql), query_params).mappings().all()
    return [_task_response(db, row) for row in rows]


@privacycare_router.get(
    "/tasks",
    dependencies=[Security(verify_oauth_client, scopes=[SYSTEM_READ])],
    response_model=Page[AssessmentTaskResponse],
)
def get_assessment_tasks(
    *,
    db: Session = Depends(get_db),
    params: Params = Depends(),
    status: Optional[str] = None,
) -> Page[AssessmentTaskResponse]:
    # Page_AssessmentTaskResponse_ is {items,total,page,size,pages}, and
    # AssessmentTaskStatusIndicator.tsx reads .items. A bare list here is the
    # same envelope mistake that shipped three empty screens in plan 03b.
    return paginate(_list_tasks(db, status=status), params)


@privacycare_router.get(
    "/tasks/{task_id}",
    dependencies=[Security(verify_oauth_client, scopes=[SYSTEM_READ])],
    response_model=AssessmentTaskResponse,
)
def get_assessment_task(
    task_id: str, *, db: Session = Depends(get_db)
) -> AssessmentTaskResponse:
    try:
        return _task_detail(db, task_id)
    except LookupError:
        raise HTTPException(
            status_code=status_codes.HTTP_404_NOT_FOUND,
            detail=f"No assessment task with id {task_id}",
        )
