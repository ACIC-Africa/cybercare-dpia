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
# exactly as it was. (The row-count proof this claim rests on was run
# manually, outside this suite — see task-5-report.md — rather than as a
# test in this file, since a passing assertion here would only prove
# isolation held for THIS run, not across the full-suite run the report
# actually measured.)
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


def test_progress_is_visible_mid_run_not_only_at_the_end(db, monkeypatch):
    # MINOR (fix round 1): test_progress_counts_reach_the_total above only
    # checks start/end state, and would pass even if progress were written
    # once at the very end of the run. Writing it incrementally, after every
    # assessment, is the entire reason run_generation commits in a loop
    # instead of once (see its own docstring: the UI polls every 15s and
    # renders completed_count). This reads completed_count off the task's
    # own row at each answer_questions call, mid-run, and asserts it climbs
    # one at a time rather than jumping straight from 0 to the total.
    from fides.api.privacycare import tasks as tasks_module

    real_answer_questions = tasks_module.answer_questions
    observed_counts = []

    def _observe_progress(db_, assessment_id, context, **kwargs):
        observed_counts.append(_task_row(db_, task_id)["completed_count"])
        return real_answer_questions(db_, assessment_id, context, **kwargs)

    monkeypatch.setattr(tasks_module, "answer_questions", _observe_progress)

    key = f"sys-{uuid.uuid4().hex[:6]}"
    sid = _seed_system(db, key)
    _seed_declaration(db, sid, "marketing.advertising")
    _seed_declaration(db, sid, "essential.service.payment_processing")
    _seed_declaration(db, sid, "essential.service")
    atype = f"kenya_dpia_{uuid.uuid4().hex[:6]}"
    _full_coverage_template(db, atype)
    db.flush()
    task_id = _seed_task(db, assessment_types=[atype], system_fides_keys=[key])
    db.flush()

    run_generation(db, task_id)

    assert observed_counts == [0, 1, 2], (
        "completed_count must be visible climbing one assessment at a time "
        "mid-run, not only readable once the run has already finished"
    )
    assert _task_row(db, task_id)["completed_count"] == 3


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


def test_a_context_build_failure_does_not_discard_the_others(db, monkeypatch):
    # Fix round 1 (coordinator review, MAJOR finding): build_context used to
    # be called OUTSIDE the per-target try/except, so an exception from it
    # aborted the whole run — every other target's assessments were never
    # attempted, and the task row was never finished (stuck `in_processing`
    # forever, the UI polling a run that would never report). This pins
    # that build_context failing for ONE target still lets every other
    # target complete, and the run still finishes and names the failure.
    from fides.api.privacycare import tasks as tasks_module

    bad_key = f"sys-{uuid.uuid4().hex[:6]}"
    good_key = f"sys-{uuid.uuid4().hex[:6]}"
    real_build_context = tasks_module.build_context

    def _fail_for_bad_target(db_, target):
        if target.system_fides_key == bad_key:
            raise RuntimeError("simulated context-build failure")
        return real_build_context(db_, target)

    monkeypatch.setattr(tasks_module, "build_context", _fail_for_bad_target)

    _seed_declaration(db, _seed_system(db, bad_key), "marketing.advertising")
    _seed_declaration(db, _seed_system(db, good_key), "marketing.advertising")
    atype = f"kenya_dpia_{uuid.uuid4().hex[:6]}"
    _full_coverage_template(db, atype)
    db.flush()
    task_id = _seed_task(
        db, assessment_types=[atype], system_fides_keys=[bad_key, good_key]
    )
    db.flush()

    run_generation(db, task_id)

    row = _task_row(db, task_id)
    assert row["status"] == "complete", "the run must finish, not hang in_processing"
    assert row["total_count"] == 2
    assert row["completed_count"] == 1
    assert bad_key in row["message"], "the message must name which target failed"
    assert _assessments_for_task(db, task_id)[0]["system_fides_key"] == good_key


def test_a_wrapper_level_failure_preserves_the_real_progress_count(db):
    # Fix round 1 (coordinator review, MAJOR finding): the Celery wrapper's
    # except path used to call _finish(..., "error", 0, 0, ...)
    # unconditionally, overwriting whatever total_count/completed_count
    # run_generation had already committed with zero — a run that produced
    # forty assessments before dying would report having produced none.
    # _fail_task is the extracted, directly-testable version of that path;
    # this pins that it reads the row's own last-committed counts rather
    # than assuming zero.
    from fides.api.privacycare.tasks import _fail_task

    task_id = _seed_task(db, assessment_types=["irrelevant"])
    db.flush()
    # Simulate run_generation having made real progress and committed it
    # before some later, unrelated exception escaped to the wrapper.
    db.execute(
        sqlalchemy.text(
            "UPDATE privacy_assessment_task "
            "SET status = 'in_processing', total_count = 40, completed_count = 37 "
            "WHERE id = :id"
        ),
        {"id": task_id},
    )
    db.commit()

    _fail_task(db, task_id, RuntimeError("connection dropped mid-run"))

    row = _task_row(db, task_id)
    assert row["status"] == "error"
    assert row["total_count"] == 40, "must not be clobbered to 0"
    assert row["completed_count"] == 37, "must not be clobbered to 0"
    assert "connection dropped mid-run" in row["message"]


def test_the_celery_task_is_registered_under_its_stable_name():
    # A task the worker cannot find is a Generate button that spins forever
    # with nothing in the logs. Importing the worker entry module is what
    # registers it; this asserts that import actually has that effect.
    import fides.api.privacycare.worker  # noqa: F401
    from fides.api.tasks import celery_app

    assert "privacycare.generate_assessments" in celery_app.tasks


def test_the_task_is_routed_to_the_privacy_assessments_queue():
    # This only pins that our constant is the same object as Ethyca's — it
    # does not prove a queued message actually lands on that queue. The real
    # routing assertion is at the call site that matters: the next task's
    # `.apply_async(queue=GENERATION_QUEUE)` (Task 6), which is what
    # actually stamps the queue name onto the message the worker consumes.
    from fides.api.privacycare.tasks import GENERATION_QUEUE
    from fides.api.tasks import PRIVACY_ASSESSMENTS_QUEUE_NAME

    assert GENERATION_QUEUE == PRIVACY_ASSESSMENTS_QUEUE_NAME
