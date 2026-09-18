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


# --- Task 4, R2 (parked residual from plan 14's final review).
# _fides_privacy_request_status used to call PrivacyRequest.get_by per row,
# loading the whole entity — including the multi-megabyte columns
# (_filtered_final_upload, access_result_urls) Fides' own
# query_without_large_columns exists to keep out of list reads. Same shape
# as test_dsr_alert_job.py's test_the_statuses_are_read_in_one_query_not_one_
# per_row, which covers Task 3's fides_request_statuses; this covers the
# other call site task 3 named as sharing that helper (api/dsr.py's list
# route) rather than duplicating the query.


def test_the_list_reads_delegated_statuses_in_one_query_not_one_per_row(db):
    # The live register now permanently carries the demo seed's own DSR
    # requests (D-SEED-8, plan 20) — including "access" rows — so the page
    # this route returns is no longer just the 3 this test creates. Filter
    # down to this test's own ids (tracked at creation) rather than
    # asserting the raw page length, the same way every other test in this
    # file that cares about "only what I created" already scopes itself by
    # id (e.g. test_the_list_reports_vanished_for_a_deleted_delegated_
    # request, just below). The batching claim under test — one query, not
    # one per row — is unaffected either way: it is about how the whole
    # page's statuses are read, not about how many rows are on it.
    created_ids = {_create(db, "access").id for _ in range(3)}

    seen: list = []

    @sqlalchemy.event.listens_for(db.get_bind(), "before_cursor_execute")
    def _record(conn, cursor, statement, *args):  # noqa: ANN001
        if "privacyrequest" in statement.lower():
            seen.append(statement)

    try:
        page = list_dsr_requests(
            right="access", status=None, params=Params(page=1, size=50),
            db=db, client=_fake_client("carol@serianu.com"),
        )
    finally:
        sqlalchemy.event.remove(db.get_bind(), "before_cursor_execute", _record)

    ours = [item for item in page.items if item.id in created_ids]
    assert len(ours) == 3, "expected all 3 of this test's own access requests on the page"
    assert all(item.fides_privacy_request_status == "pending" for item in ours)
    assert len(seen) == 1, f"expected one batched read, got {len(seen)}"


# --- Fix round 2 on task 4. The R2 report claimed the "vanished" sentinel
# behaviour was preserved through the batched read, but the only test that
# actually drives "vanished" (test_a_vanished_delegated_request_is_reported_
# explicitly, above) goes through the single-row get_dsr_request path, not
# list_dsr_requests' batched fides_request_statuses call. That claim rested
# on inspection of the shared _fides_privacy_request_status function, not
# on a test exercising the list endpoint's own code path. This closes that
# gap directly rather than just noting it.


def test_the_list_reports_vanished_for_a_deleted_delegated_request(db):
    live = _create(db, "access")
    vanished = _create(db, "access")
    db.execute(
        sqlalchemy.text("DELETE FROM privacyrequest WHERE id = :id"),
        {"id": vanished.fides_privacy_request_id},
    )

    page = list_dsr_requests(
        right="access", status=None, params=Params(page=1, size=50),
        db=db, client=_fake_client("carol@serianu.com"),
    )

    by_id = {item.id: item.fides_privacy_request_status for item in page.items}
    assert by_id[live.id] == "pending"
    assert by_id[vanished.id] == "vanished"


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


# --- Task 4, R1 probe (parked residual from plan 14's final review). The
# create route's docstring claimed a register row can never survive a right
# that failed to delegate. The drift test above cannot exercise that claim:
# it raises ValueError from delegate() BEFORE delegate() ever reaches
# PrivacyRequest.create — the first ORM write. This test forces a failure
# AFTER that write instead, by breaking persist_identity, to find out
# whether the claim actually holds. Per controller ruling, this is a probe:
# read what it reports rather than assuming the answer.


def test_a_failure_after_the_fides_request_is_created(db, monkeypatch):
    monkeypatch.setattr(
        "fides.api.models.privacy_request.privacy_request.PrivacyRequest.persist_identity",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    subject = _subject()

    with pytest.raises(Exception):
        create_dsr_request(
            DsrRequestCreate(right="access", subject_identifier=subject),
            db=db,
            client=_fake_client("carol@serianu.com"),
        )

    orphaned = db.execute(
        sqlalchemy.text(
            "SELECT count(*) FROM privacycare_dsr_request WHERE subject_identifier = :s"
        ),
        {"s": subject},
    ).scalar()
    assert orphaned == 0, (
        "a register row survived a failed delegation — the create route's "
        "docstring claims this cannot happen"
    )


# --- Fix round 1 on task 4, Finding 1. The probe above proves the row is
# gone; it does NOT prove discard_request is what removed it. Under this
# fixture, commit is patched to flush for the whole session — including
# inside Fides' persist_obj — so nothing the route does is ever a REAL
# commit. db.rollback() runs before discard_request in the except block
# (defensively, to clear a possibly-aborted transaction first — see that
# block's own comment), and under THIS fixture that rollback alone already
# discards record_request's still-flushed insert before discard_request
# ever gets a row to delete. Proven empirically while writing this test:
# an earlier version of it asserted the row still existed at the moment of
# the discard_request call and that assertion itself failed — the row was
# already gone by then, via the preceding rollback, not via
# discard_request. That is expected and correct here (discard_request is
# documented idempotent on a missing id), but it means even this spy
# cannot show discard_request's own DELETE removing a still-present row
# under test conditions — only that the route wires it in. That mechanical
# guarantee (it deletes the row it's given and only that row) is covered
# separately and directly in test_dsr_register.py
# (test_discard_request_deletes_only_the_named_row), independent of this
# fixture and of db.rollback() entirely. What THIS test proves is the
# wiring: the route calls discard_request exactly once, with the id
# record_request minted for this request — the half the fixture would
# otherwise erase (delete discard_request's call from the route entirely
# and the earlier register-clean probe still passes; this test would not).


def test_a_failed_delegation_actually_calls_discard_request(db, monkeypatch):
    # Fix round 2, Finding 1: the earlier version of this test only
    # counted calls (asserted len(calls) == 1) without checking the
    # argument — it would have passed even if the route handed
    # discard_request a wrong or stale id. This version captures the id
    # record_request actually minted (by spying on record_request itself,
    # the only source of that id) and asserts discard_request was called
    # with THAT id, not merely once.
    subject = _subject()
    monkeypatch.setattr(
        "fides.api.models.privacy_request.privacy_request.PrivacyRequest.persist_identity",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    import fides.api.privacycare.api.dsr as dsr_module

    real_record_request = dsr_module.record_request
    minted_ids: list = []

    def _record_request_spy(*args, **kwargs):
        minted_id = real_record_request(*args, **kwargs)
        minted_ids.append(minted_id)
        return minted_id

    monkeypatch.setattr(dsr_module, "record_request", _record_request_spy)

    real_discard_request = dsr_module.discard_request
    calls: list = []

    def _discard_spy(db_arg, request_id_arg):
        calls.append(request_id_arg)
        return real_discard_request(db_arg, request_id_arg)

    monkeypatch.setattr(dsr_module, "discard_request", _discard_spy)

    with pytest.raises(Exception):
        create_dsr_request(
            DsrRequestCreate(right="access", subject_identifier=subject),
            db=db,
            client=_fake_client("carol@serianu.com"),
        )

    assert len(minted_ids) == 1, f"expected record_request called once, got {len(minted_ids)}"
    assert calls == minted_ids, (
        f"expected discard_request called once with the minted id "
        f"{minted_ids}, got {calls}"
    )


# --- Fix round 1 on task 4, Finding 3. If the compensation itself fails
# (discard_request or the commit after it), an unguarded except block would
# let that failure propagate IN PLACE of delegate()'s real error, silently
# changing what the client sees. This proves the client still sees the
# original RuntimeError even when discard_request also blows up.


def test_a_failed_compensation_does_not_mask_the_original_error(db, monkeypatch):
    monkeypatch.setattr(
        "fides.api.models.privacy_request.privacy_request.PrivacyRequest.persist_identity",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("original delegation failure")),
    )

    import fides.api.privacycare.api.dsr as dsr_module

    def _broken_discard(db_arg, request_id_arg):
        raise RuntimeError("compensation itself is broken")

    monkeypatch.setattr(dsr_module, "discard_request", _broken_discard)

    with pytest.raises(RuntimeError, match="original delegation failure"):
        create_dsr_request(
            DsrRequestCreate(right="access", subject_identifier=_subject()),
            db=db,
            client=_fake_client("carol@serianu.com"),
        )


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
