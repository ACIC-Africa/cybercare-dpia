"""Where a Kenyan right needs data to move, Fides moves it.

The register owns the obligation; Fides owns the execution. The risk this
creates — and the reason D-DSR-7 exists — is that there are now two deadline
numbers for the same request, on two different screens.
"""
import uuid

import pytest
import sqlalchemy
from sqlalchemy.orm import Session

from fides.api.models.privacy_request import ProvidedIdentity
from fides.api.privacycare.dsr.delegation import (
    DELEGATING_RIGHTS,
    delegate,
    ensure_kenyan_policies,
    kenyan_policy_key,
)
from fides.api.privacycare.dsr.register import get_request, record_request
from fides.api.privacycare.dsr.timelines import seed_timelines, timeline_days

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


def test_only_the_data_moving_rights_delegate(db):
    assert set(DELEGATING_RIGHTS) == {"access", "erasure", "portability"}
    for right in ("rectification", "restriction", "objection"):
        assert kenyan_policy_key(right) is None


def test_the_two_deadlines_agree_for_every_delegating_right(db):
    # D-DSR-7. A DPO who sees 7 days on one screen and 45 on another will trust
    # neither. This is the test that keeps them from disagreeing — so it must
    # read the number Ethyca's own screens actually display (policy.execution_timeframe
    # as persisted), not the value ensure_kenyan_policies merely computed in memory.
    ensure_kenyan_policies(db)

    for right, policy_key in DELEGATING_RIGHTS.items():
        stored_timeframe = db.execute(
            sqlalchemy.text(
                "SELECT execution_timeframe FROM policy WHERE key = :key"
            ),
            {"key": policy_key},
        ).scalar()
        assert stored_timeframe == timeline_days(db, right), (
            f"{right}: the persisted Fides policy timeframe and the Kenyan "
            "clock disagree"
        )


def test_no_kenyan_policy_keeps_the_shipped_forty_five_day_default(db):
    ensure_kenyan_policies(db)
    rows = db.execute(
        sqlalchemy.text(
            "SELECT key, execution_timeframe FROM policy WHERE key LIKE 'privacycare_kenya_%'"
        )
    ).all()
    assert rows, "no Kenyan policies were created"
    assert all(timeframe != 45 for _, timeframe in rows)


def test_delegating_an_access_request_creates_a_fides_privacy_request(db):
    ensure_kenyan_policies(db)
    request_id = record_request(db, right="access", subject_identifier=_subject())

    fides_id = delegate(db, request_id=request_id)

    assert fides_id is not None
    assert get_request(db, request_id)["fides_privacy_request_id"] == fides_id
    exists = db.execute(
        sqlalchemy.text("SELECT count(*) FROM privacyrequest WHERE id = :id"),
        {"id": fides_id},
    ).scalar()
    assert exists == 1


def test_delegating_attaches_the_registers_subject_identifier_as_the_fides_identity(db):
    # D-DSR-7's failure mode isn't only the deadline: a delegated request
    # with no identity attached would show up on Ethyca's own screens as
    # belonging to nobody. persist_identity must actually run.
    ensure_kenyan_policies(db)
    subject = _subject()
    request_id = record_request(db, right="access", subject_identifier=subject)

    fides_id = delegate(db, request_id=request_id)

    identity = (
        db.query(ProvidedIdentity)
        .filter_by(privacy_request_id=fides_id, field_name="external_id")
        .first()
    )
    assert identity is not None, "no identity was persisted for the delegated request"
    assert identity.encrypted_value["value"] == subject


def test_restriction_creates_no_fides_request_at_all(db):
    # The whole reason the register exists: this right moves no data, and Fides
    # has no shape for it. A row in privacyrequest here would be a lie.
    ensure_kenyan_policies(db)
    before = db.execute(sqlalchemy.text("SELECT count(*) FROM privacyrequest")).scalar()
    request_id = record_request(db, right="restriction", subject_identifier=_subject())

    assert delegate(db, request_id=request_id) is None
    assert get_request(db, request_id)["fides_privacy_request_id"] is None
    after = db.execute(sqlalchemy.text("SELECT count(*) FROM privacyrequest")).scalar()
    assert after == before


def test_objection_creates_no_fides_request_at_all(db):
    ensure_kenyan_policies(db)
    before = db.execute(sqlalchemy.text("SELECT count(*) FROM privacyrequest")).scalar()
    request_id = record_request(db, right="objection", subject_identifier=_subject())

    assert delegate(db, request_id=request_id) is None
    assert db.execute(
        sqlalchemy.text("SELECT count(*) FROM privacyrequest")
    ).scalar() == before


def test_delegating_twice_does_not_create_a_second_fides_request(db):
    ensure_kenyan_policies(db)
    request_id = record_request(db, right="erasure", subject_identifier=_subject())

    first = delegate(db, request_id=request_id)
    second = delegate(db, request_id=request_id)

    assert first == second


def test_portability_delegates_through_an_access_policy_on_a_thirty_day_clock(db):
    # D-DSR-3: portability differs from access in format obligation and clock,
    # not in the action the runner performs.
    ensure_kenyan_policies(db)
    request_id = record_request(db, right="portability", subject_identifier=_subject())

    delegate(db, request_id=request_id)

    row = get_request(db, request_id)
    assert (row["deadline_at"] - row["received_at"]).days == 30
    action = db.execute(
        sqlalchemy.text(
            "SELECT r.action_type FROM rule r JOIN policy p ON r.policy_id = p.id "
            "WHERE p.key = :key"
        ),
        {"key": DELEGATING_RIGHTS["portability"]},
    ).scalar()
    assert action == "access"


def test_ensuring_policies_twice_is_idempotent(db):
    ensure_kenyan_policies(db)
    ensure_kenyan_policies(db)
    count = db.execute(
        sqlalchemy.text("SELECT count(*) FROM policy WHERE key LIKE 'privacycare_kenya_%'")
    ).scalar()
    assert count == len(DELEGATING_RIGHTS)
