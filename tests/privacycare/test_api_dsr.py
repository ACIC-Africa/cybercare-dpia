"""The DSR register's HTTP surface.

Our own /api/v1/privacycare namespace, not Ethyca's plus/: nothing in the
shipped admin UI calls these, so taking a path in Plus's namespace would only
risk colliding with a real Plus endpoint later. Same reasoning api/processes.py
records for the business-process routes.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy
from fastapi import HTTPException
from fastapi_pagination import Params
from sqlalchemy.orm import Session

from fides.api.oauth.roles import ROLES_TO_SCOPES_MAPPING, VIEWER
from fides.api.privacycare.api.dsr import (
    _days_left,
    create_dsr_request,
    get_dsr_request,
    list_dsr_requests,
    record_dsr_decision,
    record_dsr_notification,
)
from fides.api.privacycare.api.dsr_schemas import (
    DsrDecisionRequest,
    DsrNotificationRequest,
    DsrRequestCreate,
)
from fides.api.privacycare.dsr.delegation import ensure_kenyan_policies
from fides.api.privacycare.dsr.register import get_request as _core_get_request
from fides.api.privacycare.dsr.register import list_requests as _core_list_requests
from fides.api.privacycare.dsr.timelines import seed_timelines
from fides.common.scope_registry import PRIVACYCARE_DSR_READ, PRIVACYCARE_DSR_UPDATE
from tests.privacycare.test_api_assessments import _fake_client

DB_URL = "postgresql://postgres:fides@127.0.0.1:5442/fides"


@pytest.fixture
def db(monkeypatch):
    engine = sqlalchemy.create_engine(DB_URL)
    with Session(engine) as session:
        monkeypatch.setattr(session, "commit", session.flush)
        seed_timelines(session)
        ensure_kenyan_policies(session)
        yield session
        session.rollback()


def _subject() -> str:
    return f"subject-{uuid.uuid4().hex[:8]}@example.com"


def _create(db, right, **kwargs):
    return create_dsr_request(
        DsrRequestCreate(right=right, subject_identifier=_subject(), **kwargs),
        db=db,
        client=_fake_client("carol@serianu.com"),
    )


def test_an_access_request_reports_kenyas_seven_days(db):
    created = _create(db, "access")
    assert created.days_left == 7
    assert created.deadline_at is not None


def test_objection_reports_unclocked_rather_than_a_number(db):
    # The UI must be able to say "unclocked". None is the honest answer while
    # OQ-PRIVACY-02 is open; 0 would read as "due today".
    created = _create(db, "objection")
    assert created.deadline_at is None
    assert created.days_left is None


def test_the_three_data_moving_rights_delegate_on_creation(db):
    for right in ("access", "erasure", "portability"):
        created = _create(db, right)
        assert created.fides_privacy_request_id is not None, right


def test_restriction_and_objection_never_touch_privacyrequest(db):
    before = db.execute(sqlalchemy.text("SELECT count(*) FROM privacyrequest")).scalar()
    for right in ("restriction", "objection"):
        created = _create(db, right)
        assert created.fides_privacy_request_id is None, right
    after = db.execute(sqlalchemy.text("SELECT count(*) FROM privacyrequest")).scalar()
    assert after == before


def test_an_unknown_right_is_400_naming_it(db):
    with pytest.raises(HTTPException) as caught:
        create_dsr_request(
            DsrRequestCreate(right="marriage", subject_identifier=_subject()),
            db=db,
            client=_fake_client("carol@serianu.com"),
        )
    assert caught.value.status_code == 400
    assert "marriage" in caught.value.detail


def test_an_unknown_request_is_404(db):
    with pytest.raises(HTTPException) as caught:
        get_dsr_request("no_such_request", db=db, client=_fake_client("carol@serianu.com"))
    assert caught.value.status_code == 404


def test_a_decision_without_grounds_is_400(db):
    created = _create(db, "restriction")
    with pytest.raises(HTTPException) as caught:
        record_dsr_decision(
            created.id,
            DsrDecisionRequest(outcome="refused", grounds="   "),
            db=db,
            client=_fake_client("carol@serianu.com"),
        )
    assert caught.value.status_code == 400
    assert "grounds" in caught.value.detail.lower()


def test_a_decision_and_a_notification_are_recorded_separately(db):
    created = _create(db, "restriction")

    decided = record_dsr_decision(
        created.id,
        DsrDecisionRequest(outcome="refused", grounds="Required by the Energy Act."),
        db=db,
        client=_fake_client("carol@serianu.com"),
    )
    assert decided.outcome == "refused"
    assert decided.subject_notified_at is None

    notified = record_dsr_notification(
        created.id,
        DsrNotificationRequest(),
        db=db,
        client=_fake_client("carol@serianu.com"),
    )
    assert notified.subject_notified_at is not None


def test_the_list_filters_by_right_and_returns_the_page_envelope(db):
    _create(db, "access")
    page = list_dsr_requests(
        right="access", status=None, params=Params(page=1, size=50),
        db=db, client=_fake_client("carol@serianu.com"),
    )
    assert {"items", "total", "page", "size", "pages"} <= set(page.model_dump())
    assert all(item.right == "access" for item in page.items)


def test_viewer_has_neither_dsr_scope(db):
    # I3 (final review, 2026-09-15): this used to assert Viewer had
    # PRIVACYCARE_DSR_READ (mirroring privacycare_discovery's split) and
    # only lacked PRIVACYCARE_DSR_UPDATE. That precedent doesn't hold here:
    # the register carries subject_identifier and free-text
    # outcome_grounds (a DPO's own reasoning for refusing a data subject),
    # the same kind of sensitive content upstream already excludes from
    # Viewer for PRIVACY_REQUEST_READ (see roles.py's own comment, three
    # lines above viewer_scopes' definition) — a discovery monitor, by
    # contrast, carries no subject identities at all. PENDING A PRODUCT
    # RULING (roles.py): Viewer gets neither DSR scope for now; Owner and
    # Contributor are unaffected (registry derivation, not this list).
    assert PRIVACYCARE_DSR_READ not in ROLES_TO_SCOPES_MAPPING[VIEWER]
    assert PRIVACYCARE_DSR_UPDATE not in ROLES_TO_SCOPES_MAPPING[VIEWER]


# --- Ruling 3 (carried from Task 2's review, deferred to the HTTP layer) ---
#
# record_decision and record_notification (dsr/register.py) have no guard of
# their own against re-deciding an already-closed request or overwriting an
# existing notification timestamp — they will happily UPDATE a second time.
# An HTTP surface that silently overwrote a recorded regulatory decision, or
# a recorded notification date, would be a defect: both are facts a regulator
# can ask about, and a second, different answer overwriting the first with no
# trace is exactly the kind of "unrecoverable audit trail" mistake this
# register exists to prevent. The routes below choose 409 Conflict over
# silent idempotency for both: a second POST to either sub-resource is a
# meaningfully different request (a different outcome, a different
# notification time) that deserves the caller's attention, not a No-Op.


def test_a_second_decision_on_a_closed_request_is_409(db):
    created = _create(db, "restriction")
    record_dsr_decision(
        created.id,
        DsrDecisionRequest(outcome="refused", grounds="Required by the Energy Act."),
        db=db,
        client=_fake_client("carol@serianu.com"),
    )
    with pytest.raises(HTTPException) as caught:
        record_dsr_decision(
            created.id,
            DsrDecisionRequest(outcome="granted", grounds="Changed our mind."),
            db=db,
            client=_fake_client("carol@serianu.com"),
        )
    assert caught.value.status_code == 409
    # The original decision must survive the rejected second call untouched.
    reread = get_dsr_request(created.id, db=db, client=_fake_client("carol@serianu.com"))
    assert reread.outcome == "refused"


def test_a_second_notification_on_an_already_notified_request_is_409(db):
    created = _create(db, "restriction")
    record_dsr_decision(
        created.id,
        DsrDecisionRequest(outcome="refused", grounds="Required by the Energy Act."),
        db=db,
        client=_fake_client("carol@serianu.com"),
    )
    first = record_dsr_notification(
        created.id, DsrNotificationRequest(), db=db, client=_fake_client("carol@serianu.com")
    )
    with pytest.raises(HTTPException) as caught:
        record_dsr_notification(
            created.id, DsrNotificationRequest(), db=db, client=_fake_client("carol@serianu.com")
        )
    assert caught.value.status_code == 409
    # The original notification timestamp must survive untouched.
    reread = get_dsr_request(created.id, db=db, client=_fake_client("carol@serianu.com"))
    assert reread.subject_notified_at == first.subject_notified_at


def test_deciding_a_request_that_does_not_exist_is_404(db):
    with pytest.raises(HTTPException) as caught:
        record_dsr_decision(
            "no_such_request",
            DsrDecisionRequest(outcome="refused", grounds="Required by the Energy Act."),
            db=db,
            client=_fake_client("carol@serianu.com"),
        )
    assert caught.value.status_code == 404


def test_notifying_a_request_that_does_not_exist_is_404(db):
    with pytest.raises(HTTPException) as caught:
        record_dsr_notification(
            "no_such_request",
            DsrNotificationRequest(),
            db=db,
            client=_fake_client("carol@serianu.com"),
        )
    assert caught.value.status_code == 404


# --- Fix round 1 (coordinator ruling): owner_source is now STORED at
# creation (register.record_request), not inferred at read time — an
# earlier version of _response_from_row guessed "explicit" for any row
# with a non-null owner_email, which could misreport a business_process-
# or configured_dpo-resolved owner. This test drives all four of D-DSR-8's
# possible sources through the HTTP layer and checks the API response
# reports EXACTLY what the core stored on the row — not merely a
# plausible-looking value.


def test_owner_source_round_trips_through_the_api_for_all_four_sources(db, monkeypatch):
    monkeypatch.delenv("PRIVACYCARE_DPO_EMAIL", raising=False)

    explicit = _create(db, "restriction", owner_email="dpo@customer.co.ke")
    assert explicit.owner_source == "explicit"
    assert explicit.owner_source == _core_get_request(db, explicit.id)["owner_source"]

    process_id = f"bp_{uuid.uuid4().hex[:12]}"
    db.execute(
        sqlalchemy.text(
            "INSERT INTO privacycare_business_process (id, name, owner_email) "
            "VALUES (:id, :name, :email)"
        ),
        {"id": process_id, "name": "Fuel card applications", "email": "ops@customer.co.ke"},
    )
    from_process = _create(db, "restriction", business_process_id=process_id)
    assert from_process.owner_source == "business_process"
    assert (
        from_process.owner_source
        == _core_get_request(db, from_process.id)["owner_source"]
    )

    monkeypatch.setenv("PRIVACYCARE_DPO_EMAIL", "dpo-fallback@customer.co.ke")
    from_dpo = _create(db, "restriction")
    assert from_dpo.owner_source == "configured_dpo"
    assert from_dpo.owner_source == _core_get_request(db, from_dpo.id)["owner_source"]

    monkeypatch.delenv("PRIVACYCARE_DPO_EMAIL", raising=False)
    unassigned = _create(db, "restriction")
    assert unassigned.owner_source == "unassigned"
    assert unassigned.owner_email is None
    assert (
        unassigned.owner_source
        == _core_get_request(db, unassigned.id)["owner_source"]
    )


# --- I4 (final review). received_at is optional on DsrRequestCreate,
# defaults to now, and a future value is rejected — paper/email intake is
# the Kenyan reality, and a request recorded days after it arrived must not
# silently grant the controller extra time by starting the clock at
# data-entry time instead.


def test_a_past_received_at_is_honoured_by_the_route(db):
    received = datetime.now(timezone.utc) - timedelta(days=2)

    created = _create(db, "access", received_at=received)

    assert created.received_at == received
    assert created.deadline_at == received + timedelta(days=7)


def test_a_future_received_at_is_rejected_with_400(db):
    future = datetime.now(timezone.utc) + timedelta(hours=1)

    with pytest.raises(HTTPException) as caught:
        create_dsr_request(
            DsrRequestCreate(
                right="access", subject_identifier=_subject(), received_at=future
            ),
            db=db,
            client=_fake_client("carol@serianu.com"),
        )
    assert caught.value.status_code == 400
    assert "future" in caught.value.detail.lower()


# --- I5 (narrowed, per ruling). The response must answer "where is this
# obligation" on one screen: a nullable fides_privacy_request_status field,
# read live, and an explicit "vanished" report when the stored id no longer
# resolves rather than presenting a dead id as though it were live.


def test_the_response_reports_the_delegated_requests_live_status(db):
    created = _create(db, "access")
    assert created.fides_privacy_request_id is not None
    assert created.fides_privacy_request_status == "pending"


def test_a_non_delegating_right_reports_no_status(db):
    created = _create(db, "restriction")
    assert created.fides_privacy_request_id is None
    assert created.fides_privacy_request_status is None


def test_a_vanished_delegated_request_is_reported_explicitly(db):
    created = _create(db, "access")
    db.execute(
        sqlalchemy.text("DELETE FROM privacyrequest WHERE id = :id"),
        {"id": created.fides_privacy_request_id},
    )

    reread = get_dsr_request(created.id, db=db, client=_fake_client("carol@serianu.com"))

    assert reread.fides_privacy_request_id == created.fides_privacy_request_id
    assert reread.fides_privacy_request_status == "vanished"


# --- I6 (final review, guard only). subject_notified_at means "when the
# subject was told the outcome" — recording it before any decision exists
# is a false regulatory record, not merely a premature one.


def test_notifying_a_request_that_has_not_been_decided_is_409(db):
    created = _create(db, "restriction")

    with pytest.raises(HTTPException) as caught:
        record_dsr_notification(
            created.id, DsrNotificationRequest(), db=db, client=_fake_client("carol@serianu.com")
        )

    assert caught.value.status_code == 409
    assert "not been decided" in caught.value.detail.lower()
    reread = get_dsr_request(created.id, db=db, client=_fake_client("carol@serianu.com"))
    assert reread.subject_notified_at is None


# --- Minor finding (final review): no test drove the one branch left in
# create_dsr_request that can fail AFTER record_request has already
# written a row (delegate() raising). The reviewer notes this branch
# currently governs ALL delegating-right traffic in the live deployment and
# had zero coverage. Simulated with a D-DSR-7 drift (same mechanism as
# test_dsr_delegation.py's own drift test) rather than deleting any
# FK-referenced row, so nothing about this test depends on a schema detail
# elsewhere.


def test_record_succeeds_delegate_fails_the_register_insert_is_rolled_back(db):
    db.execute(
        sqlalchemy.text(
            'UPDATE privacycare_dsr_timeline SET days = 3 WHERE "right" = \'erasure\''
        )
    )
    subject = _subject()

    with pytest.raises(HTTPException) as caught:
        create_dsr_request(
            DsrRequestCreate(right="erasure", subject_identifier=subject),
            db=db,
            client=_fake_client("carol@serianu.com"),
        )

    assert caught.value.status_code == 400
    remaining = [r for r in _core_list_requests(db) if r["subject_identifier"] == subject]
    assert remaining == [], "the register insert must not survive a failed delegate()"


# --- Minor finding (final review): _days_left used math.ceil unconditionally,
# so a deadline breached by less than 24 hours reported 0 — read by the UI
# as "due today" rather than "already overdue". The alerting plan will
# threshold on this value, so a passed deadline must come back negative.


def test_days_left_is_negative_once_the_deadline_has_passed():
    just_passed = datetime.now(timezone.utc) - timedelta(hours=1)
    assert _days_left(just_passed) < 0


def test_days_left_is_still_due_today_or_positive_before_the_deadline():
    six_hours_out = datetime.now(timezone.utc) + timedelta(hours=6)
    assert _days_left(six_hours_out) >= 1


def test_days_left_is_none_when_unclocked():
    assert _days_left(None) is None
