"""Where a Kenyan right needs data to move, Fides moves it.

The register owns the obligation; Fides owns the execution. The risk this
creates — and the reason D-DSR-7 exists — is that there are now two deadline
numbers for the same request, on two different screens.
"""
import uuid

import pytest
import sqlalchemy
from sqlalchemy.orm import Session

from fides.api.models.masking_secret import MaskingSecret
from fides.api.models.privacy_request import PrivacyRequest, ProvidedIdentity
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


# --- Minor finding (final review): the actual D-DSR-7 claim is "two
# deadline numbers on two screens agree" — the tests above only ever
# checked policy.execution_timeframe (the CONFIGURED clock), never the
# per-request due_date Fides actually stamps onto the created
# privacyrequest (the clock a reviewer looking at THAT screen would see).


def test_the_delegated_privacyrequests_due_date_matches_the_registers_deadline(db):
    ensure_kenyan_policies(db)
    request_id = record_request(db, right="access", subject_identifier=_subject())

    fides_id = delegate(db, request_id=request_id)

    row = get_request(db, request_id)
    due_date = db.execute(
        sqlalchemy.text("SELECT due_date FROM privacyrequest WHERE id = :id"),
        {"id": fides_id},
    ).scalar()
    assert due_date == row["deadline_at"], (
        "privacyrequest.due_date and privacycare_dsr_request.deadline_at "
        "disagree — the actual D-DSR-7 claim"
    )


# --- I2 (final review, D-DSR-7). The timeline table is deliberately
# editable without a deploy; the moment it changes, an already-provisioned
# Fides policy keeps its old execution_timeframe until ensure_kenyan_
# policies() is re-run. delegate() must refuse rather than silently create
# a request whose due_date would be computed against a clock the register
# no longer agrees with.
#
# The pre-existing "two deadlines agree" test above calls
# ensure_kenyan_policies() one line above its own assertion — it can only
# ever prove the writer works, never that anything downstream notices when
# the two numbers have since drifted apart. This test edits the timeline
# row AFTER provisioning, which the writer above never does.


def test_delegate_refuses_when_the_policy_and_timeline_have_drifted(db):
    ensure_kenyan_policies(db)
    request_id = record_request(db, right="access", subject_identifier=_subject())

    # Simulate Carol answering an open question with a new number (or an
    # operator hand-editing the policy in Ethyca's admin UI has the
    # identical effect) — the policy was provisioned at 7 days and has not
    # been re-synced.
    db.execute(
        sqlalchemy.text(
            'UPDATE privacycare_dsr_timeline SET days = 3 WHERE "right" = \'access\''
        )
    )

    with pytest.raises(ValueError) as caught:
        delegate(db, request_id=request_id)

    # Both numbers, named, per the spec: an operator must be able to tell
    # which side is stale without opening two other screens.
    assert "7" in str(caught.value)
    assert "3" in str(caught.value)
    # And nothing was created on the stale clock.
    assert get_request(db, request_id)["fides_privacy_request_id"] is None


def test_delegate_does_not_refuse_an_already_delegated_request_on_later_drift(db):
    # The drift check must gate CREATING a new request on a stale clock, not
    # every call — a request already delegated on whatever clock was live at
    # the time must keep returning its existing id, not start failing
    # because the timeline moved on afterward.
    ensure_kenyan_policies(db)
    request_id = record_request(db, right="access", subject_identifier=_subject())
    first = delegate(db, request_id=request_id)

    db.execute(
        sqlalchemy.text(
            'UPDATE privacycare_dsr_timeline SET days = 3 WHERE "right" = \'access\''
        )
    )

    second = delegate(db, request_id=request_id)
    assert second == first


# --- I1 (partial, per ruling). Masking secrets ARE persisted; queueing,
# cache_data, error-notification dispatch and duplicate detection are
# deliberately NOT — see delegate()'s own docstring for the full reasoning.
# Without persisted secrets, an erasure request would log "Secret type ...
# expected but was not present" once it is eventually approved and run.


def test_delegating_an_erasure_request_persists_masking_secrets(db):
    ensure_kenyan_policies(db)
    request_id = record_request(db, right="erasure", subject_identifier=_subject())

    fides_id = delegate(db, request_id=request_id)

    secrets = (
        db.query(MaskingSecret).filter_by(privacy_request_id=fides_id).all()
    )
    assert secrets, (
        "no masking secrets were persisted for the delegated erasure "
        "request — the eventual run would log 'Secret type ... expected "
        "but was not present'"
    )


# --- I5 (narrowed, per ruling). delegate() must not hand back a STORED
# fides_privacy_request_id whose privacyrequest row has since vanished
# (e.g. deleted directly against Ethyca's own tables) as though it were
# still live — it must create a fresh one instead.


def test_delegate_creates_a_fresh_request_when_the_stored_one_has_vanished(db):
    ensure_kenyan_policies(db)
    request_id = record_request(db, right="access", subject_identifier=_subject())
    first = delegate(db, request_id=request_id)

    db.execute(
        sqlalchemy.text("DELETE FROM privacyrequest WHERE id = :id"), {"id": first}
    )

    second = delegate(db, request_id=request_id)

    assert second is not None
    assert second != first, "a vanished id must not be returned as though live"
    assert PrivacyRequest.get_by(db, field="id", value=second) is not None
    assert get_request(db, request_id)["fides_privacy_request_id"] == second
