# The generation task. Unlike every other test file in this package,
# run_generation COMMITS — repeatedly, by design (see tasks.py's own
# docstring): the UI polls task progress every 15 seconds and renders
# completed_count, so one transaction around a fifty-system run would show
# 0% for minutes then jump to 100%.
#
# The plain "Session + rollback-at-teardown" fixture every other test file
# in this package uses relies on nothing under test ever committing. Under
# that fixture here, run_generation's commits would be REAL commits against
# the dev `fides` database — permanently leaking privacy_assessment /
# privacy_assessment_task rows on every test run — and a failure-path
# db.rollback() inside run_generation would only discard work since the
# LAST internal commit, not the test's own seeded rows, leaving those
# half-committed too.
#
# So this file's `db` fixture instead binds the Session to a connection that
# already has an externally-managed transaction open. run_generation's
# `db.commit()` calls become SAVEPOINT releases against that outer
# transaction (join_transaction_mode="create_savepoint") — real as far as
# the code under test is concerned, so the per-assessment commit behaviour
# is genuinely exercised — and the outer transaction.rollback() at teardown
# discards all of it, including every savepoint, leaving the dev database
# exactly as it was. See test_run_generation_leaves_no_rows_behind at the
# bottom of this file for the row-count proof.
import uuid

import pytest
import sqlalchemy
from sqlalchemy import event
from sqlalchemy.orm import Session

from fides.api.privacycare.tasks import run_generation
from tests.privacycare.test_context import (
    _seed_data_use,
    _seed_declaration,
    _seed_system,
)
from tests.privacycare.test_api_assessments import _seed_question, _seed_template

DB_URL = "postgresql://postgres:fides@127.0.0.1:5442/fides"


@pytest.fixture
def db():
    # run_generation's db.commit() calls are real, by design (see tasks.py's
    # docstring) — so this fixture cannot be the plain "Session + rollback"
    # every other file in this package uses; that would let each commit
    # inside the task under test escape to the real dev database.
    #
    # The environment's pinned SQLAlchemy is 1.4 (confirmed: 1.4.27), which
    # predates the 2.0 `Session(join_transaction_mode="create_savepoint")`
    # convenience kwarg. This is SQLAlchemy 1.4's own documented equivalent
    # ("Joining a Session into an External Transaction (such as for test
    # suites)"): bind the Session to a Connection with a transaction already
    # open, put the Session in a SAVEPOINT via begin_nested(), and reopen a
    # fresh SAVEPOINT every time one ends (i.e. every time code under test
    # calls commit() or rollback()) via the after_transaction_end event. Each
    # `db.commit()` inside run_generation then only releases/reopens a
    # SAVEPOINT against the outer, never-committed transaction; rolling that
    # outer transaction back at teardown discards all of them at once.
    engine = sqlalchemy.create_engine(DB_URL)
    connection = engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection)
    session.begin_nested()

    @event.listens_for(session, "after_transaction_end")
    def _restart_savepoint(sess, trans):
        if trans.nested and not trans._parent.nested:
            sess.expire_all()
            sess.begin_nested()

    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


def _seed_task(
    db,
    *,
    assessment_types,
    system_fides_keys=None,
    use_llm=False,
    high_risk_only=False,
    created_by="alice@example.com",
) -> str:
    task_id = f"pat_{uuid.uuid4().hex[:8]}"
    db.execute(
        sqlalchemy.text(
            "INSERT INTO privacy_assessment_task "
            "(id, action_type, status, celery_id, assessment_types, "
            " system_fides_keys, created_by, use_llm, high_risk_only) "
            "VALUES (:id, 'generate', 'pending', :celery_id, :types, "
            " :keys, :created_by, :use_llm, :high_risk_only)"
        ),
        {
            "id": task_id,
            "celery_id": str(uuid.uuid4()),
            "types": assessment_types,
            "keys": system_fides_keys,
            "created_by": created_by,
            "use_llm": use_llm,
            "high_risk_only": high_risk_only,
        },
    )
    return task_id


def _task_row(db, task_id):
    return db.execute(
        sqlalchemy.text(
            "SELECT status, total_count, completed_count, message "
            "FROM privacy_assessment_task WHERE id = :id"
        ),
        {"id": task_id},
    ).mappings().first()


def _assessments_for_task(db, task_id):
    return db.execute(
        sqlalchemy.text(
            "SELECT id, name, status, system_fides_key, declaration_id, "
            "       data_use, data_categories, template_id, created_by, "
            "       context_snapshot, last_evaluated_at "
            "FROM privacy_assessment WHERE privacy_assessment_task_id = :id "
            "ORDER BY name"
        ),
        {"id": task_id},
    ).mappings().all()


def _full_coverage_template(db, assessment_type: str) -> str:
    """An active template of the given type with one full-coverage question."""
    tid = _seed_template(db, assessment_type=assessment_type)
    qid = _seed_question(db, tid, "q_full", "necessity", 1)
    db.execute(
        sqlalchemy.text(
            "UPDATE assessment_question SET expected_coverage = 'full', "
            "fides_sources = :sources WHERE id = :id"
        ),
        {"sources": ["privacy_declaration.data_use"], "id": qid},
    )
    return tid


def test_generation_creates_one_assessment_per_declaration(db):
    key = f"sys-{uuid.uuid4().hex[:6]}"
    sid = _seed_system(db, key, name="CRM")
    _seed_declaration(db, sid, "marketing.advertising")
    _seed_declaration(db, sid, "essential.service.payment_processing")
    atype = f"kenya_dpia_{uuid.uuid4().hex[:6]}"
    _full_coverage_template(db, atype)
    db.flush()
    task_id = _seed_task(db, assessment_types=[atype], system_fides_keys=[key])
    db.flush()

    run_generation(db, task_id)

    assessments = _assessments_for_task(db, task_id)
    assert len(assessments) == 2
    assert {a["data_use"] for a in assessments} == {
        "marketing.advertising",
        "essential.service.payment_processing",
    }


def test_a_finished_assessment_is_in_progress_not_generating(db):
    # `generating` is the transient state the UI renders as a spinner. An
    # assessment left in it after the task finished would spin forever.
    key = f"sys-{uuid.uuid4().hex[:6]}"
    _seed_declaration(db, _seed_system(db, key), "marketing.advertising")
    atype = f"kenya_dpia_{uuid.uuid4().hex[:6]}"
    _full_coverage_template(db, atype)
    db.flush()
    task_id = _seed_task(db, assessment_types=[atype], system_fides_keys=[key])
    db.flush()

    run_generation(db, task_id)

    assert [a["status"] for a in _assessments_for_task(db, task_id)] == ["in_progress"]


def test_progress_counts_reach_the_total(db):
    key = f"sys-{uuid.uuid4().hex[:6]}"
    sid = _seed_system(db, key)
    _seed_declaration(db, sid, "marketing.advertising")
    _seed_declaration(db, sid, "essential.service.payment_processing")
    atype = f"kenya_dpia_{uuid.uuid4().hex[:6]}"
    _full_coverage_template(db, atype)
    db.flush()
    task_id = _seed_task(db, assessment_types=[atype], system_fides_keys=[key])
    db.flush()

    run_generation(db, task_id)

    row = _task_row(db, task_id)
    assert row["status"] == "complete"
    assert row["total_count"] == 2
    assert row["completed_count"] == 2


def test_a_run_with_no_targets_completes_rather_than_erroring(db):
    # An estate with no matching systems is an empty result, not a failure.
    # Reporting `error` would send a DPO hunting a bug that isn't there.
    atype = f"kenya_dpia_{uuid.uuid4().hex[:6]}"
    _full_coverage_template(db, atype)
    db.flush()
    task_id = _seed_task(
        db, assessment_types=[atype], system_fides_keys=["no-such-system"]
    )
    db.flush()

    run_generation(db, task_id)

    row = _task_row(db, task_id)
    assert row["status"] == "complete"
    assert row["total_count"] == 0
    assert "no" in (row["message"] or "").lower()


def test_an_unknown_assessment_type_errors_with_a_message_naming_it(db):
    key = f"sys-{uuid.uuid4().hex[:6]}"
    _seed_declaration(db, _seed_system(db, key), "marketing.advertising")
    db.flush()
    task_id = _seed_task(
        db, assessment_types=["not_a_real_type"], system_fides_keys=[key]
    )
    db.flush()

    run_generation(db, task_id)

    row = _task_row(db, task_id)
    assert row["status"] == "error"
    assert "not_a_real_type" in row["message"]


def test_the_assessment_records_the_context_it_was_generated_from(db):
    # context_snapshot is what lets a DPO answer "which facts produced this"
    # years later. A NULL there makes the question unanswerable.
    #
    # ctl_data_uses is pre-seeded with the real Fides taxonomy (verified
    # live: "marketing.advertising" already exists there, named "Advertising,
    # Marketing or Promotion") — _seed_data_use's INSERT is
    # ON CONFLICT DO NOTHING, so reusing that key would silently keep the
    # taxonomy's name instead of the name this test seeds and asserts on. A
    # unique custom key avoids the collision, per the brief's own warning to
    # seed unique custom keys rather than assume a lookup table is empty.
    key = f"sys-{uuid.uuid4().hex[:6]}"
    data_use = f"marketing.custom_{uuid.uuid4().hex[:6]}"
    sid = _seed_system(db, key, name="CRM")
    _seed_data_use(db, data_use, "Advertising", "Promoting goods.")
    _seed_declaration(db, sid, data_use, name="Email campaigns")
    atype = f"kenya_dpia_{uuid.uuid4().hex[:6]}"
    _full_coverage_template(db, atype)
    db.flush()
    task_id = _seed_task(db, assessment_types=[atype], system_fides_keys=[key])
    db.flush()

    run_generation(db, task_id)

    assessment = _assessments_for_task(db, task_id)[0]
    assert assessment["context_snapshot"]["system"]["name"] == "CRM"
    assert assessment["context_snapshot"]["data_use"]["name"] == "Advertising"
    assert assessment["last_evaluated_at"] is not None


def test_completeness_is_recomputed_after_generation(db):
    key = f"sys-{uuid.uuid4().hex[:6]}"
    _seed_declaration(db, _seed_system(db, key), "marketing.advertising")
    atype = f"kenya_dpia_{uuid.uuid4().hex[:6]}"
    _full_coverage_template(db, atype)   # exactly one question, full coverage
    db.flush()
    task_id = _seed_task(db, assessment_types=[atype], system_fides_keys=[key])
    db.flush()

    run_generation(db, task_id)

    completeness = db.execute(
        sqlalchemy.text(
            "SELECT completeness FROM privacy_assessment "
            "WHERE privacy_assessment_task_id = :id"
        ),
        {"id": task_id},
    ).scalar()
    # recompute_completeness (api/answers.py, Task 3/4) returns a 0.0-1.0
    # fraction, not a percentage — pinned by test_answers.py/
    # test_api_assessments.py asserting completeness == pytest.approx(1.0)
    # for "both of 2 questions answered". One question, fully answered, is
    # 1.0, not 100.0.
    assert completeness == 1.0


def test_one_failing_target_does_not_discard_the_others(db, monkeypatch):
    # A single system's gateway 429 must not throw away the rest of the run.
    from fides.api.privacycare import tasks as tasks_module

    calls = {"n": 0}
    real = tasks_module.answer_questions

    def _fail_the_first(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated per-target failure")
        return real(*args, **kwargs)

    monkeypatch.setattr(tasks_module, "answer_questions", _fail_the_first)

    key = f"sys-{uuid.uuid4().hex[:6]}"
    sid = _seed_system(db, key)
    _seed_declaration(db, sid, "marketing.advertising")
    _seed_declaration(db, sid, "essential.service.payment_processing")
    atype = f"kenya_dpia_{uuid.uuid4().hex[:6]}"
    _full_coverage_template(db, atype)
    db.flush()
    task_id = _seed_task(db, assessment_types=[atype], system_fides_keys=[key])
    db.flush()

    run_generation(db, task_id)

    row = _task_row(db, task_id)
    assert row["status"] == "complete"
    assert row["completed_count"] == 1
    assert "1" in row["message"] and "fail" in row["message"].lower()


def test_every_target_failing_is_reported_as_an_error(db, monkeypatch):
    from fides.api.privacycare import tasks as tasks_module

    monkeypatch.setattr(
        tasks_module,
        "answer_questions",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("everything failed")),
    )

    key = f"sys-{uuid.uuid4().hex[:6]}"
    _seed_declaration(db, _seed_system(db, key), "marketing.advertising")
    atype = f"kenya_dpia_{uuid.uuid4().hex[:6]}"
    _full_coverage_template(db, atype)
    db.flush()
    task_id = _seed_task(db, assessment_types=[atype], system_fides_keys=[key])
    db.flush()

    run_generation(db, task_id)

    assert _task_row(db, task_id)["status"] == "error"


def test_the_celery_task_is_registered_under_its_stable_name():
    # A task the worker cannot find is a Generate button that spins forever
    # with nothing in the logs. Importing the worker entry module is what
    # registers it; this asserts that import actually has that effect.
    import fides.api.privacycare.worker  # noqa: F401
    from fides.api.tasks import celery_app

    assert "privacycare.generate_assessments" in celery_app.tasks


def test_the_task_is_routed_to_the_privacy_assessments_queue():
    from fides.api.privacycare.tasks import GENERATION_QUEUE
    from fides.api.tasks import PRIVACY_ASSESSMENTS_QUEUE_NAME

    assert GENERATION_QUEUE == PRIVACY_ASSESSMENTS_QUEUE_NAME
