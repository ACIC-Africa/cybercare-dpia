import uuid

import pytest
import sqlalchemy
from fastapi import HTTPException
from sqlalchemy.orm import Session

from fides.api.privacycare.api.schemas import CreateAssessmentTaskRequest
from fides.api.privacycare.api.tasks import create_privacy_assessment
from tests.privacycare.test_api_assessments import _fake_client

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
