"""The scheduled job: walks the register once a day, decides which
obligation is due an alert (dsr/alerts.py), delivers it through a channel
(dsr/channels.py), and reports what happened. Nothing here decides
thresholds or implements delivery — those are covered by
test_dsr_alerts.py and test_dsr_channels.py respectively.

D-DSR-5 / D-AL-7: a channel that is ignored is worse than no channel, so a
failed send must never be recorded as sent — the next run is the retry,
and it only works because the ledger row was never written.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy
from sqlalchemy.orm import Session

import fides.api.privacycare.dsr.alert_job as alert_job_module
from fides.api.privacycare.dsr.alert_job import (
    RunSummary,
    initiate_scheduled_dsr_alerts,
    run_deadline_alerts,
)
from fides.api.privacycare.dsr.alerts import alerts_sent_for
from fides.api.privacycare.dsr.channels import LoggingChannel
from fides.api.privacycare.dsr.delegation import delegate, ensure_kenyan_policies
from fides.api.privacycare.dsr.register import record_decision, record_request
from fides.api.privacycare.dsr.timelines import seed_timelines

DB_URL = "postgresql://postgres:fides@127.0.0.1:5442/fides"
NOW = datetime(2026, 9, 15, 7, 0, tzinfo=timezone.utc)


def _scope_alert_run(monkeypatch) -> set:
    """run_deadline_alerts walks list_requests(db) for the WHOLE shared
    register — this live database now also carries the demo seed's own 7
    permanent DSR rows (D-SEED-8, plan 20), spanning all six Kenyan rights
    (access twice), plus whatever any other test session has left behind.
    A RunSummary equality or an absolute field count (unclocked==1,
    unowned==1, ...) only proves what it used to prove once the run is
    scoped to just the ids THIS test created — the same idea as every
    other file in this suite scoping its own assertions to
    alerts_sent_for(db, request_id) rather than a database-wide count, one
    level up: applied to run_deadline_alerts' own input instead of its
    output.

    Monkeypatches alert_job.list_requests (the one place run_deadline_
    alerts reads the register) to filter the real result down to the
    returned set's ids. Nothing about alert_job.py itself changes — this
    is a test-side seam, not a production behaviour change — and the real
    list_requests is still the thing doing the reading, just filtered
    afterwards. Add each request's id to the returned set right after
    creating it, before the next run_deadline_alerts call that should see
    it.
    """
    ids: set = set()
    real_list_requests = alert_job_module.list_requests

    def _scoped_list_requests(db_):
        return [row for row in real_list_requests(db_) if row["id"] in ids]

    monkeypatch.setattr(alert_job_module, "list_requests", _scoped_list_requests)
    return ids


@pytest.fixture
def db(monkeypatch):
    engine = sqlalchemy.create_engine(DB_URL)
    with Session(engine) as session:
        monkeypatch.setattr(session, "commit", session.flush)
        seed_timelines(session)
        yield session
        session.rollback()


def _subject() -> str:
    return f"subject-{uuid.uuid4().hex[:8]}@example.com"


def _set_deadline(db: Session, request_id: str, when: datetime) -> None:
    """The register freezes deadline_at at creation (record_request), so a
    test that needs an obligation near its threshold moves the deadline
    rather than trying to move the clock."""
    db.execute(
        sqlalchemy.text(
            "UPDATE privacycare_dsr_request SET deadline_at = :when WHERE id = :id"
        ),
        {"when": when, "id": request_id},
    )


class _FailingChannel:
    """Fails on the obligations whose ids are in `fail_for`, succeeds otherwise."""

    name = "failing"

    def __init__(self, fail_for: set) -> None:
        self.fail_for = fail_for
        self.sent: list = []

    def send(self, alert) -> None:
        if alert.dsr_request_id in self.fail_for:
            raise RuntimeError("channel unavailable")
        self.sent.append(alert)


def test_an_obligation_at_its_threshold_warns_its_owner_once(db, monkeypatch):
    # Acceptance 1. The second run the same day must send nothing.
    scope = _scope_alert_run(monkeypatch)
    request_id = record_request(
        db, right="access", subject_identifier=_subject(),
        owner_email="ops@customer.co.ke",
    )
    scope.add(request_id)
    # access is a 7-day clock; warn_threshold(7) == 3, so 3 days out is
    # exactly at the threshold.
    _set_deadline(db, request_id, NOW + timedelta(days=3))

    channel = LoggingChannel()
    first = run_deadline_alerts(db, channel=channel, now=NOW)

    assert first == RunSummary(
        attempted=1, sent=1, failed=0, skipped_closed=0, skipped_discharged=0,
        unclocked=0, vanished=0, unowned=0,
    )
    assert len(channel.sent) == 1
    assert channel.sent[0].kind == "approaching"
    assert channel.sent[0].recipient == "ops@customer.co.ke"
    assert alerts_sent_for(db, request_id) == frozenset({"approaching"})

    second = run_deadline_alerts(db, channel=channel, now=NOW)
    assert second == RunSummary(
        attempted=0, sent=0, failed=0, skipped_closed=0, skipped_discharged=0,
        unclocked=0, vanished=0, unowned=0,
    )
    assert len(channel.sent) == 1, "the second run must send nothing"


def test_a_breached_obligation_escalates_once_and_distinctly(db):
    # Acceptance 2. A breach fires even without a prior approaching warning,
    # and fires only once.
    request_id = record_request(
        db, right="access", subject_identifier=_subject(),
        owner_email="ops@customer.co.ke",
    )
    _set_deadline(db, request_id, NOW - timedelta(hours=1))

    channel = LoggingChannel()
    summary = run_deadline_alerts(db, channel=channel, now=NOW)

    assert (summary.attempted, summary.sent, summary.failed) == (1, 1, 0)
    assert channel.sent[0].kind == "breached"
    assert alerts_sent_for(db, request_id) == frozenset({"breached"})

    again = run_deadline_alerts(db, channel=channel, now=NOW)
    assert (again.attempted, again.sent) == (0, 0)
    assert len(channel.sent) == 1


def test_the_alert_reports_days_left_correctly_at_its_boundaries(db):
    # Fix round 1, Finding 2. The ceil-for-future / floor-for-past split
    # in _days_left exists precisely so "due today" and "just breached"
    # read differently — nothing asserted that anywhere. Read it off the
    # Alert payloads actually delivered, at the three instants the split
    # was built to distinguish.
    exactly_now = record_request(
        db, right="access", subject_identifier=_subject(),
        owner_email="ops@customer.co.ke",
    )
    _set_deadline(db, exactly_now, NOW)  # remaining == 0: breached, days_left == 0

    just_passed = record_request(
        db, right="access", subject_identifier=_subject(),
        owner_email="ops@customer.co.ke",
    )
    # a hair past the deadline: still breached, but days_left must NOT
    # read 0 — that would misreport "just breached" as "due today".
    _set_deadline(db, just_passed, NOW - timedelta(seconds=1))

    six_hours_out = record_request(
        db, right="access", subject_identifier=_subject(),
        owner_email="ops@customer.co.ke",
    )
    # inside the 3-day approaching threshold (warn_threshold(7) == 3), and
    # ceil(0.25) must round UP to 1, not truncate to 0.
    _set_deadline(db, six_hours_out, NOW + timedelta(hours=6))

    channel = LoggingChannel()
    run_deadline_alerts(db, channel=channel, now=NOW)

    by_id = {alert.dsr_request_id: alert for alert in channel.sent}
    assert by_id[exactly_now].kind == "breached"
    assert by_id[exactly_now].days_left == 0
    assert by_id[just_passed].kind == "breached"
    assert by_id[just_passed].days_left == -1
    assert by_id[six_hours_out].kind == "approaching"
    assert by_id[six_hours_out].days_left == 1


def test_an_objection_is_never_alerted_and_is_reported_as_unclocked(db, monkeypatch):
    # Acceptance 3 / D-AL-4. summary.unclocked counts it; summary.attempted
    # does not. Skipping it silently would make an open question invisible.
    scope = _scope_alert_run(monkeypatch)
    request_id = record_request(
        db, right="objection", subject_identifier=_subject(),
        owner_email="ops@customer.co.ke",
    )
    scope.add(request_id)

    channel = LoggingChannel()
    summary = run_deadline_alerts(db, channel=channel, now=NOW)

    assert summary.unclocked == 1
    assert summary.attempted == 0
    assert channel.sent == []
    assert alerts_sent_for(db, request_id) == frozenset()


def test_an_obligation_fides_already_completed_is_not_alerted(db):
    # Acceptance 4 / D-AL-6. Chasing an owner about finished work is how a new
    # channel earns being muted, and the once-only rule means that first
    # impression is the only one.
    ensure_kenyan_policies(db)
    request_id = record_request(
        db, right="access", subject_identifier=_subject(),
        owner_email="ops@customer.co.ke",
    )
    fides_id = delegate(db, request_id=request_id)
    _set_deadline(db, request_id, NOW - timedelta(hours=1))  # would otherwise breach
    db.execute(
        sqlalchemy.text("UPDATE privacyrequest SET status = 'complete' WHERE id = :id"),
        {"id": fides_id},
    )

    channel = LoggingChannel()
    summary = run_deadline_alerts(db, channel=channel, now=NOW)

    assert summary.skipped_discharged == 1
    assert summary.attempted == 0
    assert channel.sent == []
    assert alerts_sent_for(db, request_id) == frozenset()


def test_a_closed_non_delegating_obligation_is_not_alerted(db, monkeypatch):
    # Fix round 1, Finding 1. Rectification never delegates (no Fides
    # analogue exists), so nothing about Fides' status could ever have
    # protected it — only the register's own status can. Barbara's ruling
    # makes the register the record of truth: a human closed this, so it
    # is never alerted on, breached or not.
    scope = _scope_alert_run(monkeypatch)
    request_id = record_request(
        db, right="rectification", subject_identifier=_subject(),
        owner_email="ops@customer.co.ke",
    )
    scope.add(request_id)
    _set_deadline(db, request_id, NOW - timedelta(hours=1))  # would otherwise breach
    record_decision(
        db, request_id=request_id, outcome="granted",
        grounds="corrected per subject's request", decided_by="dpo@customer.co.ke",
    )

    channel = LoggingChannel()
    summary = run_deadline_alerts(db, channel=channel, now=NOW)

    assert summary.skipped_closed == 1
    assert summary.attempted == 0
    assert summary.unclocked == 0
    assert channel.sent == []
    assert alerts_sent_for(db, request_id) == frozenset()


def test_a_closed_delegating_obligation_is_not_alerted_even_mid_flight(db):
    # Fix round 1, Finding 1. The register row is closed, but Fides' side
    # is still merely "pending" — not "complete", so the discharge check
    # (skipped_discharged) would NOT have caught this on its own. The
    # register's own status must take priority regardless of what Fides
    # separately reports.
    ensure_kenyan_policies(db)
    request_id = record_request(
        db, right="access", subject_identifier=_subject(),
        owner_email="ops@customer.co.ke",
    )
    delegate(db, request_id=request_id)
    _set_deadline(db, request_id, NOW - timedelta(hours=1))  # would otherwise breach
    record_decision(
        db, request_id=request_id, outcome="granted",
        grounds="access package delivered", decided_by="dpo@customer.co.ke",
    )

    channel = LoggingChannel()
    summary = run_deadline_alerts(db, channel=channel, now=NOW)

    assert summary.skipped_closed == 1
    assert summary.skipped_discharged == 0
    assert summary.attempted == 0
    assert channel.sent == []
    assert alerts_sent_for(db, request_id) == frozenset()


def test_a_vanished_delegated_request_is_reported_not_alerted(db):
    # Acceptance 5. A stored id that resolves to nothing is a data-integrity
    # problem, not a deadline problem.
    ensure_kenyan_policies(db)
    request_id = record_request(
        db, right="access", subject_identifier=_subject(),
        owner_email="ops@customer.co.ke",
    )
    fides_id = delegate(db, request_id=request_id)
    _set_deadline(db, request_id, NOW - timedelta(hours=1))
    db.execute(
        sqlalchemy.text("DELETE FROM privacyrequest WHERE id = :id"), {"id": fides_id}
    )

    channel = LoggingChannel()
    summary = run_deadline_alerts(db, channel=channel, now=NOW)

    assert summary.vanished == 1
    assert summary.attempted == 0
    assert summary.skipped_discharged == 0
    assert channel.sent == []
    assert alerts_sent_for(db, request_id) == frozenset()


def test_an_unowned_obligation_goes_to_the_escalation_address(db, monkeypatch):
    # Acceptance 6 / D-AL-5. And with none configured, it is logged and
    # counted in summary.unowned — never dropped.
    monkeypatch.delenv("PRIVACYCARE_DPO_EMAIL", raising=False)
    monkeypatch.setenv("PRIVACYCARE_ALERT_ESCALATION_EMAIL", "escalate@customer.co.ke")
    scope = _scope_alert_run(monkeypatch)

    request_id = record_request(db, right="access", subject_identifier=_subject())
    scope.add(request_id)
    _set_deadline(db, request_id, NOW - timedelta(hours=1))

    channel = LoggingChannel()
    summary = run_deadline_alerts(db, channel=channel, now=NOW)

    assert summary.unowned == 1
    assert (summary.attempted, summary.sent, summary.failed) == (1, 1, 0)
    assert channel.sent[0].recipient == "escalate@customer.co.ke"
    assert channel.sent[0].owner_email is None

    # With none configured: logged and counted, never dropped, never sent.
    monkeypatch.delenv("PRIVACYCARE_ALERT_ESCALATION_EMAIL", raising=False)
    other_id = record_request(db, right="access", subject_identifier=_subject())
    scope.add(other_id)
    _set_deadline(db, other_id, NOW - timedelta(hours=1))

    again = run_deadline_alerts(db, channel=channel, now=NOW)
    assert again.unowned == 1
    assert (again.attempted, again.sent, again.failed) == (0, 0, 0)
    assert alerts_sent_for(db, other_id) == frozenset()


def test_a_blank_escalation_address_is_treated_the_same_as_unset(db, monkeypatch):
    # M3, final review of plan 15. `PRIVACYCARE_ALERT_ESCALATION_EMAIL=`
    # (present but empty) reads back as `""` from os.environ.get, which
    # passes an `is None` check -- the bug this test catches -- and would
    # be sent as the destination and recorded in the ledger as delivered,
    # never retried, to nobody. `if not recipient` (the fix) treats "" the
    # same as unset: logged, counted in unowned, nothing attempted.
    monkeypatch.delenv("PRIVACYCARE_DPO_EMAIL", raising=False)
    monkeypatch.setenv("PRIVACYCARE_ALERT_ESCALATION_EMAIL", "")
    scope = _scope_alert_run(monkeypatch)

    request_id = record_request(db, right="access", subject_identifier=_subject())
    scope.add(request_id)
    _set_deadline(db, request_id, NOW - timedelta(hours=1))

    channel = LoggingChannel()
    summary = run_deadline_alerts(db, channel=channel, now=NOW)

    assert summary.unowned == 1
    assert (summary.attempted, summary.sent, summary.failed) == (0, 0, 0)
    assert channel.sent == []
    assert alerts_sent_for(db, request_id) == frozenset()


def test_a_channel_failure_leaves_the_alert_unsent_and_unrecorded(db):
    # D-AL-7 / acceptance 7. If a failed send were recorded, the unique
    # constraint would suppress the retry forever — the owner would never be
    # told, and the ledger would claim they had been.
    request_id = record_request(
        db, right="access", subject_identifier=_subject(),
        owner_email="ops@customer.co.ke",
    )
    _set_deadline(db, request_id, NOW + timedelta(days=1))

    summary = run_deadline_alerts(db, channel=_FailingChannel({request_id}), now=NOW)

    assert (summary.sent, summary.failed) == (0, 1)
    assert alerts_sent_for(db, request_id) == frozenset()

    # ...and the next run, with a working channel, still delivers it.
    again = run_deadline_alerts(db, channel=LoggingChannel(), now=NOW)
    assert again.sent == 1


def test_one_failure_does_not_stop_the_rest_of_the_run(db):
    doomed = record_request(db, right="access", subject_identifier=_subject(),
                            owner_email="a@customer.co.ke")
    healthy = record_request(db, right="access", subject_identifier=_subject(),
                             owner_email="b@customer.co.ke")
    for request_id in (doomed, healthy):
        _set_deadline(db, request_id, NOW + timedelta(days=1))

    channel = _FailingChannel({doomed})
    summary = run_deadline_alerts(db, channel=channel, now=NOW)

    assert (summary.sent, summary.failed) == (1, 1)
    assert [a.dsr_request_id for a in channel.sent] == [healthy]


def test_the_statuses_are_read_in_one_query_not_one_per_row(db):
    # Residual R2. PrivacyRequest carries multi-megabyte columns; Fides ships
    # query_without_large_columns precisely to keep them out of list reads.
    # A per-row load is an OOM waiting for volume.
    ensure_kenyan_policies(db)
    for _ in range(3):
        request_id = record_request(db, right="access", subject_identifier=_subject(),
                                    owner_email="ops@customer.co.ke")
        delegate(db, request_id=request_id)
        _set_deadline(db, request_id, NOW + timedelta(days=1))

    seen: list = []

    @sqlalchemy.event.listens_for(db.get_bind(), "before_cursor_execute")
    def _record(conn, cursor, statement, *args):  # noqa: ANN001
        if "privacyrequest" in statement.lower():
            seen.append(statement)

    try:
        run_deadline_alerts(db, channel=LoggingChannel(), now=NOW)
    finally:
        sqlalchemy.event.remove(db.get_bind(), "before_cursor_execute", _record)

    assert len(seen) == 1, f"expected one batched read, got {len(seen)}"


def test_the_alert_ledger_is_read_in_one_query_not_one_per_row(db):
    # M1, final review of plan 15. alerts_sent_for used to be called once
    # per open, clocked, non-discharged row -- issued even for rows that
    # turn out not to be due once alert_due looks at the threshold, which
    # is most of the register on most days. Batched the same way as the
    # Fides-status read just above (test_the_statuses_are_read_in_one_
    # query_not_one_per_row), via alerts_sent_for_many.
    for _ in range(3):
        request_id = record_request(db, right="access", subject_identifier=_subject(),
                                    owner_email="ops@customer.co.ke")
        _set_deadline(db, request_id, NOW + timedelta(days=1))

    seen: list = []

    @sqlalchemy.event.listens_for(db.get_bind(), "before_cursor_execute")
    def _record(conn, cursor, statement, *args):  # noqa: ANN001
        lowered = statement.lower()
        if "select" in lowered and "privacycare_dsr_alert" in lowered:
            seen.append(statement)

    try:
        run_deadline_alerts(db, channel=LoggingChannel(), now=NOW)
    finally:
        sqlalchemy.event.remove(db.get_bind(), "before_cursor_execute", _record)

    assert len(seen) == 1, f"expected one batched read, got {len(seen)}"


def test_each_successfully_recorded_alert_is_committed_immediately(db, monkeypatch):
    # I1, final review of plan 15. Before this fix, run_deadline_alerts
    # never committed at all -- only _scheduled_dsr_deadline_alerts did,
    # once, after the whole loop. Session.__exit__ closes without
    # committing, so an escaping exception anywhere in the loop (an
    # unrecognised right raising ValueError out of timeline_days, an OOM
    # kill, a redeploy) discarded every ledger row from the run even though
    # the messages for rows processed earlier had already gone out.
    #
    # The `db` fixture already monkeypatches `commit` to `flush` (never a
    # no-op) so nothing here survives the test's own rollback -- what this
    # proves is that `db.commit` is now CALLED once per successfully
    # recorded alert, not once at the very end of the run.
    ids = []
    for _ in range(3):
        request_id = record_request(db, right="access", subject_identifier=_subject(),
                                    owner_email="ops@customer.co.ke")
        _set_deadline(db, request_id, NOW - timedelta(hours=1))
        ids.append(request_id)

    commit_calls = []
    real_commit = db.commit  # the fixture's flush stand-in

    def _counting_commit():
        real_commit()
        commit_calls.append(True)

    monkeypatch.setattr(db, "commit", _counting_commit)

    summary = run_deadline_alerts(db, channel=LoggingChannel(), now=NOW)

    assert summary.sent == 3
    assert len(commit_calls) == 3, (
        f"expected one commit per successfully recorded alert (3), got "
        f"{len(commit_calls)} -- commits are batched to the end of the "
        f"run rather than happening immediately after each success"
    )
    for request_id in ids:
        assert alerts_sent_for(db, request_id) == frozenset({"breached"})


def test_a_crash_mid_run_still_commits_the_alerts_already_sent(db, monkeypatch):
    # I1. The bounded-loss claim directly: a row processed BEFORE an
    # escaping exception must already be committed by the time the
    # exception propagates out of run_deadline_alerts, so the next run
    # (the retry a caught D-AL-7 failure relies on) does not re-send an
    # alert that already went out. `timeline_days` is forced to raise on
    # the SECOND row only -- the same failure mode as an unrecognised
    # right, uncaught anywhere in the loop (alert_job.py calls it directly,
    # with no try/except, unlike channel.send).
    # The live register now permanently carries the demo seed's own DSR
    # rows (D-SEED-8), most of them open+clocked and so also processed by
    # this loop — meaning `timeline_days` gets called on THEM too, and the
    # "second row" this test means to crash on is not reliably `crashes_id`
    # any more. Scope the run to just this test's own two rows first (see
    # `_scope_alert_run`'s own docstring), so "the second row" is
    # unambiguous again regardless of what else the register holds.
    scope = _scope_alert_run(monkeypatch)

    ok_id = record_request(db, right="access", subject_identifier=_subject(),
                           owner_email="a@customer.co.ke")
    scope.add(ok_id)
    _set_deadline(db, ok_id, NOW - timedelta(hours=1))
    crashes_id = record_request(db, right="access", subject_identifier=_subject(),
                                owner_email="b@customer.co.ke")
    scope.add(crashes_id)
    _set_deadline(db, crashes_id, NOW - timedelta(hours=1))

    real_timeline_days = alert_job_module.timeline_days
    calls = {"n": 0}

    def _timeline_days_that_crashes_on_the_second_row(db_, right):
        calls["n"] += 1
        if calls["n"] > 1:
            raise ValueError("simulated crash mid-run")
        return real_timeline_days(db_, right)

    monkeypatch.setattr(
        alert_job_module, "timeline_days", _timeline_days_that_crashes_on_the_second_row
    )

    commit_calls = []
    real_commit = db.commit
    monkeypatch.setattr(
        db, "commit", lambda: (real_commit(), commit_calls.append(True))
    )

    with pytest.raises(ValueError, match="simulated crash mid-run"):
        run_deadline_alerts(db, channel=LoggingChannel(), now=NOW)

    # Exactly one commit happened -- for the row processed before the
    # crash. Without the fix, this would be zero: nothing commits until
    # the whole loop finishes, which it never does here.
    assert len(commit_calls) == 1
    assert alerts_sent_for(db, ok_id) == frozenset({"breached"})


def test_initiate_scheduled_dsr_alerts_is_a_noop_under_test_mode(db):
    # CONFIG.test_mode is true for the whole suite (FIDES__TEST_MODE=true,
    # pyproject.toml's pytest config) — this must return immediately rather
    # than asserting the scheduler is running (it is not, in a test
    # process) or registering a real cron job.
    initiate_scheduled_dsr_alerts()
