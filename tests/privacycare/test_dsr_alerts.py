"""Which obligation is due an alert, and the ledger that stops it repeating.

D-DSR-5: an alert that repeats gets the channel muted, and a muted channel is
worse than no channel. The once-only guarantee is a unique constraint, not a
flag — a flag loses to a second worker, a restart mid-run, or a retry.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy
from sqlalchemy.exc import IntegrityError
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


# --- Fix round 1 findings -----------------------------------------------
#
# Finding 1 (Important): record_alert's False return is documented to mean
# exactly one thing — "already sent". A bogus dsr_request_id trips the
# table's foreign key, not its once-only unique constraint, and must raise
# rather than lie by returning False (which would suppress every future
# retry and let the ledger assert an owner was told when they never were).


def test_a_bogus_request_id_raises_instead_of_lying_about_already_sent(db):
    bogus_id = f"no-such-request-{uuid.uuid4().hex[:8]}"

    with pytest.raises(IntegrityError):
        record_alert(db, dsr_request_id=bogus_id, kind=APPROACHING,
                     channel="logging", recipient=None)


# Findings 2-4 (Minor): boundaries the original ten tests asserted only by
# inspection of alert_due's source, not by exercising them.


def test_a_deadline_present_with_no_days_allowed_is_never_due():
    # Finding 2: the existing unclocked test passes deadline_at AND
    # days_allowed as None together, so it returns via the earlier
    # `deadline_at is None` branch and never reaches the `days_allowed is
    # None` guard below it. This combination should not occur on a real row
    # (deadline_at and days_allowed are always computed together — see
    # dsr/timelines.py deadline_for), but alert_due trusts nothing it was
    # not given: no threshold means no warning to compare against.
    assert alert_due(deadline_at=NOW + timedelta(days=1), days_allowed=None,
                     now=NOW, already_sent=frozenset()) is None


def test_a_deadline_exactly_at_now_counts_as_breached():
    # Finding 3: only a past deadline was exercised before. The `<=`
    # comparison in alert_due means the instant the clock reaches zero is
    # already a breach, not one tick later.
    assert alert_due(deadline_at=NOW, days_allowed=7,
                     now=NOW, already_sent=frozenset()) == BREACHED


def test_the_warning_threshold_holds_at_each_real_kenyan_clock():
    # Finding 4: the 7-day case was the only threshold boundary exercised
    # through alert_due (test_a_warning_is_due_at_the_threshold above).
    # warn_threshold's own unit test proves the arithmetic for 7/14/30, but
    # that does not prove alert_due actually compares against it correctly
    # for each — exercise the full days_allowed -> warn_threshold ->
    # comparison chain for all three of Kenya's real statutory clocks.
    assert alert_due(deadline_at=NOW + timedelta(days=5), days_allowed=14,
                     now=NOW, already_sent=frozenset()) == APPROACHING
    assert alert_due(deadline_at=NOW + timedelta(days=6), days_allowed=14,
                     now=NOW, already_sent=frozenset()) is None

    assert alert_due(deadline_at=NOW + timedelta(days=10), days_allowed=30,
                     now=NOW, already_sent=frozenset()) == APPROACHING
    assert alert_due(deadline_at=NOW + timedelta(days=11), days_allowed=30,
                     now=NOW, already_sent=frozenset()) is None
