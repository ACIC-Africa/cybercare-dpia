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
# UPDATE (Task 2, plan 20, spec 2026-09-17-privacycare-20-screening-rekey):
# Task 1 re-keyed the screening decision from a processing activity
# (privacydeclaration) to a business process, because this customer runs 86
# real business processes and only 2 declarations exist system-wide — the
# privacy SME confirmed screening belongs on the actual operational unit,
# not an invented stand-in. is_screened_out() now takes a
# business_process_id. This file's job widens accordingly: it pins that
# run_generation resolves a GenerationTarget's declaration_id through
# privacycare_process_declaration to find the business process(es) that
# process it, reads THEIR verdict, and stays opt-in at the new boundary too
# — a declaration with no row in that link table at all is never blocked.
# Only one such link exists in the whole system as of this task (Task 4 of
# this plan is what populates the rest), so every test here that needs the
# gate to see a process at all seeds its own business process and its own
# link, rather than depending on live data that would make the test pass
# vacuously.
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

import pytest
import sqlalchemy

from fides.api.privacycare.screening.gate import record_decision
from fides.api.privacycare.tasks import run_generation, skipped_for_task
from tests.privacycare.test_context import _seed_declaration, _seed_system
from tests.privacycare.test_tasks import (
    _assessments_for_task,
    _full_coverage_template,
    _run_wrapper,
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


def _seed_business_process(db, name: str, business_cycle: str) -> str:  # noqa: F811
    """Seeds one of the customer's own business processes (her register,
    not a generic fixture name) — Task 1 re-pointed the screening decision
    at privacycare_business_process, not privacydeclaration, so this is the
    table a decision needs a real row in. Same helper, same reasoning, as
    test_screening_gate.py's own _seed_business_process."""
    process_id = f"bp_{uuid.uuid4().hex[:12]}"
    db.execute(
        sqlalchemy.text(
            "INSERT INTO privacycare_business_process (id, name, business_cycle) "
            "VALUES (:id, :name, :cycle)"
        ),
        {"id": process_id, "name": name, "cycle": business_cycle},
    )
    return process_id


def _link(db, process_id: str, declaration_id: str) -> None:  # noqa: F811
    """Seeds one privacycare_process_declaration row: this business process
    processes data under this declaration. This is the join run_generation's
    gate now resolves an activity's declaration_id through to find its
    business process(es) — see tasks.py's _is_activity_screened_out. Only
    one such link exists in the whole system as of this task (Task 4 of
    this plan is what populates the rest), so a test that wants the gate to
    see a process at all must seed this itself rather than rely on live
    data, which would make the test pass vacuously."""
    db.execute(
        sqlalchemy.text(
            "INSERT INTO privacycare_process_declaration "
            "(id, business_process_id, privacy_declaration_id) "
            "VALUES (:id, :process_id, :declaration_id)"
        ),
        {
            "id": str(uuid.uuid4()),
            "process_id": process_id,
            "declaration_id": declaration_id,
        },
    )


def _screen_out(db, business_process_id: str, *, decided_by="carol@example.com") -> None:  # noqa: F811
    record_decision(
        db,
        business_process_id=business_process_id,
        triggered_keys=[],
        justification="No processing risk identified.",
        decided_by=decided_by,
    )


def _backdate(db, business_process_id: str, triggered_keys: list[str], *, hours: int) -> None:  # noqa: F811
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
            "WHERE business_process_id = :process_id AND triggered_keys = :keys"
        ),
        {"hours": hours, "process_id": business_process_id, "keys": triggered_keys},
    )


def _screen_in(db, business_process_id: str, *, decided_by="carol@example.com") -> None:  # noqa: F811
    _seed_trigger(db, "large_scale")
    record_decision(
        db,
        business_process_id=business_process_id,
        triggered_keys=["large_scale"],
        justification=None,
        decided_by=decided_by,
    )


def test_a_screened_out_declaration_produces_no_assessment(db):  # noqa: F811
    key = f"sys-{uuid.uuid4().hex[:6]}"
    sid = _seed_system(db, key)
    decl_id = _seed_declaration(db, sid, "marketing.advertising")
    process_id = _seed_business_process(db, "Fuel Card Issuance", "Card Operations")
    _link(db, process_id, decl_id)
    _screen_out(db, process_id)
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
    process_id = _seed_business_process(db, "Fraud Risk Assessments", "Audit & Risk")
    _link(db, process_id, decl_id)
    _screen_in(db, process_id)
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
    # The gate is opt-in: a declaration that has never been screened at all,
    # and that has no link to any business process either, must generate
    # exactly as if the gate did not exist. Getting this backwards would
    # silently halt every existing workflow.
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


def test_a_declaration_with_no_process_link_is_not_blocked_even_when_a_process_is_screened_out(
    db,  # noqa: F811
):
    # The gate is opt-in at the NEW boundary too (Task 2, plan 20): the
    # verdict now lives on a business process, reached through
    # privacycare_process_declaration. A business process being screened
    # out must never reach past its own linked declarations and block an
    # UNLINKED one — that is the same "an unscreened activity is not
    # blocked" contract as the test above, but this is the one that would
    # actually fail if run_generation's join leaked across declarations
    # (e.g. by screening every declaration on the same system, or by
    # forgetting the join entirely and matching everything).
    key = f"sys-{uuid.uuid4().hex[:6]}"
    sid = _seed_system(db, key)
    unlinked_decl = _seed_declaration(db, sid, "marketing.advertising")
    linked_decl = _seed_declaration(db, sid, "essential.service.payment_processing")
    process_id = _seed_business_process(db, "CSR Planning & Execution", "CSR")
    _link(db, process_id, linked_decl)
    _screen_out(db, process_id)
    atype = f"kenya_dpia_{uuid.uuid4().hex[:6]}"
    _full_coverage_template(db, atype)
    db.flush()
    task_id = _seed_task(db, assessment_types=[atype], system_fides_keys=[key])
    db.flush()

    run_generation(db, task_id)

    assessments = _assessments_for_task(db, task_id)
    assert len(assessments) == 1
    assert assessments[0]["declaration_id"] == unlinked_decl
    row = _task_row(db, task_id)
    assert row["status"] == "complete"
    assert row["total_count"] == 2
    assert row["completed_count"] == 1


# Batch cleanup (plan 20): _is_activity_screened_out's multi-link branch —
# one declaration linked to SEVERAL business processes — had no test at
# all. The docstring's rule: skip only when EVERY linked process currently
# screens out; one linked process that is applicable, or simply never
# screened, is enough to let the activity generate. The three tests below
# each seed their own two-process link on a single declaration (the live
# table holds 3 rows, all on one process, so leaning on it would pass
# vacuously) and cover the three cells that matter: all-out, one-applicable,
# one-unscreened.


def test_a_declaration_linked_to_several_processes_is_skipped_only_when_all_are_not_applicable(
    db,  # noqa: F811
):
    key = f"sys-{uuid.uuid4().hex[:6]}"
    sid = _seed_system(db, key)
    decl_id = _seed_declaration(db, sid, "marketing.advertising")
    process_1 = _seed_business_process(db, "Fuel Card Issuance", "Card Operations")
    process_2 = _seed_business_process(db, "Fraud Risk Assessments", "Audit & Risk")
    _link(db, process_1, decl_id)
    _link(db, process_2, decl_id)
    _screen_out(db, process_1)
    _screen_out(db, process_2)
    atype = f"kenya_dpia_{uuid.uuid4().hex[:6]}"
    _full_coverage_template(db, atype)
    db.flush()
    task_id = _seed_task(db, assessment_types=[atype], system_fides_keys=[key])
    db.flush()

    run_generation(db, task_id)

    assert _assessments_for_task(db, task_id) == [], (
        "every business process linked to this activity is not applicable, "
        "so the activity must be skipped, exactly as the single-process "
        "case already is"
    )
    row = _task_row(db, task_id)
    assert row["status"] == "complete"
    assert row["total_count"] == 1
    assert row["completed_count"] == 0


def test_a_declaration_linked_to_several_processes_generates_when_one_is_applicable(
    db,  # noqa: F811
):
    # One linked process screened IN is enough to let the activity
    # generate, even though the other linked process is not applicable —
    # the resolver must not skip merely because SOME linked process is not
    # applicable; it must require ALL of them to be.
    key = f"sys-{uuid.uuid4().hex[:6]}"
    sid = _seed_system(db, key)
    decl_id = _seed_declaration(db, sid, "marketing.advertising")
    not_applicable = _seed_business_process(db, "Fuel Card Issuance", "Card Operations")
    applicable = _seed_business_process(db, "Fraud Risk Assessments", "Audit & Risk")
    _link(db, not_applicable, decl_id)
    _link(db, applicable, decl_id)
    _screen_out(db, not_applicable)
    _screen_in(db, applicable)
    atype = f"kenya_dpia_{uuid.uuid4().hex[:6]}"
    _full_coverage_template(db, atype)
    db.flush()
    task_id = _seed_task(db, assessment_types=[atype], system_fides_keys=[key])
    db.flush()

    run_generation(db, task_id)

    assessments = _assessments_for_task(db, task_id)
    assert len(assessments) == 1, (
        "one linked process being applicable must let the activity "
        "generate, regardless of the other linked process's not-applicable "
        "verdict"
    )
    assert assessments[0]["declaration_id"] == decl_id
    row = _task_row(db, task_id)
    assert row["status"] == "complete"
    assert row["completed_count"] == 1


def test_a_declaration_linked_to_several_processes_generates_when_one_is_unscreened(
    db,  # noqa: F811
):
    # A linked process that has simply never been screened is, per
    # is_screened_out's own opt-in contract, not "not applicable" — so it
    # must not be treated as agreeing with a sibling process's not-applicable
    # verdict. The activity must generate.
    key = f"sys-{uuid.uuid4().hex[:6]}"
    sid = _seed_system(db, key)
    decl_id = _seed_declaration(db, sid, "marketing.advertising")
    not_applicable = _seed_business_process(db, "Fuel Card Issuance", "Card Operations")
    unscreened = _seed_business_process(db, "CSR Planning & Execution", "CSR")
    _link(db, not_applicable, decl_id)
    _link(db, unscreened, decl_id)
    _screen_out(db, not_applicable)
    atype = f"kenya_dpia_{uuid.uuid4().hex[:6]}"
    _full_coverage_template(db, atype)
    db.flush()
    task_id = _seed_task(db, assessment_types=[atype], system_fides_keys=[key])
    db.flush()

    run_generation(db, task_id)

    assessments = _assessments_for_task(db, task_id)
    assert len(assessments) == 1, (
        "one linked process being unscreened must let the activity "
        "generate, the same as if it were explicitly applicable"
    )
    assert assessments[0]["declaration_id"] == decl_id
    row = _task_row(db, task_id)
    assert row["status"] == "complete"
    assert row["completed_count"] == 1


def test_a_mixed_run_skips_the_screened_out_and_processes_the_rest(db):  # noqa: F811
    key = f"sys-{uuid.uuid4().hex[:6]}"
    sid = _seed_system(db, key)
    in_decl = _seed_declaration(db, sid, "marketing.advertising")
    out_decl = _seed_declaration(db, sid, "essential.service.payment_processing")
    process_id = _seed_business_process(db, "Fuel Card Issuance", "Card Operations")
    _link(db, process_id, out_decl)
    _screen_out(db, process_id)
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
    process_id = _seed_business_process(db, "Fraud Risk Assessments", "Audit & Risk")
    _link(db, process_id, decl_id)
    _screen_out(db, process_id)
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
    process_id = _seed_business_process(db, "CSR Planning & Execution", "CSR")
    _link(db, process_id, out_decl)
    _screen_out(db, process_id)
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
    process_id = _seed_business_process(db, "Fuel Card Issuance", "Card Operations")
    _link(db, process_id, decl_id)
    _screen_out(db, process_id)
    _backdate(db, process_id, [], hours=1)
    atype = f"kenya_dpia_{uuid.uuid4().hex[:6]}"
    _full_coverage_template(db, atype)
    db.flush()

    first_task = _seed_task(db, assessment_types=[atype], system_fides_keys=[key])
    db.flush()
    run_generation(db, first_task)
    assert _assessments_for_task(db, first_task) == []

    _screen_in(db, process_id, decided_by="michael@example.com")

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
    process_id = _seed_business_process(db, "Fraud Risk Assessments", "Audit & Risk")
    _link(db, process_id, out_decl_1)
    _link(db, process_id, out_decl_2)
    _screen_out(db, process_id)
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
    process_id = _seed_business_process(db, "CSR Planning & Execution", "CSR")
    _link(db, process_id, out_decl_1)
    _link(db, process_id, out_decl_2)
    _screen_out(db, process_id)
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
    process_id = _seed_business_process(db, "Fuel Card Issuance", "Card Operations")
    _link(db, process_id, decl_id)
    _screen_out(db, process_id)
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


def test_a_crash_after_real_skips_does_not_report_zero_skipped(
    db, monkeypatch  # noqa: F811
):
    # Fix round 1 (coordinator review, Important finding, self-flagged in
    # the task-1 report): `_fail_task` never persisted a skip count, so a
    # crash between loop iterations reported skipped_for_task == 0 even
    # when earlier targets in the SAME run had genuinely been screened out
    # moments before. `skipped` lived only as a local Python integer until
    # the run's final _finish call, which a crash never reaches.
    #
    # This drives the REAL run_generation through the REAL Celery wrapper
    # (_run_wrapper, same helper test_tasks.py's own wrapper tests use) so
    # both the accrual and the failure path are exercised as they actually
    # run in production, not reimplemented by hand. Two declarations are
    # screened out for real (each linked to its own business process,
    # since Task 2 resolves the verdict through
    # privacycare_process_declaration now, not off the declaration
    # directly); a third's gate resolution is made to raise, simulating
    # exactly the "transient database error mid-loop" scenario the finding
    # describes — the gate check (now _is_activity_screened_out) is called
    # directly in the for loop, outside every per-target try/except, so
    # this exception escapes run_generation entirely.
    from fides.api.privacycare import tasks as tasks_module

    key = f"sys-{uuid.uuid4().hex[:6]}"
    sid = _seed_system(db, key)
    out_decl_1 = _seed_declaration(db, sid, "a.screened.one")
    out_decl_2 = _seed_declaration(db, sid, "b.screened.two")
    crash_decl = _seed_declaration(db, sid, "c.crashes")
    process_1 = _seed_business_process(db, "Fuel Card Issuance", "Card Operations")
    process_2 = _seed_business_process(db, "Fraud Risk Assessments", "Audit & Risk")
    _link(db, process_1, out_decl_1)
    _link(db, process_2, out_decl_2)
    _screen_out(db, process_1)
    _screen_out(db, process_2)
    atype = f"kenya_dpia_{uuid.uuid4().hex[:6]}"
    _full_coverage_template(db, atype)
    task_id = _seed_task(db, assessment_types=[atype], system_fides_keys=[key])
    # Committed, not merely flushed: _fail_task opens with db.rollback() to
    # clear a poisoned transaction (see test_tasks.py's
    # test_the_celery_wrapper_records_a_failure_and_re_raises, same reason),
    # and an uncommitted seed would vanish with it.
    db.commit()

    real_is_activity_screened_out = tasks_module._is_activity_screened_out

    def _screened_out_then_boom(db_, declaration_id):
        if declaration_id == crash_decl:
            raise RuntimeError("transient database error")
        return real_is_activity_screened_out(db_, declaration_id)

    monkeypatch.setattr(
        tasks_module, "_is_activity_screened_out", _screened_out_then_boom
    )

    with pytest.raises(RuntimeError, match="transient database error"):
        _run_wrapper(db, task_id)

    assert skipped_for_task(db, task_id) == 2, (
        "two activities were genuinely screened out before the crash on a "
        "third target's gate lookup — that fact must survive the crash, "
        "not reset to zero because it lived only in a local variable until "
        "a final _finish call the crash never let the run reach"
    )
