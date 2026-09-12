import uuid

import pytest
import sqlalchemy
from fastapi import HTTPException
from fastapi_pagination import Params, paginate
from sqlalchemy.orm import Session

from fides.api.privacycare.api.schemas import CreateAssessmentTaskRequest
from fides.api.privacycare.api.tasks import (
    _list_tasks,
    _task_detail,
    create_privacy_assessment,
    get_assessment_task,
)
from tests.privacycare.test_api_assessments import (
    _fake_client,
    _seed_assessment,
    _seed_template,
)
from tests.privacycare.test_context import _seed_system
from tests.privacycare.test_tasks import _seed_task

DB_URL = "postgresql://postgres:fides@127.0.0.1:5442/fides"


@pytest.fixture
def db():
    engine = sqlalchemy.create_engine(DB_URL)
    with Session(engine) as session:
        yield session
        session.rollback()


@pytest.fixture
def queued(monkeypatch):
    """Capture apply_async instead of reaching a broker."""
    calls = []

    def _apply_async(*args, **kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(
        "fides.api.privacycare.api.tasks.generate_assessments.apply_async",
        _apply_async,
    )
    return calls


def _task_row(db, task_id):
    return db.execute(
        sqlalchemy.text(
            "SELECT action_type, status, celery_id, assessment_types, "
            "       system_fides_keys, created_by, use_llm, llm_model, "
            "       high_risk_only "
            "FROM privacy_assessment_task WHERE id = :id"
        ),
        {"id": task_id},
    ).mappings().first()


def test_create_writes_a_pending_task_row(db, queued, monkeypatch):
    monkeypatch.setattr(db, "commit", lambda: None)

    response = create_privacy_assessment(
        CreateAssessmentTaskRequest(
            assessment_types=["gdpr_dpia"],
            system_fides_keys=["crm"],
            use_llm=True,
            model="claude-opus-5",
        ),
        db=db,
        client=_fake_client("alice@example.com"),
    )

    row = _task_row(db, response.task_id)
    assert row["action_type"] == "generate"
    assert row["status"] == "pending"
    assert list(row["assessment_types"]) == ["gdpr_dpia"]
    assert list(row["system_fides_keys"]) == ["crm"]
    assert row["use_llm"] is True
    assert row["llm_model"] == "claude-opus-5"


def test_the_celery_id_written_to_the_row_is_the_id_the_job_is_queued_under(
    db, queued, monkeypatch
):
    # celery_id is NOT NULL, so the row cannot be written after the job is
    # queued; and the row must exist before the worker starts or the worker
    # has nothing to read. Generating the id ourselves closes that race. If
    # these two ever diverged, nothing would link a running job back to its
    # row.
    monkeypatch.setattr(db, "commit", lambda: None)

    response = create_privacy_assessment(
        CreateAssessmentTaskRequest(assessment_types=["gdpr_dpia"]),
        db=db,
        client=_fake_client("alice@example.com"),
    )

    assert queued[0]["task_id"] == _task_row(db, response.task_id)["celery_id"]


def test_the_job_carries_the_task_row_id_and_the_queue(db, queued, monkeypatch):
    monkeypatch.setattr(db, "commit", lambda: None)

    response = create_privacy_assessment(
        CreateAssessmentTaskRequest(assessment_types=["gdpr_dpia"]),
        db=db,
        client=_fake_client("alice@example.com"),
    )

    assert queued[0]["args"] == [response.task_id]
    assert queued[0]["queue"] == "fidesplus.privacy_assessments"


def test_the_row_is_committed_before_the_job_is_queued(db, monkeypatch):
    # A worker can pick the message up the instant it is published. If the
    # row is still uncommitted, the worker raises "No privacy_assessment_task
    # with id ..." and the run is lost before it starts.
    order = []
    monkeypatch.setattr(db, "commit", lambda: order.append("commit"))
    monkeypatch.setattr(
        "fides.api.privacycare.api.tasks.generate_assessments.apply_async",
        lambda *a, **k: order.append("queue"),
    )

    create_privacy_assessment(
        CreateAssessmentTaskRequest(assessment_types=["gdpr_dpia"]),
        db=db,
        client=_fake_client("alice@example.com"),
    )

    assert order == ["commit", "queue"]


def test_created_by_comes_from_the_authenticated_client(db, queued, monkeypatch):
    monkeypatch.setattr(db, "commit", lambda: None)

    response = create_privacy_assessment(
        CreateAssessmentTaskRequest(assessment_types=["gdpr_dpia"]),
        db=db,
        client=_fake_client("carol@example.com"),
    )

    assert _task_row(db, response.task_id)["created_by"] == "carol@example.com"


def test_a_machine_client_is_still_named(db, queued, monkeypatch):
    monkeypatch.setattr(db, "commit", lambda: None)

    response = create_privacy_assessment(
        CreateAssessmentTaskRequest(assessment_types=["gdpr_dpia"]),
        db=db,
        client=_fake_client(None, id="api_client_abc123"),
    )

    assert _task_row(db, response.task_id)["created_by"] == "client:api_client_abc123"


def test_an_empty_system_fides_keys_list_is_stored_as_empty_not_null(db, queued, monkeypatch):
    # SQL NULL is how this column says "every system". An empty list is
    # falsy, so `[]` used to be written as NULL — and a caller who
    # explicitly asked for zero systems got a DPIA generated over the whole
    # estate, with nothing logged to say the narrowing had been discarded.
    # The admin UI never sends `[]`, but this is a public API and `[]` is
    # the natural serialisation of "no selection" for any other client.
    monkeypatch.setattr(db, "commit", lambda: None)

    response = create_privacy_assessment(
        CreateAssessmentTaskRequest(assessment_types=["gdpr_dpia"], system_fides_keys=[]),
        db=db,
        client=_fake_client("alice@example.com"),
    )

    assert _task_row(db, response.task_id)["system_fides_keys"] == []


def test_omitting_system_fides_keys_still_means_every_system(db, queued, monkeypatch):
    # The other half of the distinction: absent stays NULL.
    monkeypatch.setattr(db, "commit", lambda: None)

    response = create_privacy_assessment(
        CreateAssessmentTaskRequest(assessment_types=["gdpr_dpia"]),
        db=db,
        client=_fake_client("alice@example.com"),
    )

    assert _task_row(db, response.task_id)["system_fides_keys"] is None


def test_the_task_record_reads_back_an_empty_scope_as_empty(db, queued, monkeypatch):
    # AssessmentTaskResponse must report whichever request was actually
    # made; rendering `[]` as None would relabel "no systems" as "all
    # systems" on the screen a DPO audits.
    monkeypatch.setattr(db, "commit", lambda: None)

    response = create_privacy_assessment(
        CreateAssessmentTaskRequest(assessment_types=["gdpr_dpia"], system_fides_keys=[]),
        db=db,
        client=_fake_client("alice@example.com"),
    )

    detail = _task_detail(db, response.task_id)
    assert detail.system_fides_keys == []
    assert detail.systems == []


def test_an_empty_assessment_types_list_is_rejected_by_the_schema():
    with pytest.raises(ValueError):
        CreateAssessmentTaskRequest(assessment_types=[])


def test_the_response_names_the_task_and_says_it_is_queued(db, queued, monkeypatch):
    monkeypatch.setattr(db, "commit", lambda: None)

    response = create_privacy_assessment(
        CreateAssessmentTaskRequest(assessment_types=["gdpr_dpia"]),
        db=db,
        client=_fake_client("alice@example.com"),
    )

    assert response.status == "pending"
    assert response.message
    assert response.task_id


def test_task_detail_reports_progress_as_a_percentage(db):
    # AssessmentTaskStatusIndicator.tsx renders
    # `Math.round(activeTask.progress)`%, so progress is 0-100, not 0-1. A
    # fraction here renders as "0%" through an entire run.
    task_id = _seed_task(db, assessment_types=["gdpr_dpia"])
    db.execute(
        sqlalchemy.text(
            "UPDATE privacy_assessment_task "
            "SET total_count = 4, completed_count = 1 WHERE id = :id"
        ),
        {"id": task_id},
    )
    db.flush()

    assert _task_detail(db, task_id).progress == 25.0


def test_progress_is_zero_when_nothing_is_counted_yet(db):
    # total_count is 0 until the task starts. Deriving progress must not
    # divide by zero — the Ethyca model's own property guards this.
    task_id = _seed_task(db, assessment_types=["gdpr_dpia"])
    db.flush()

    assert _task_detail(db, task_id).progress == 0.0


def test_task_detail_lists_the_assessments_it_produced(db):
    # There is no assessment_ids column; the field is derived from
    # privacy_assessment.privacy_assessment_task_id.
    task_id = _seed_task(db, assessment_types=["gdpr_dpia"])
    tid = _seed_template(db)
    first = _seed_assessment(db, tid, "A", task_id=task_id)
    second = _seed_assessment(db, tid, "B", task_id=task_id)
    _seed_assessment(db, tid, "Unrelated")
    db.flush()

    assert sorted(_task_detail(db, task_id).assessment_ids) == sorted([first, second])


def test_task_detail_resolves_the_systems_it_was_asked_about(db):
    key = f"sys-{uuid.uuid4().hex[:6]}"
    _seed_system(db, key, name="CRM")
    task_id = _seed_task(db, assessment_types=["gdpr_dpia"], system_fides_keys=[key])
    db.flush()

    systems = _task_detail(db, task_id).systems

    assert [(s.fides_key, s.name) for s in systems] == [(key, "CRM")]


def test_a_system_key_with_no_matching_system_still_appears(db):
    # A system deleted after the task ran must not vanish from the task's
    # record of what it was asked to assess.
    task_id = _seed_task(
        db, assessment_types=["gdpr_dpia"], system_fides_keys=["deleted-system"]
    )
    db.flush()

    systems = _task_detail(db, task_id).systems

    assert [(s.fides_key, s.name) for s in systems] == [("deleted-system", None)]


def test_systems_is_none_when_the_task_targeted_every_system(db):
    task_id = _seed_task(db, assessment_types=["gdpr_dpia"], system_fides_keys=None)
    db.flush()

    detail = _task_detail(db, task_id)

    assert detail.system_fides_keys is None
    assert detail.systems is None


def test_task_detail_404s_for_an_unknown_id(db):
    with pytest.raises(LookupError):
        _task_detail(db, "no-such-task")


def test_the_task_detail_route_maps_an_unknown_id_to_404(db):
    with pytest.raises(HTTPException) as exc_info:
        get_assessment_task("no-such-task", db=db)
    assert exc_info.value.status_code == 404


def test_list_tasks_is_newest_first(db):
    # The UI reads items[0] to find the active task. Oldest-first would make
    # it watch a run that finished last week.
    older = _seed_task(db, assessment_types=["gdpr_dpia"])
    newer = _seed_task(db, assessment_types=["gdpr_dpia"])
    db.execute(
        sqlalchemy.text(
            "UPDATE privacy_assessment_task SET created_at = now() - interval '1 day' "
            "WHERE id = :id"
        ),
        {"id": older},
    )
    db.flush()

    ids = [item.id for item in _list_tasks(db)]

    assert ids.index(newer) < ids.index(older)


def test_list_tasks_filters_by_status(db):
    done = _seed_task(db, assessment_types=["gdpr_dpia"])
    db.execute(
        sqlalchemy.text(
            "UPDATE privacy_assessment_task SET status = 'complete' WHERE id = :id"
        ),
        {"id": done},
    )
    pending = _seed_task(db, assessment_types=["gdpr_dpia"])
    db.flush()

    ids = [item.id for item in _list_tasks(db, status="complete")]

    assert done in ids
    assert pending not in ids


def test_list_tasks_returns_an_items_envelope(db):
    # Page_AssessmentTaskResponse_ is {items,total,page,size,pages}. Three
    # screens in this module have already shipped empty because a route
    # returned a bare list where the UI read `.items`.
    _seed_task(db, assessment_types=["gdpr_dpia"])
    db.flush()

    page = paginate(_list_tasks(db), Params(page=1, size=50))

    assert hasattr(page, "items")
    assert page.total >= 1


def test_the_tasks_route_is_matched_before_the_assessment_id_route():
    # /tasks would otherwise be swallowed by /{assessment_id}, and the
    # progress bar would 404 forever against a route that exists.
    #
    # route.path on this router carries the full "/plus/privacy-assessments"
    # prefix (Fides' APIRouter subclass applies it at add_api_route time,
    # confirmed by inspection — it is not the bare suffix passed to
    # @privacycare_router.get), so the paths compared here must match that.
    from fides.api.privacycare.api.router import privacycare_router

    paths = [route.path for route in privacycare_router.routes]
    assert paths.index("/plus/privacy-assessments/tasks") < paths.index(
        "/plus/privacy-assessments/{assessment_id}"
    )
