"""Which obligation is due an alert, and the ledger that stops it repeating.

D-DSR-5: an alert that repeats gets the channel muted, and a muted channel is
worse than no channel. The once-only guarantee is a unique constraint, not a
flag — a flag loses to a second worker, a restart mid-run, or a retry.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy
from sqlalchemy.orm import Session

from fides.api.privacycare.dsr.alerts import (
    APPROACHING,
    BREACHED,
    alert_due,
    alerts_sent_for,
    record_alert,
    warn_threshold,
)
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


def test_the_threshold_is_a_third_of_the_clock_not_a_fixed_number():
    # D-AL-2: three days' notice on a 7-day statutory clock is a different
    # urgency from three days on a 30-day one. A fixed threshold gets one wrong.
    assert warn_threshold(7) == 3
    assert warn_threshold(14) == 5
    assert warn_threshold(30) == 10


def test_nothing_is_due_while_the_deadline_is_comfortably_ahead():
    assert alert_due(deadline_at=NOW + timedelta(days=6), days_allowed=7,
                     now=NOW, already_sent=frozenset()) is None


def test_a_warning_is_due_at_the_threshold():
    assert alert_due(deadline_at=NOW + timedelta(days=3), days_allowed=7,
                     now=NOW, already_sent=frozenset()) == APPROACHING


def test_a_breach_is_due_once_the_deadline_has_passed():
    assert alert_due(deadline_at=NOW - timedelta(hours=1), days_allowed=7,
                     now=NOW, already_sent=frozenset()) == BREACHED


def test_a_breach_is_due_even_when_the_warning_already_went_out():
    # They are distinct events: "this is getting close" and "this is now late"
    # are different things to tell an owner, and the second is not optional.
    assert alert_due(deadline_at=NOW - timedelta(hours=1), days_allowed=7,
                     now=NOW, already_sent=frozenset({APPROACHING})) == BREACHED


def test_nothing_repeats():
    assert alert_due(deadline_at=NOW + timedelta(days=1), days_allowed=7,
                     now=NOW, already_sent=frozenset({APPROACHING})) is None
    assert alert_due(deadline_at=NOW - timedelta(days=5), days_allowed=7,
                     now=NOW, already_sent=frozenset({APPROACHING, BREACHED})) is None


def test_an_unclocked_obligation_is_never_due_an_alert():
    # D-AL-4: objection has no statutory timeline until the SME answers
    # OQ-PRIVACY-02. No deadline means no threshold — not "due today".
    assert alert_due(deadline_at=None, days_allowed=None,
                     now=NOW, already_sent=frozenset()) is None


def test_recording_an_alert_is_idempotent(db):
    request_id = record_request(db, right="access",
                                subject_identifier=f"s-{uuid.uuid4().hex[:8]}@example.com")

    assert record_alert(db, dsr_request_id=request_id, kind=APPROACHING,
                        channel="logging", recipient="ops@customer.co.ke") is True
    assert record_alert(db, dsr_request_id=request_id, kind=APPROACHING,
                        channel="logging", recipient="ops@customer.co.ke") is False

    count = db.execute(
        sqlalchemy.text(
            "SELECT count(*) FROM privacycare_dsr_alert "
            "WHERE dsr_request_id = :id AND kind = :kind"
        ),
        {"id": request_id, "kind": APPROACHING},
    ).scalar()
    assert count == 1


def test_the_two_kinds_are_recorded_separately(db):
    request_id = record_request(db, right="erasure",
                                subject_identifier=f"s-{uuid.uuid4().hex[:8]}@example.com")
    record_alert(db, dsr_request_id=request_id, kind=APPROACHING,
                 channel="logging", recipient=None)
    record_alert(db, dsr_request_id=request_id, kind=BREACHED,
                 channel="logging", recipient=None)

    assert alerts_sent_for(db, request_id) == frozenset({APPROACHING, BREACHED})


def test_an_unknown_kind_is_rejected(db):
    request_id = record_request(db, right="access",
                                subject_identifier=f"s-{uuid.uuid4().hex[:8]}@example.com")
    with pytest.raises(ValueError, match="nagging"):
        record_alert(db, dsr_request_id=request_id, kind="nagging",
                     channel="logging", recipient=None)
