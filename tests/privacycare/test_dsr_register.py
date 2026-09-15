"""The register: PrivacyCare's record of a Kenyan obligation.

Barbara ruled on 2026-09-15 that this, not Fides' privacy request, is the record
of truth (spec D-DSR-1). Two of the six rights move no data and so have no Fides
side at all; the register is the whole record for them.
"""
import uuid
from datetime import datetime, timezone

import pytest
import sqlalchemy
from sqlalchemy.orm import Session

from fides.api.privacycare.dsr.register import (
    get_request,
    list_requests,
    record_decision,
    record_notification,
    record_request,
    resolve_owner,
)
from fides.api.privacycare.dsr.timelines import seed_timelines

DB_URL = "postgresql://postgres:fides@127.0.0.1:5442/fides"


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


def test_an_access_request_carries_kenyas_seven_days_not_fides_forty_five(db):
    # The single most important assertion in this plan. Fides ships every policy
    # with execution_timeframe=45, a CCPA number. A Kenyan access request that
    # is late on day 8 must not show 37 days remaining.
    request_id = record_request(db, right="access", subject_identifier=_subject())

    row = get_request(db, request_id)

    elapsed = row["deadline_at"] - row["received_at"]
    assert elapsed.days == 7


def test_objection_is_recorded_without_a_deadline(db):
    # OQ-PRIVACY-02 is open. The right is still exercisable — it is accepted and
    # reported as unclocked rather than refused or given a guessed date.
    request_id = record_request(db, right="objection", subject_identifier=_subject())

    row = get_request(db, request_id)

    assert row["deadline_at"] is None
    assert row["status"] == "open"


def test_changing_the_timeline_does_not_move_a_live_deadline(db):
    # The deadline is frozen at creation. If Carol later answers OQ-DSR-01 and
    # the access clock changes, obligations already in flight keep the deadline
    # the customer was told about.
    request_id = record_request(db, right="access", subject_identifier=_subject())
    before = get_request(db, request_id)["deadline_at"]

    db.execute(
        sqlalchemy.text(
            'UPDATE privacycare_dsr_timeline SET days = 3 WHERE "right" = \'access\''
        )
    )

    assert get_request(db, request_id)["deadline_at"] == before


def test_an_unknown_right_is_rejected(db):
    with pytest.raises(ValueError, match="marriage"):
        record_request(db, right="marriage", subject_identifier=_subject())


def test_a_missing_timeline_row_is_an_unseeded_database_error_not_unclocked(db):
    # Ruling 3 (carried from Task 1's review): timeline_days returns None both
    # for objection's real "unclocked" state and for a right whose timeline row
    # was never seeded at all. The register must not conflate the two — an
    # unseeded database is an engineering error, not a policy choice about when
    # a controller is in breach. This must be caught by checking the timeline
    # row's *presence*, not by trusting a None value.
    db.execute(
        sqlalchemy.text('DELETE FROM privacycare_dsr_timeline WHERE "right" = :right'),
        {"right": "erasure"},
    )

    with pytest.raises(ValueError, match="erasure"):
        record_request(db, right="erasure", subject_identifier=_subject())


def test_an_explicit_owner_wins(db):
    resolution = resolve_owner(db, explicit="dpo@customer.co.ke", business_process_id=None)
    assert resolution == ("dpo@customer.co.ke", "explicit")


def test_the_business_process_owner_is_used_when_no_owner_is_given(db):
    # D-DSR-8: PrivacyCare already holds business-process owners from plan 08,
    # which is precisely the field Fides' privacy request lacks.
    process_id = f"bp_{uuid.uuid4().hex[:12]}"
    db.execute(
        sqlalchemy.text(
            "INSERT INTO privacycare_business_process (id, name, owner_email) "
            "VALUES (:id, :name, :email)"
        ),
        {"id": process_id, "name": "Fuel card applications", "email": "ops@customer.co.ke"},
    )

    resolution = resolve_owner(db, explicit=None, business_process_id=process_id)

    assert resolution == ("ops@customer.co.ke", "business_process")


def test_an_unresolvable_owner_is_flagged_not_rejected(db, monkeypatch):
    monkeypatch.delenv("PRIVACYCARE_DPO_EMAIL", raising=False)

    resolution = resolve_owner(db, explicit=None, business_process_id=None)

    assert resolution == (None, "unassigned")
    # And the request is still accepted — an obligation the customer owes does
    # not stop existing because we cannot work out who should answer it.
    request_id = record_request(db, right="erasure", subject_identifier=_subject())
    assert get_request(db, request_id)["owner_email"] is None


def test_a_decision_records_its_grounds_and_its_author(db):
    # Restriction and objection are decision-and-notify rights: the grounds are
    # the deliverable, because a regulator asks why, not whether.
    request_id = record_request(db, right="restriction", subject_identifier=_subject())

    record_decision(db, request_id=request_id, outcome="refused",
                    grounds="Processing is required by the Energy Act.",
                    decided_by="carol@serianu.com")

    row = get_request(db, request_id)
    assert row["outcome"] == "refused"
    assert row["outcome_grounds"].startswith("Processing is required")
    assert row["decided_by"] == "carol@serianu.com"
    assert row["decided_at"] is not None
    assert row["status"] == "closed"


def test_a_decision_without_grounds_is_rejected(db):
    request_id = record_request(db, right="objection", subject_identifier=_subject())
    with pytest.raises(ValueError, match="grounds"):
        record_decision(db, request_id=request_id, outcome="refused", grounds="   ",
                        decided_by="carol@serianu.com")


def test_an_unknown_outcome_is_rejected(db):
    request_id = record_request(db, right="objection", subject_identifier=_subject())
    with pytest.raises(ValueError, match="maybe"):
        record_decision(db, request_id=request_id, outcome="maybe", grounds="x",
                        decided_by="carol@serianu.com")


def test_notifying_the_subject_is_recorded_separately_from_deciding(db):
    # The clock the brief names for restriction is a *refusal notification*
    # clock. Deciding and telling the subject are different events and the
    # second is the one the deadline is about.
    request_id = record_request(db, right="restriction", subject_identifier=_subject())
    record_decision(db, request_id=request_id, outcome="granted", grounds="Upheld.",
                    decided_by="carol@serianu.com")
    when = datetime.now(timezone.utc)

    record_notification(db, request_id=request_id, notified_at=when)

    assert get_request(db, request_id)["subject_notified_at"] is not None


def test_the_register_lists_by_right_and_status(db):
    subject = _subject()
    record_request(db, right="access", subject_identifier=subject)
    record_request(db, right="objection", subject_identifier=subject)

    rights = {r["right"] for r in list_requests(db) if r["subject_identifier"] == subject}

    assert rights == {"access", "objection"}
    only_access = [r for r in list_requests(db, right="access")
                   if r["subject_identifier"] == subject]
    assert len(only_access) == 1


# --- Fix round 1 on plan 14 task 4 (coordinator ruling): owner_source must
# be STORED at creation, not inferred at read time. An earlier version of
# the HTTP layer inferred "explicit" for any row with a non-null
# owner_email, which silently misreported a business_process- or
# configured_dpo-resolved owner as if a human had typed it in. These four
# tests lock down that record_request now persists the real D-DSR-8 source
# resolve_owner computed, for each of its four possible values —
# configured_dpo in particular had NO coverage anywhere in this suite
# before this fix (test_an_unresolvable_owner_is_flagged_not_rejected only
# ever exercised the "nothing configured" -> unassigned branch).


def test_an_explicit_owner_source_is_stored_on_the_row(db):
    request_id = record_request(
        db,
        right="restriction",
        subject_identifier=_subject(),
        owner_email="dpo@customer.co.ke",
    )
    row = get_request(db, request_id)
    assert row["owner_email"] == "dpo@customer.co.ke"
    assert row["owner_source"] == "explicit"


def test_a_business_process_owner_source_is_stored_on_the_row(db):
    process_id = f"bp_{uuid.uuid4().hex[:12]}"
    db.execute(
        sqlalchemy.text(
            "INSERT INTO privacycare_business_process (id, name, owner_email) "
            "VALUES (:id, :name, :email)"
        ),
        {"id": process_id, "name": "Fuel card applications", "email": "ops@customer.co.ke"},
    )

    request_id = record_request(
        db,
        right="restriction",
        subject_identifier=_subject(),
        business_process_id=process_id,
    )

    row = get_request(db, request_id)
    assert row["owner_email"] == "ops@customer.co.ke"
    assert row["owner_source"] == "business_process"


def test_a_configured_dpo_owner_source_is_stored_on_the_row(db, monkeypatch):
    monkeypatch.setenv("PRIVACYCARE_DPO_EMAIL", "dpo-fallback@customer.co.ke")

    request_id = record_request(db, right="restriction", subject_identifier=_subject())

    row = get_request(db, request_id)
    assert row["owner_email"] == "dpo-fallback@customer.co.ke"
    assert row["owner_source"] == "configured_dpo"


def test_an_unassigned_owner_source_is_stored_on_the_row(db, monkeypatch):
    monkeypatch.delenv("PRIVACYCARE_DPO_EMAIL", raising=False)

    request_id = record_request(db, right="restriction", subject_identifier=_subject())

    row = get_request(db, request_id)
    assert row["owner_email"] is None
    assert row["owner_source"] == "unassigned"
