# Task 3 of plan 18: the screening gate actually stopping generation.
#
# Tasks 1 and 2 built the table and the verdict (privacycare_screening_
# decision, is_screened_out). Nobody consumed it yet. In Fides today
# "prior consultation" is asked six times inside GDPR question text and
# enforced nowhere — this workstream criticised that in writing. A
# screening gate that records a verdict nobody acts on would be the same
# defect wearing our own badge. This file pins that run_generation actually
# skips a screened-out declaration BEFORE any assessment row, LLM call, or
# completeness computation happens for it — and that the gate is opt-in
# (an unscreened declaration is never blocked).
#
# run_generation commits repeatedly by design (see tasks.py's own
# docstring), so this file reuses test_tasks.py's savepoint-based `db`
# fixture rather than the plain rollback-only fixture test_screening_gate.py
# uses — the latter would let run_generation's internal commits escape to
# the real dev database.
import glob
import os
import re
import uuid

import sqlalchemy

from fides.api.privacycare.screening.gate import record_decision
from fides.api.privacycare.tasks import run_generation, skipped_for_task
from tests.privacycare.test_context import _seed_declaration, _seed_system
from tests.privacycare.test_tasks import (
    _assessments_for_task,
    _full_coverage_template,
    _seed_task,
    _task_row,
    db,  # noqa: F401 - reused fixture, not a local definition
)


def _seed_trigger(db, key: str = "large_scale") -> None:  # noqa: F811
    """The one trigger row needed to record a screen-IN decision. A
    screen-OUT needs no trigger row at all: record_decision's unknown-key
    check is only ever run against a non-empty triggered_keys list.

    UPDATE (Task 5, spec 2026-09-17, plan 18): the authorized `--commit`
    seed has now run against this same live database, so
    privacycare_screening_trigger is no longer empty by default — Carol's
    six rows (which include "large_scale", this helper's own default) are
    permanent, same as the Kenya template. ON CONFLICT (trigger_key) DO
    NOTHING keeps this helper working either way: a no-op against the real
    row in this database, a real insert against a from-empty one (e.g.
    CI). Every caller here only needs the key to be valid to screen
    against, never any particular label/description text."""
    db.execute(
        sqlalchemy.text(
            "INSERT INTO privacycare_screening_trigger "
            "(id, trigger_key, label, description, display_order) "
            "VALUES (:id, :key, :label, :description, 1) "
            "ON CONFLICT (trigger_key) DO NOTHING"
        ),
        {
            "id": str(uuid.uuid4()),
            "key": key,
            "label": key.replace("_", " ").title(),
            "description": f"Test description for {key}.",
        },
    )


def _screen_out(db, declaration_id: str, *, decided_by="carol@example.com") -> None:  # noqa: F811
    record_decision(
        db,
        declaration_id=declaration_id,
        triggered_keys=[],
        justification="No processing risk identified.",
        decided_by=decided_by,
    )


def _backdate(db, declaration_id: str, triggered_keys: list[str], *, hours: int) -> None:  # noqa: F811
    """Pushes one decision's decided_at into the past by hand. Postgres'
    now() (this column's server_default) is the TRANSACTION's start time,
    not the statement's, and this test's session shares one transaction
    across both record_decision calls (run_generation's internal commits
    are savepoint releases, not new transactions) — so without this, both
    decisions would tie on decided_at and the outcome would depend on the
    id tie-break's coin flip instead of proving "newest" actually means
    newest. Same technique as test_screening_gate.py's own _backdate."""
    db.execute(
        sqlalchemy.text(
            "UPDATE privacycare_screening_decision "
            "SET decided_at = decided_at - (:hours || ' hours')::interval "
            "WHERE declaration_id = :decl_id AND triggered_keys = :keys"
        ),
        {"hours": hours, "decl_id": declaration_id, "keys": triggered_keys},
    )


def _screen_in(db, declaration_id: str, *, decided_by="carol@example.com") -> None:  # noqa: F811
    _seed_trigger(db, "large_scale")
    record_decision(
        db,
        declaration_id=declaration_id,
        triggered_keys=["large_scale"],
        justification=None,
        decided_by=decided_by,
    )


def test_a_screened_out_declaration_produces_no_assessment(db):  # noqa: F811
    key = f"sys-{uuid.uuid4().hex[:6]}"
    sid = _seed_system(db, key)
    decl_id = _seed_declaration(db, sid, "marketing.advertising")
    _screen_out(db, decl_id)
    atype = f"kenya_dpia_{uuid.uuid4().hex[:6]}"
    _full_coverage_template(db, atype)
    db.flush()
    task_id = _seed_task(db, assessment_types=[atype], system_fides_keys=[key])
    db.flush()

    run_generation(db, task_id)

    assert _assessments_for_task(db, task_id) == [], (
        "a screened-out declaration must produce no privacy_assessment row "
        "at all — not one that is later discarded"
    )
    row = _task_row(db, task_id)
    assert row["status"] == "complete", (
        "every target being screened out is the gate working as intended, "
        "not a run failure"
    )
    assert row["total_count"] == 1
    assert row["completed_count"] == 0
    assert "screen" in row["message"].lower()


def test_a_screened_in_declaration_generates_exactly_as_before(db):  # noqa: F811
    key = f"sys-{uuid.uuid4().hex[:6]}"
    sid = _seed_system(db, key)
    decl_id = _seed_declaration(db, sid, "marketing.advertising")
    _screen_in(db, decl_id)
    atype = f"kenya_dpia_{uuid.uuid4().hex[:6]}"
    _full_coverage_template(db, atype)
    db.flush()
    task_id = _seed_task(db, assessment_types=[atype], system_fides_keys=[key])
    db.flush()

    run_generation(db, task_id)

    assessments = _assessments_for_task(db, task_id)
    assert len(assessments) == 1
    assert assessments[0]["declaration_id"] == decl_id
    row = _task_row(db, task_id)
    assert row["status"] == "complete"
    assert row["total_count"] == 1
    assert row["completed_count"] == 1


def test_an_unscreened_declaration_is_not_blocked(db):  # noqa: F811
    # The gate is opt-in: a declaration that has never been screened at all
    # must generate exactly as if the gate did not exist. Getting this
    # backwards would silently halt every existing workflow.
    key = f"sys-{uuid.uuid4().hex[:6]}"
    sid = _seed_system(db, key)
    decl_id = _seed_declaration(db, sid, "marketing.advertising")
    atype = f"kenya_dpia_{uuid.uuid4().hex[:6]}"
    _full_coverage_template(db, atype)
    db.flush()
    task_id = _seed_task(db, assessment_types=[atype], system_fides_keys=[key])
    db.flush()

    run_generation(db, task_id)

    assessments = _assessments_for_task(db, task_id)
    assert len(assessments) == 1
    assert assessments[0]["declaration_id"] == decl_id
    row = _task_row(db, task_id)
    assert row["status"] == "complete"
    assert row["completed_count"] == 1


def test_a_mixed_run_skips_the_screened_out_and_processes_the_rest(db):  # noqa: F811
    key = f"sys-{uuid.uuid4().hex[:6]}"
    sid = _seed_system(db, key)
    in_decl = _seed_declaration(db, sid, "marketing.advertising")
    out_decl = _seed_declaration(db, sid, "essential.service.payment_processing")
    _screen_out(db, out_decl)
    atype = f"kenya_dpia_{uuid.uuid4().hex[:6]}"
    _full_coverage_template(db, atype)
    db.flush()
    task_id = _seed_task(db, assessment_types=[atype], system_fides_keys=[key])
    db.flush()

    run_generation(db, task_id)

    assessments = _assessments_for_task(db, task_id)
    assert len(assessments) == 1
    assert assessments[0]["declaration_id"] == in_decl

    row = _task_row(db, task_id)
    assert row["status"] == "complete"
    # Someone who runs generation over a mixed estate and gets fewer
    # assessments than requested needs to see the skip accounted for, not
    # wonder where it went or mistake it for a failure.
    assert row["total_count"] == 2
    assert row["completed_count"] == 1
    assert "1" in row["message"]
    assert "screen" in row["message"].lower()
    assert "fail" not in row["message"].lower()


def test_a_screened_out_target_is_not_reported_as_a_failure(db):  # noqa: F811
    # Fix round 1 (Finding 2, minor): this test used to assert only
    # status/message and never that the target was actually skipped — it
    # would have kept passing even if the gate check were deleted entirely,
    # since an ungated run over one screened-out-but-otherwise-normal
    # declaration also reports "complete" with no "fail" in the message.
    # Asserting the empty assessment list ties the claim in the test's name
    # to the behaviour it actually depends on.
    key = f"sys-{uuid.uuid4().hex[:6]}"
    sid = _seed_system(db, key)
    decl_id = _seed_declaration(db, sid, "marketing.advertising")
    _screen_out(db, decl_id)
    atype = f"kenya_dpia_{uuid.uuid4().hex[:6]}"
    _full_coverage_template(db, atype)
    db.flush()
    task_id = _seed_task(db, assessment_types=[atype], system_fides_keys=[key])
    db.flush()

    run_generation(db, task_id)

    assert _assessments_for_task(db, task_id) == [], (
        "precondition: the target must actually have been skipped, not "
        "merely have generated without incident"
    )
    row = _task_row(db, task_id)
    assert row["completed_count"] == 0
    assert row["status"] == "complete", (
        "a screen-out must never flip the run's status to error"
    )
    assert "fail" not in row["message"].lower()


def test_a_run_with_zero_completions_from_both_skips_and_failures_names_both(
    db, monkeypatch  # noqa: F811
):
    # Fix round 1 (Finding 1, important): with completed==0, the old
    # completed==0 branch fell straight into `if failures:` and wrote "All
    # {total} assessments failed" whenever there was at least one failure —
    # silently folding any screened-out targets into that count. Three
    # targets here: one screened out, two raise. completed stays 0, but the
    # message must credit the skip as a skip, not as a third failure, and
    # status must still be "error" because something genuinely broke (an
    # all-skipped zero-completion run is the one case that is NOT an error
    # — see test_a_screened_out_declaration_produces_no_assessment above).
    from fides.api.privacycare import tasks as tasks_module

    monkeypatch.setattr(
        tasks_module,
        "answer_questions",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("simulated failure")),
    )

    key = f"sys-{uuid.uuid4().hex[:6]}"
    sid = _seed_system(db, key)
    out_decl = _seed_declaration(db, sid, "marketing.advertising")
    fail_decl = _seed_declaration(db, sid, "essential.service.payment_processing")
    _screen_out(db, out_decl)
    atype = f"kenya_dpia_{uuid.uuid4().hex[:6]}"
    _full_coverage_template(db, atype)
    db.flush()
    task_id = _seed_task(db, assessment_types=[atype], system_fides_keys=[key])
    db.flush()

    run_generation(db, task_id)

    assert _assessments_for_task(db, task_id) == []
    row = _task_row(db, task_id)
    assert row["total_count"] == 2
    assert row["completed_count"] == 0
    assert row["status"] == "error", (
        "a genuine failure occurred and produced nothing — unlike an "
        "all-screened-out run, this is not a successful outcome"
    )
    message = row["message"].lower()
    assert "1 screened out" in message
    assert "1 failed" in message
    assert "all 2 assessments failed" not in message, (
        "the screened-out target must never be folded into the failure count"
    )


def test_re_screening_a_declaration_back_in_lets_the_next_run_generate(db):  # noqa: F811
    # The append-only rule doing real work: the old screen-out row still
    # exists (record_decision never updates), and current_verdict reads the
    # newest — so re-screening in must let a LATER run proceed even though
    # an EARLIER run, over the same declaration, correctly produced nothing.
    key = f"sys-{uuid.uuid4().hex[:6]}"
    sid = _seed_system(db, key)
    decl_id = _seed_declaration(db, sid, "marketing.advertising")
    _screen_out(db, decl_id)
    _backdate(db, decl_id, [], hours=1)
    atype = f"kenya_dpia_{uuid.uuid4().hex[:6]}"
    _full_coverage_template(db, atype)
    db.flush()

    first_task = _seed_task(db, assessment_types=[atype], system_fides_keys=[key])
    db.flush()
    run_generation(db, first_task)
    assert _assessments_for_task(db, first_task) == []

    _screen_in(db, decl_id, decided_by="michael@example.com")

    second_task = _seed_task(db, assessment_types=[atype], system_fides_keys=[key])
    db.flush()
    run_generation(db, second_task)

    assessments = _assessments_for_task(db, second_task)
    assert len(assessments) == 1
    assert assessments[0]["declaration_id"] == decl_id
    row = _task_row(db, second_task)
    assert row["status"] == "complete"
    assert row["completed_count"] == 1


# Plan 19, Task 1: the skip count is visible, not just narrated in a
# free-text message nothing else reads. The four tests below pin
# skipped_for_task's contract directly against a real run_generation call —
# not against a hand-inserted row — so they also exercise the write path
# inside run_generation's own transaction.


def test_a_run_that_skips_two_targets_records_two(db):  # noqa: F811
    key = f"sys-{uuid.uuid4().hex[:6]}"
    sid = _seed_system(db, key)
    _seed_declaration(db, sid, "marketing.advertising")  # kept, unscreened
    out_decl_1 = _seed_declaration(db, sid, "essential.service.payment_processing")
    out_decl_2 = _seed_declaration(db, sid, "essential.service")
    _screen_out(db, out_decl_1)
    _screen_out(db, out_decl_2)
    atype = f"kenya_dpia_{uuid.uuid4().hex[:6]}"
    _full_coverage_template(db, atype)
    db.flush()
    task_id = _seed_task(db, assessment_types=[atype], system_fides_keys=[key])
    db.flush()

    run_generation(db, task_id)

    assert skipped_for_task(db, task_id) == 2


def test_a_run_that_skips_nothing_persists_no_row(db):  # noqa: F811
    key = f"sys-{uuid.uuid4().hex[:6]}"
    sid = _seed_system(db, key)
    _seed_declaration(db, sid, "marketing.advertising")
    atype = f"kenya_dpia_{uuid.uuid4().hex[:6]}"
    _full_coverage_template(db, atype)
    db.flush()
    task_id = _seed_task(db, assessment_types=[atype], system_fides_keys=[key])
    db.flush()

    run_generation(db, task_id)

    assert skipped_for_task(db, task_id) == 0
    row = db.execute(
        sqlalchemy.text(
            "SELECT 1 FROM privacycare_generation_skip WHERE task_id = :id"
        ),
        {"id": task_id},
    ).first()
    assert row is None, (
        "a run that skipped nothing must leave no row — an absent row is "
        "what makes skipped_for_task's 0 correct, not a stored zero"
    )


def test_the_persisted_count_matches_the_task_message(db):  # noqa: F811
    # The count and the message are two views of one fact recorded in the
    # same transaction — they must never disagree. Parsing the digit back
    # out of the message (rather than hardcoding the expected count twice)
    # is what actually proves that, instead of two independently-correct
    # assertions that happen to agree by construction.
    key = f"sys-{uuid.uuid4().hex[:6]}"
    sid = _seed_system(db, key)
    _seed_declaration(db, sid, "marketing.advertising")
    out_decl_1 = _seed_declaration(db, sid, "essential.service.payment_processing")
    out_decl_2 = _seed_declaration(db, sid, "essential.service")
    _screen_out(db, out_decl_1)
    _screen_out(db, out_decl_2)
    atype = f"kenya_dpia_{uuid.uuid4().hex[:6]}"
    _full_coverage_template(db, atype)
    db.flush()
    task_id = _seed_task(db, assessment_types=[atype], system_fides_keys=[key])
    db.flush()

    run_generation(db, task_id)

    row = _task_row(db, task_id)
    match = re.search(r"(\d+) screened out", row["message"])
    assert match is not None, row["message"]
    assert skipped_for_task(db, task_id) == int(match.group(1))


def test_re_running_generation_for_the_same_task_does_not_duplicate_the_skip_row(
    db,  # noqa: F811
):
    key = f"sys-{uuid.uuid4().hex[:6]}"
    sid = _seed_system(db, key)
    decl_id = _seed_declaration(db, sid, "marketing.advertising")
    _screen_out(db, decl_id)
    atype = f"kenya_dpia_{uuid.uuid4().hex[:6]}"
    _full_coverage_template(db, atype)
    db.flush()
    task_id = _seed_task(db, assessment_types=[atype], system_fides_keys=[key])
    db.flush()

    run_generation(db, task_id)
    run_generation(db, task_id)

    assert skipped_for_task(db, task_id) == 1
    row_count = db.execute(
        sqlalchemy.text(
            "SELECT count(*) FROM privacycare_generation_skip WHERE task_id = :id"
        ),
        {"id": task_id},
    ).scalar_one()
    assert row_count == 1, (
        "a second run over the same task id must update the one row, not "
        "insert a second one — the unique index on task_id is the guarantee"
    )


def test_the_migration_creates_only_the_generation_skip_table():
    # Structural, not live-DB: proves the migration script itself never
    # mentions Ethyca's privacy_assessment_task table at all — this is a
    # new PrivacyCare table keyed by task id, never a column added to
    # theirs, which is the whole reason this table exists (see models.py's
    # GenerationSkip comment).
    versions_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "src",
        "fides",
        "api",
        "privacycare",
        "migrations",
        "versions",
    )
    matches = glob.glob(os.path.join(versions_dir, "*_generation_skip.py"))
    assert len(matches) == 1, (
        f"expected exactly one *_generation_skip.py migration, found {matches}"
    )
    with open(matches[0]) as f:
        source = f.read()

    assert "down_revision = '54c8fce54023'" in source, (
        "must chain onto the current PrivacyCare head"
    )
    assert source.count("op.create_table") == 1
    assert "privacycare_generation_skip" in source
    assert "'privacy_assessment_task'" not in source, (
        "this migration must never name privacy_assessment_task as an "
        "operation TARGET (op.create_table/add_column/alter_column/…) — "
        "the count lives in a PrivacyCare table keyed by task id, not a "
        "column on theirs. (Prose may still mention the table by name.)"
    )
    assert "op.alter_column" not in source
    assert "op.add_column" not in source
