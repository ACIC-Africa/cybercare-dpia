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

from fides.api.privacycare.dsr.alert_job import (
    RunSummary,
    initiate_scheduled_dsr_alerts,
    run_deadline_alerts,
)
from fides.api.privacycare.dsr.alerts import alerts_sent_for
from fides.api.privacycare.dsr.channels import LoggingChannel
from fides.api.privacycare.dsr.delegation import delegate, ensure_kenyan_policies
from fides.api.privacycare.dsr.register import record_request
from fides.api.privacycare.dsr.timelines import seed_timelines

DB_URL = "postgresql://postgres:fides@127.0.0.1:5442/fides"
NOW = datetime(2026, 9, 15, 7, 0, tzinfo=timezone.utc)


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


def test_an_obligation_at_its_threshold_warns_its_owner_once(db):
    # Acceptance 1. The second run the same day must send nothing.
    request_id = record_request(
        db, right="access", subject_identifier=_subject(),
        owner_email="ops@customer.co.ke",
    )
    # access is a 7-day clock; warn_threshold(7) == 3, so 3 days out is
    # exactly at the threshold.
    _set_deadline(db, request_id, NOW + timedelta(days=3))

    channel = LoggingChannel()
    first = run_deadline_alerts(db, channel=channel, now=NOW)

    assert first == RunSummary(
        attempted=1, sent=1, failed=0, skipped_discharged=0,
        unclocked=0, vanished=0, unowned=0,
    )
    assert len(channel.sent) == 1
    assert channel.sent[0].kind == "approaching"
    assert channel.sent[0].recipient == "ops@customer.co.ke"
    assert alerts_sent_for(db, request_id) == frozenset({"approaching"})

    second = run_deadline_alerts(db, channel=channel, now=NOW)
    assert second == RunSummary(
        attempted=0, sent=0, failed=0, skipped_discharged=0,
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


def test_an_objection_is_never_alerted_and_is_reported_as_unclocked(db):
    # Acceptance 3 / D-AL-4. summary.unclocked counts it; summary.attempted
    # does not. Skipping it silently would make an open question invisible.
    request_id = record_request(
        db, right="objection", subject_identifier=_subject(),
        owner_email="ops@customer.co.ke",
    )

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

    request_id = record_request(db, right="access", subject_identifier=_subject())
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
    _set_deadline(db, other_id, NOW - timedelta(hours=1))

    again = run_deadline_alerts(db, channel=channel, now=NOW)
    assert again.unowned == 1
    assert (again.attempted, again.sent, again.failed) == (0, 0, 0)
    assert alerts_sent_for(db, other_id) == frozenset()


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


def test_initiate_scheduled_dsr_alerts_is_a_noop_under_test_mode(db):
    # CONFIG.test_mode is true for the whole suite (FIDES__TEST_MODE=true,
    # pyproject.toml's pytest config) — this must return immediately rather
    # than asserting the scheduler is running (it is not, in a test
    # process) or registering a real cron job.
    initiate_scheduled_dsr_alerts()
