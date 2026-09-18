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

from fides.api.privacycare.llm import DEFAULT_MODEL, GatewayUnavailable
from fides.api.privacycare.tasks import run_generation
from tests.privacycare.test_api_assessments import _seed_question, _seed_template
from tests.privacycare.test_context import (
    _seed_data_use,
    _seed_declaration,
    _seed_system,
)
from tests.privacycare.test_settings import _set_model_overrides

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
    llm_model=None,
) -> str:
    task_id = f"pat_{uuid.uuid4().hex[:8]}"
    db.execute(
        sqlalchemy.text(
            "INSERT INTO privacy_assessment_task "
            "(id, action_type, status, celery_id, assessment_types, "
            " system_fides_keys, created_by, use_llm, high_risk_only, "
            " llm_model) "
            "VALUES (:id, 'generate', 'pending', :celery_id, :types, "
            " :keys, :created_by, :use_llm, :high_risk_only, :llm_model)"
        ),
        {
            "id": task_id,
            "celery_id": str(uuid.uuid4()),
            "types": assessment_types,
            "keys": system_fides_keys,
            "created_by": created_by,
            "use_llm": use_llm,
            "high_risk_only": high_risk_only,
            "llm_model": llm_model,
        },
    )
    return task_id


def _timestamps(db, task_id):
    return (
        db.execute(
            sqlalchemy.text(
                "SELECT created_at, updated_at FROM privacy_assessment_task "
                "WHERE id = :id"
            ),
            {"id": task_id},
        )
        .mappings()
        .first()
    )


def _task_row(db, task_id):
    return (
        db.execute(
            sqlalchemy.text(
                "SELECT status, total_count, completed_count, message "
                "FROM privacy_assessment_task WHERE id = :id"
            ),
            {"id": task_id},
        )
        .mappings()
        .first()
    )


def _assessments_for_task(db, task_id):
    return (
        db.execute(
            sqlalchemy.text(
                "SELECT id, name, status, system_fides_key, declaration_id, "
                "       data_use, data_categories, template_id, created_by, "
                "       context_snapshot, last_evaluated_at "
                "FROM privacy_assessment WHERE privacy_assessment_task_id = :id "
                "ORDER BY name"
            ),
            {"id": task_id},
        )
        .mappings()
        .all()
    )


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


def _partial_coverage_template(db, assessment_type: str) -> str:
    """An active template of the given type with one `partial`-coverage
    question whose only fides_source resolves against a seeded system, so
    draft_with_llm is actually reached rather than short-circuited by "no
    facts to ground an answer in"."""
    tid = _seed_template(db, assessment_type=assessment_type)
    qid = _seed_question(db, tid, "q_partial", "necessity", 1)
    db.execute(
        sqlalchemy.text(
            "UPDATE assessment_question SET expected_coverage = 'partial', "
            "fides_sources = :sources WHERE id = :id"
        ),
        {"sources": ["system.name"], "id": qid},
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


def _captured_generation_model(db, monkeypatch, **task_kwargs) -> str:
    """Run one generation and return the model answer_questions was given.

    Captured at the boundary the review found broken: tasks.run_generation
    used to hand answer_questions `task["llm_model"]` directly, so the
    configured override could never reach the gateway however it was set.
    """
    key = f"sys-{uuid.uuid4().hex[:6]}"
    _seed_declaration(db, _seed_system(db, key, name="CRM"), "marketing.advertising")
    atype = f"kenya_dpia_{uuid.uuid4().hex[:6]}"
    _full_coverage_template(db, atype)
    db.flush()
    task_id = _seed_task(
        db, assessment_types=[atype], system_fides_keys=[key], **task_kwargs
    )
    db.flush()

    captured = {}

    def _capture(db_, assessment_id, context, *, use_llm, model):
        captured["model"] = model
        return 0

    monkeypatch.setattr("fides.api.privacycare.tasks.answer_questions", _capture)
    run_generation(db, task_id)

    assert "model" in captured, "answer_questions was never called"
    return captured["model"]


def test_generation_uses_the_model_the_settings_screen_configured(db, monkeypatch):
    # The finding: an officer set assessment_model_override and generation
    # carried on calling llm.DEFAULT_MODEL, because nothing outside
    # api/config.py ever read the config row.
    _set_model_overrides(db, assessment="claude-opus-5")

    assert _captured_generation_model(db, monkeypatch) == "claude-opus-5"


def test_a_task_that_chose_its_own_model_is_not_overridden_by_the_config(
    db, monkeypatch
):
    # Rung 1: the officer who started THIS run picked a model for it.
    _set_model_overrides(db, assessment="claude-opus-5")

    model = _captured_generation_model(db, monkeypatch, llm_model="claude-haiku-5")

    assert model == "claude-haiku-5"


def test_generation_falls_back_to_the_platform_default_with_no_override(
    db, monkeypatch
):
    _set_model_overrides(db, assessment=None)

    assert _captured_generation_model(db, monkeypatch) == DEFAULT_MODEL


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


def test_an_empty_system_scope_generates_nothing_rather_than_everything(db):
    # NULL system_fides_keys means "every system"; an empty array means
    # "none". Reading the second as the first ran a caller's explicit
    # narrowing over the entire estate.
    key = f"sys-{uuid.uuid4().hex[:6]}"
    _seed_declaration(db, _seed_system(db, key), "marketing.advertising")
    atype = f"kenya_dpia_{uuid.uuid4().hex[:6]}"
    _full_coverage_template(db, atype)
    db.flush()
    task_id = _seed_task(db, assessment_types=[atype], system_fides_keys=[])
    db.flush()

    run_generation(db, task_id)

    row = _task_row(db, task_id)
    assert row["status"] == "complete"
    assert row["total_count"] == 0
    assert _assessments_for_task(db, task_id) == []


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
    _full_coverage_template(db, atype)  # exactly one question, full coverage
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
    # 100.0, not 1.0 (fix round 1, coordinator review): recompute_
    # completeness returns a 0-100 percentage — see its own docstring in
    # api/answers.py. This assertion was WRONG in the previous round (it
    # read 1.0, on the mistaken belief that recompute_completeness returned
    # a 0.0-1.0 fraction, and "corrected" the brief's original 100.0 to
    # match that mistaken belief). The brief's 100.0 was right all along;
    # recompute_completeness was the one that was wrong, and is now fixed
    # to match its own consumer (AssessmentCard.tsx).
    assert completeness == 100.0


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


def test_a_run_where_the_gateway_is_down_for_every_question_says_so(db, monkeypatch):
    # The eighth silent-degradation defect in this workstream: a run in
    # which the gateway failed for EVERY partial-coverage question used to
    # report `status=complete` with a message that looked identical to a
    # healthy run that drafted every answer for real. answer_questions
    # already swallows GatewayUnavailable per question (draft_with_llm
    # writes nothing and returns None; see that module's own docstrings for
    # why that stays true) and the failure never propagates as an
    # exception, so it never reached `failures` and never touched the
    # message — nothing here surfaced it above a WARNING log line.
    #
    # Proof this test actually exercises the fix: it fails against
    # tasks.py before run_generation accumulates and reports
    # gateway_unavailable (verified by running this test against the
    # pre-fix revision — see docs/demo/generation-gateway-report.md).
    monkeypatch.setattr(
        "fides.api.privacycare.generator.complete",
        lambda *a, **k: (_ for _ in ()).throw(
            GatewayUnavailable("gateway returned 502: bad gateway")
        ),
    )

    key = f"sys-{uuid.uuid4().hex[:6]}"
    sid = _seed_system(db, key, name="CRM")
    _seed_declaration(db, sid, "marketing.advertising")
    _seed_declaration(db, sid, "essential.service.payment_processing")
    atype = f"kenya_dpia_{uuid.uuid4().hex[:6]}"
    _partial_coverage_template(db, atype)
    db.flush()
    task_id = _seed_task(
        db, assessment_types=[atype], system_fides_keys=[key], use_llm=True
    )
    db.flush()

    run_generation(db, task_id)

    row = _task_row(db, task_id)
    # Not a hard failure: the record-derived work these assessments could
    # have carried is real and useful, and a `status=error` would throw it
    # away. Both assessments still complete, just with nothing AI-drafted.
    assert row["status"] == "complete"
    assert row["total_count"] == 2
    assert row["completed_count"] == 2
    message = row["message"]
    assert "AI drafting was unavailable" in message, message
    assert "2 question" in message, message
    assert "read directly from the records" in message, message
    # Plain language: a privacy officer reading this does not know what a
    # gateway is.
    assert "gateway" not in message.lower(), message
    assert "GatewayUnavailable" not in message, message


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


def test_a_status_change_moves_updated_at_off_created_at(db):
    # privacy_assessment_task is only ever written through raw
    # sqlalchemy.text(), which bypasses Base.updated_at's ORM-level
    # onupdate=func.now(); the column's only database-level default is now()
    # at INSERT, and there is no trigger. So updated_at used to equal
    # created_at for every task in every state — and it is the ONLY record
    # of when a generation run ended, rendered by
    # AssessmentTaskStatusIndicator.tsx as the run's finish time.
    from fides.api.privacycare.tasks import _set_status

    task_id = _seed_task(db, assessment_types=["kenya_dpia"])
    db.flush()
    before = _timestamps(db, task_id)
    assert before["updated_at"] == before["created_at"], (
        "precondition: a freshly inserted task has not been updated yet"
    )

    _set_status(db, task_id, "in_processing", 4, 0, None)
    db.flush()

    after = _timestamps(db, task_id)
    assert after["updated_at"] > after["created_at"]
    assert after["created_at"] == before["created_at"], "created_at must not move"


def test_every_later_status_write_moves_updated_at_again(db):
    # The finish write is the one the UI reads as "this run ended". It must
    # be later than the in_processing write, not merely later than INSERT.
    from fides.api.privacycare.tasks import _set_status

    task_id = _seed_task(db, assessment_types=["kenya_dpia"])
    db.flush()

    _set_status(db, task_id, "in_processing", 4, 0, None)
    db.flush()
    mid = _timestamps(db, task_id)["updated_at"]

    _set_status(db, task_id, "complete", 4, 4, "Generated 4 of 4 assessments.")
    db.flush()

    assert _timestamps(db, task_id)["updated_at"] > mid


def test_a_real_run_leaves_a_finish_time_later_than_its_queue_time(db):
    # End to end through run_generation rather than through _set_status
    # directly: the task the UI polls must carry a genuine finish time.
    key = f"sys-{uuid.uuid4().hex[:6]}"
    _seed_declaration(db, _seed_system(db, key), "marketing.advertising")
    atype = f"kenya_dpia_{uuid.uuid4().hex[:6]}"
    _full_coverage_template(db, atype)
    db.flush()
    task_id = _seed_task(db, assessment_types=[atype], system_fides_keys=[key])
    db.flush()

    run_generation(db, task_id)

    stamps = _timestamps(db, task_id)
    assert _task_row(db, task_id)["status"] == "complete"
    assert stamps["updated_at"] > stamps["created_at"]


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


def _run_wrapper(session, task_id):
    """Invoke the real Celery task body with a session we control.

    generate_assessments is bound (bind=True), so `self` is the task instance
    and `self.get_new_session()` is what supplies its session in production.
    Patching that method on DatabaseTask — the base the task actually resolves
    it from — exercises the wrapper's own code (the context-manager use, the
    try/except, the _fail_task call, the re-raise) against a transaction this
    test can roll back. Calling run_generation directly, as every other test
    in this file does, skips all of it.

    The task object itself is a Celery PromiseProxy until first use, which is
    why the patch goes on the base class rather than on the task.
    """
    import contextlib

    from fides.api.privacycare.tasks import generate_assessments
    from fides.api.tasks import DatabaseTask

    @contextlib.contextmanager
    def _session(_self):
        yield session

    original = DatabaseTask.get_new_session
    DatabaseTask.get_new_session = _session
    try:
        return generate_assessments.run(task_id)
    finally:
        DatabaseTask.get_new_session = original


def test_the_celery_wrapper_runs_a_generation_end_to_end(db):
    # Nothing exercised this wrapper. Replacing get_new_session with a method
    # that does not exist left the whole suite green — the only code that runs
    # a real generation was the least tested code in the module.
    key = f"sys-{uuid.uuid4().hex[:6]}"
    _seed_declaration(db, _seed_system(db, key), "marketing.advertising")
    atype = f"kenya_dpia_{uuid.uuid4().hex[:6]}"
    _full_coverage_template(db, atype)
    db.flush()
    task_id = _seed_task(db, assessment_types=[atype], system_fides_keys=[key])
    db.flush()

    _run_wrapper(db, task_id)

    row = _task_row(db, task_id)
    assert row["status"] == "complete", row
    assert row["completed_count"] == 1
    assert len(_assessments_for_task(db, task_id)) == 1


def test_the_celery_wrapper_records_a_failure_and_re_raises(db, monkeypatch):
    # A task that dies without updating its row leaves the UI polling
    # `in_processing` forever with nothing to show for it. The wrapper must do
    # BOTH: mark the row, and re-raise so Celery itself sees the failure.
    from fides.api.privacycare import tasks as tasks_module

    monkeypatch.setattr(
        tasks_module,
        "run_generation",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    task_id = _seed_task(db, assessment_types=["gdpr_dpia"])
    # Committed, not just flushed, because that is the real ordering: the
    # route commits the task row BEFORE queueing the job (a worker can consume
    # the message the instant it is published). _fail_task opens with
    # db.rollback() to clear a poisoned transaction before writing the error
    # row, so an uncommitted seed would vanish here and the test would be
    # asserting against a fixture artefact rather than the wrapper.
    db.commit()

    with pytest.raises(RuntimeError, match="boom"):
        _run_wrapper(db, task_id)

    row = _task_row(db, task_id)
    assert row["status"] == "error", (
        "the wrapper must mark the row, or the progress bar polls a run that "
        f"will never report: {row}"
    )
    assert "boom" in (row["message"] or "")
