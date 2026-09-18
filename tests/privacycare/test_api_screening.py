"""The screening gate's HTTP surface (plan 18, Task 4; re-keyed to the
business process, plus the list route, in plan 20, Task 3).

Follows api/risk.py (plan 17) and its test file's own shape: routes are
called directly as plain functions against a rolled-back session (no
TestClient), and scope enforcement is proved two ways — the role-mapping
assertion below, and a walk of app.routes (mirroring
test_every_risk_route_requires_its_declared_scope /
test_every_dsr_route_requires_its_declared_scope) confirming every route
actually declares the scope it is supposed to.

UPDATE (Task 5, spec 2026-09-17, plan 18): the authorized `--commit` seed
has now run against this same live database and Carol's six trigger rows
stay there permanently (same as the Kenya template). The `triggers`
fixture below is INSERT ... ON CONFLICT (trigger_key) DO NOTHING, so it is
a no-op against those six real rows rather than raising UniqueViolation;
every test using it below cares only about trigger_key identity (whether a
given key is valid to screen against), never about the fixture's own
throwaway label/description text, with the one exception noted at
test_triggers_come_back_in_display_order itself.

UPDATE (plan 20, Task 3): screening is now keyed to a business process
(privacycare_business_process — 86 of the customer's own, real ones), not
a processing activity (privacydeclaration — 2 of those, both invented).
Every fixture and test below was re-keyed from declaration_id to
business_process_id — see api/screening.py's own module docstring for the
full rationale. This file also gained the list route
(GET /api/v1/privacycare/screening) the screen cannot work without: every
business process with its status, in one call.

THE DISTINCTION THIS FILE EXISTS TO PROVE: an unknown business_process_id
is a 404 on every id-keyed read AND the write route; a business process
that exists but has never been screened is a 200 with no verdict — never
collapsed into either the 404 case or a fabricated "not applicable" — and
that holds on the list route too: an unscreened process is listed WITH a
None verdict, not omitted and not 404'd.
"""
import uuid

import pytest
import sqlalchemy
from fastapi import HTTPException
from sqlalchemy.orm import Session

from fides.api.models.fides_user import FidesUser
from fides.api.oauth.roles import CONTRIBUTOR, OWNER, ROLES_TO_SCOPES_MAPPING, VIEWER
from fides.api.privacycare.api.screening import (
    _CLIENT_DECIDED_BY_LABEL,
    _UNRESOLVED_DECIDED_BY_LABEL,
    get_current_screening_verdict,
    get_data_mapping,
    get_screening_history,
    list_screening_status,
    list_screening_triggers,
    record_screening_decision,
    save_data_mapping,
)
from fides.api.privacycare.api.screening_schemas import (
    DataMappingRequest,
    ScreeningDecisionRequest,
)
from fides.common.scope_registry import (
    PRIVACYCARE_SCREENING_CREATE,
    PRIVACYCARE_SCREENING_READ,
)
from tests.privacycare.test_api_assessments import _fake_client
from tests.privacycare.test_context import _seed_declaration, _seed_system

DB_URL = "postgresql://postgres:fides@127.0.0.1:5442/fides"

# The six trigger keys, in the order Task 1's seed script assigns them
# display_order — same list test_screening_gate.py uses; this file only
# needs stable keys to screen against, not Carol's exact label text.
TRIGGER_KEYS = (
    "large_scale",
    "special_category",
    "systematic_monitoring",
    "new_technology",
    "automated_decision",
    "vulnerable_subjects",
)


@pytest.fixture
def db(monkeypatch):
    engine = sqlalchemy.create_engine(DB_URL)
    with Session(engine) as session:
        # NEVER a no-op: base_class.persist_obj does add/commit/refresh, and
        # a no-op commit starves refresh(). flush() gives write visibility
        # within the transaction without making it durable; rollback() on
        # teardown discards everything this test wrote.
        monkeypatch.setattr(session, "commit", session.flush)
        yield session
        session.rollback()


@pytest.fixture
def triggers(db):
    """Ensures the six trigger rows this test file needs exist, inside the
    same rolled-back session every test uses. ON CONFLICT (trigger_key) DO
    NOTHING: Task 5's real seed means these six keys already exist in the
    live database, so this is a no-op there and a real insert only against
    a from-empty database (e.g. CI)."""
    for order, key in enumerate(TRIGGER_KEYS, start=1):
        db.execute(
            sqlalchemy.text(
                "INSERT INTO privacycare_screening_trigger "
                "(id, trigger_key, label, description, display_order) "
                "VALUES (:id, :key, :label, :description, :order) "
                "ON CONFLICT (trigger_key) DO NOTHING"
            ),
            {
                "id": str(uuid.uuid4()),
                "key": key,
                "label": key.replace("_", " ").title(),
                "description": f"Test description for {key}.",
                "order": order,
            },
        )
    return TRIGGER_KEYS


def _seed_business_process(db, name: str, business_cycle: str) -> str:
    """Seeds one of the customer's own business processes (her register,
    not a generic fixture name) — screening now reads/writes against
    privacycare_business_process, not privacydeclaration. Same helper,
    same reasoning, as test_screening_gate.py's own
    _seed_business_process."""
    process_id = f"bp_{uuid.uuid4().hex[:12]}"
    db.execute(
        sqlalchemy.text(
            "INSERT INTO privacycare_business_process (id, name, business_cycle) "
            "VALUES (:id, :name, :cycle)"
        ),
        {"id": process_id, "name": name, "cycle": business_cycle},
    )
    return process_id


def _link_declaration(db, business_process_id: str, declaration_id: str) -> None:
    """Seeds one privacycare_process_declaration row linking a business
    process to a processing activity — this is the join the list route's
    has_mapping reads through. declaration_id need not resolve to a real
    privacydeclaration row: this helper is also how these tests create an
    ORPHAN link (mirrors the one real orphan row in this customer's live
    data, whose privacy_declaration_id no longer exists) to prove the list
    route tolerates it rather than crashing or misreporting it as mapped."""
    db.execute(
        sqlalchemy.text(
            "INSERT INTO privacycare_process_declaration "
            "(id, business_process_id, privacy_declaration_id) "
            "VALUES (:id, :process_id, :declaration_id)"
        ),
        {
            "id": str(uuid.uuid4()),
            "process_id": business_process_id,
            "declaration_id": declaration_id,
        },
    )


@pytest.fixture
def business_process_id(db) -> str:
    return _seed_business_process(db, "Fuel Card Issuance", "Card Operations")


def _record(db, business_process_id, **overrides):
    body = dict(triggered_keys=[], justification="Internal only, no external sharing.")
    body.update(overrides)
    return record_screening_decision(
        business_process_id,
        ScreeningDecisionRequest(**body),
        db=db,
        client=_fake_client("carol@example.com"),
    )


def _find(processes, business_process_id: str):
    for process in processes:
        if process.business_process_id == business_process_id:
            return process
    raise AssertionError(f"{business_process_id!r} missing from the list response")


# --- Triggers route ------------------------------------------------------


def test_triggers_come_back_in_display_order(db, triggers):
    # UPDATE (Task 5): this test's own name is "...in display order", and
    # that's the contract this route actually needs to prove — exact
    # label/description text is test_seed_screening_triggers.py's job (per
    # this file's own docstring, "not Carol's exact label text"). Before
    # Task 5's real seed, the `triggers` fixture owned every row in the
    # table outright, so asserting its own synthetic label/description
    # text back was an easy way to prove passthrough; now that Carol's six
    # real rows already exist, the fixture's INSERT is a no-op (ON
    # CONFLICT DO NOTHING) and what comes back is her real text, not the
    # fixture's placeholder — checked here only for non-emptiness, not a
    # hardcoded pattern this file was never meant to own.
    response = list_screening_triggers(db=db)

    assert [t.trigger_key for t in response.triggers] == list(TRIGGER_KEYS)
    for trigger in response.triggers:
        assert trigger.label
        assert trigger.description


def test_triggers_route_is_a_200_empty_envelope_when_nothing_is_seeded(db):
    # UPDATE (Task 5): the live table is no longer empty by default (Carol's
    # six rows are permanent, same as the Kenya template), so proving a
    # genuinely empty table renders as [] rather than an exception now means
    # clearing it inside this test's own rolled-back transaction — the
    # DELETE never escapes past this test's session.rollback() teardown.
    db.execute(sqlalchemy.text("DELETE FROM privacycare_screening_trigger"))
    response = list_screening_triggers(db=db)
    assert response.triggers == []


# --- List route ------------------------------------------------------------


def test_the_list_route_returns_a_never_screened_process_with_no_verdict(db, triggers):
    process_id = _seed_business_process(db, "Fuel Card Issuance", "Card Operations")

    response = list_screening_status(db=db)

    row = _find(response.processes, process_id)
    assert row.name == "Fuel Card Issuance"
    assert row.business_cycle == "Card Operations"
    assert row.dpia_required is None
    assert row.decided_by is None
    assert row.decided_at is None
    assert row.has_mapping is False


def test_recording_a_decision_changes_what_the_list_route_returns(db, triggers):
    process_id = _seed_business_process(db, "Fraud Risk Assessments", "Audit & Risk")

    before = _find(list_screening_status(db=db).processes, process_id)
    assert before.dpia_required is None
    assert before.decided_by is None

    _record(db, process_id, triggered_keys=["large_scale"], justification=None)

    after = _find(list_screening_status(db=db).processes, process_id)
    assert after.dpia_required is True
    assert after.decided_by == "carol@example.com"
    assert after.decided_at is not None


def test_the_list_route_sorts_by_business_cycle_then_name(db):
    # Two processes with distinct, sortable business-cycle names, seeded
    # inside this test's own rolled-back transaction so ordering is judged
    # only between them (relative position via .index()), never against an
    # assumed total count or position in a list that also carries this
    # customer's 86+ live rows.
    suffix = uuid.uuid4().hex[:6]
    cycle_a = f"AAA cycle {suffix}"
    cycle_z = f"ZZZ cycle {suffix}"
    first = _seed_business_process(db, "Fraud Risk Assessments", cycle_a)
    second = _seed_business_process(db, "CSR Planning & Execution", cycle_z)

    ids = [p.business_process_id for p in list_screening_status(db=db).processes]

    assert ids.index(first) < ids.index(second)


def test_the_list_route_reports_a_mapping_only_when_the_link_resolves(db):
    process_id = _seed_business_process(db, "CSR Planning & Execution", "CSR")
    system_id = _seed_system(db, f"sys_{uuid.uuid4().hex[:8]}")
    declaration_id = _seed_declaration(db, system_id, "marketing")
    _link_declaration(db, process_id, declaration_id)

    row = _find(list_screening_status(db=db).processes, process_id)
    assert row.has_mapping is True


def test_an_orphan_mapping_link_is_not_reported_as_mapped_and_does_not_crash(db):
    # Mirrors this customer's own live data: one privacycare_process_
    # declaration row whose privacy_declaration_id no longer resolves to
    # any privacydeclaration row (a declaration deleted after the link was
    # made). The list route must not treat that as "mapped", and must not
    # raise trying to find out.
    process_id = _seed_business_process(db, "CSR Planning & Execution", "CSR")
    _link_declaration(db, process_id, "decl_deleted_last_year")

    row = _find(list_screening_status(db=db).processes, process_id)
    assert row.has_mapping is False


# --- Current verdict route -------------------------------------------------


def test_an_unscreened_process_is_a_200_with_no_verdict_not_a_404(db, business_process_id):
    response = get_current_screening_verdict(business_process_id, db=db)

    assert response.business_process_id == business_process_id
    assert response.verdict is None


def test_an_unknown_business_process_id_is_404_on_current_verdict(db):
    with pytest.raises(HTTPException) as caught:
        get_current_screening_verdict(str(uuid.uuid4()), db=db)
    assert caught.value.status_code == 404


def test_recording_a_decision_changes_the_current_verdict(db, triggers, business_process_id):
    before = get_current_screening_verdict(business_process_id, db=db)
    assert before.verdict is None

    _record(db, business_process_id, triggered_keys=["large_scale"], justification=None)

    after = get_current_screening_verdict(business_process_id, db=db)
    assert after.verdict is not None
    assert after.verdict.dpia_required is True
    assert after.verdict.triggered_keys == ["large_scale"]
    assert after.verdict.business_process_id == business_process_id
    assert after.verdict.decided_by == "carol@example.com"


# --- History route --------------------------------------------------------


def test_history_is_empty_for_a_never_screened_process(db, business_process_id):
    response = get_screening_history(business_process_id, db=db)
    assert response.business_process_id == business_process_id
    assert response.decisions == []


def test_an_unknown_business_process_id_is_404_on_history(db):
    with pytest.raises(HTTPException) as caught:
        get_screening_history(str(uuid.uuid4()), db=db)
    assert caught.value.status_code == 404


def _backdate(db, business_process_id: str, triggered_keys: list, *, hours: int) -> None:
    """Pushes one decision's decided_at into the past by hand — same
    reasoning as test_screening_gate.py's own _backdate: this test's
    session never commits, so Postgres' now() (decided_at's server_default)
    is the whole TRANSACTION's start time, and both decisions recorded here
    would otherwise share one identical decided_at, leaving "newest first"
    resolved by the id tie-break rather than actually observable."""
    db.execute(
        sqlalchemy.text(
            "UPDATE privacycare_screening_decision "
            "SET decided_at = decided_at - (:hours || ' hours')::interval "
            "WHERE business_process_id = :process_id AND triggered_keys = :keys"
        ),
        {"hours": hours, "process_id": business_process_id, "keys": triggered_keys},
    )


def test_history_returns_both_decisions_after_rescreening(db, triggers, business_process_id):
    _record(db, business_process_id, triggered_keys=[], justification="Nothing ticked this quarter.")
    _backdate(db, business_process_id, [], hours=1)
    _record(db, business_process_id, triggered_keys=["special_category"], justification=None)

    response = get_screening_history(business_process_id, db=db)

    assert response.business_process_id == business_process_id
    assert len(response.decisions) == 2
    assert {d.dpia_required for d in response.decisions} == {True, False}
    # Newest first (gate.decision_history's own ordering).
    assert response.decisions[0].triggered_keys == ["special_category"]
    assert response.decisions[1].triggered_keys == []


# --- decided_by display resolution ------------------------------------------
#
# decided_by (the stored identifier) is never touched by any of this — every
# assertion below checks it is UNCHANGED alongside checking the new,
# resolved decided_by_display.


def _seed_fidesuser(db, *, first_name=None, last_name=None, username=None) -> str:
    """A real fidesuser row (id auto-generated with the 'fid_' table-prefix
    scheme, same as test_api_monitors.py's own steward_user fixture) so a
    test can record a decision as an ACTUAL, resolvable user — every other
    helper in this file (`_record`, `_fake_client`) uses a fake string user
    id that was never a real fidesuser row to begin with."""
    user = FidesUser(
        first_name=first_name,
        last_name=last_name,
        username=username or f"t_screening_{uuid.uuid4().hex[:8]}",
    )
    db.add(user)
    db.flush()
    return user.id


def _record_as(db, business_process_id, client, **overrides):
    """Same shape as `_record` above, but with the caller's own `client`
    instead of always `_fake_client("carol@example.com")` — needed to record
    a decision as a real fidesuser id, a "client:..." identifier, or an id
    that resolves to nothing at all."""
    body = dict(triggered_keys=[], justification="Internal only, no external sharing.")
    body.update(overrides)
    return record_screening_decision(
        business_process_id,
        ScreeningDecisionRequest(**body),
        db=db,
        client=client,
    )


def test_the_list_route_resolves_decided_by_to_a_real_users_name(db, triggers):
    process_id = _seed_business_process(db, "Fuel Card Issuance", "Card Operations")
    user_id = _seed_fidesuser(db, first_name="Carol", last_name="Mwangi")
    _record_as(db, process_id, _fake_client(user_id, id="client_irrelevant"))

    row = _find(list_screening_status(db=db).processes, process_id)
    assert row.decided_by == user_id, "the stored audit identifier must be unchanged"
    assert row.decided_by_display == "Carol Mwangi"


def test_the_list_route_falls_back_to_username_when_name_fields_are_blank(db, triggers):
    process_id = _seed_business_process(db, "Fuel Card Issuance", "Card Operations")
    username = f"carol.mwangi.{uuid.uuid4().hex[:6]}"
    user_id = _seed_fidesuser(db, username=username)
    _record_as(db, process_id, _fake_client(user_id, id="client_irrelevant"))

    row = _find(list_screening_status(db=db).processes, process_id)
    assert row.decided_by_display == username


def test_the_list_route_labels_a_client_decider_in_plain_language_not_the_raw_token(db, triggers):
    # user_id=None mirrors identity.py's own two legitimate no-named-user
    # paths (root/admin login, a bare M2M client) — _created_by_from_client
    # falls back to f"client:{client.id}" for exactly this case.
    process_id = _seed_business_process(db, "Fuel Card Issuance", "Card Operations")
    _record_as(db, process_id, _fake_client(None, id="client_m2m_abc123"))

    row = _find(list_screening_status(db=db).processes, process_id)
    assert row.decided_by == "client:client_m2m_abc123", (
        "the stored audit identifier must be unchanged"
    )
    assert row.decided_by_display == _CLIENT_DECIDED_BY_LABEL
    assert "client_m2m_abc123" not in row.decided_by_display, (
        "the raw token must never appear in the display string"
    )


def test_the_list_route_labels_an_unresolvable_decided_by_honestly_never_blank(db, triggers):
    # No fidesuser row answers to this id — the same shape a deleted user's
    # old decision carries (the id was real once, and resolves to nothing
    # now). A read-time join cannot tell "never existed" apart from
    # "existed and was deleted" — both simply fail to resolve — so both get
    # the same honest, non-blank answer, never a fabricated name and never
    # an empty cell.
    process_id = _seed_business_process(db, "Fuel Card Issuance", "Card Operations")
    fake_user_id = f"fid_{uuid.uuid4()}"
    _record_as(db, process_id, _fake_client(fake_user_id, id="client_irrelevant"))

    row = _find(list_screening_status(db=db).processes, process_id)
    assert row.decided_by == fake_user_id, "the stored audit identifier must be unchanged"
    assert row.decided_by_display == _UNRESOLVED_DECIDED_BY_LABEL
    assert fake_user_id not in row.decided_by_display, (
        "the raw identifier must never appear in the display string"
    )


def test_current_verdict_and_history_routes_also_carry_the_resolved_name(
    db, triggers, business_process_id
):
    user_id = _seed_fidesuser(db, first_name="Carol", last_name="Mwangi")
    _record_as(db, business_process_id, _fake_client(user_id, id="client_irrelevant"))

    current = get_current_screening_verdict(business_process_id, db=db)
    assert current.verdict.decided_by == user_id
    assert current.verdict.decided_by_display == "Carol Mwangi"

    history = get_screening_history(business_process_id, db=db)
    assert len(history.decisions) == 1
    assert history.decisions[0].decided_by == user_id
    assert history.decisions[0].decided_by_display == "Carol Mwangi"


def test_record_decision_route_response_already_carries_the_resolved_name(
    db, triggers, business_process_id
):
    user_id = _seed_fidesuser(db, first_name="Josephine", last_name="Wanjiru")
    verdict = _record_as(
        db, business_process_id, _fake_client(user_id, id="client_irrelevant"),
        triggered_keys=["large_scale"], justification=None,
    )
    assert verdict.decided_by == user_id
    assert verdict.decided_by_display == "Josephine Wanjiru"


def test_history_resolves_multiple_distinct_deciders_with_one_companion_query(
    db, triggers, business_process_id
):
    carol_id = _seed_fidesuser(db, first_name="Carol", last_name="Mwangi")
    josephine_id = _seed_fidesuser(db, first_name="Josephine", last_name="Wanjiru")

    _record_as(
        db, business_process_id, _fake_client(carol_id, id="c1"),
        triggered_keys=[], justification="Nothing ticked this quarter.",
    )
    _backdate(db, business_process_id, [], hours=1)
    _record_as(
        db, business_process_id, _fake_client(josephine_id, id="c2"),
        triggered_keys=["special_category"], justification=None,
    )

    # Asserted on the emitted SQL via before_cursor_execute — same idiom
    # test_api_assessments.py's own test_empty_body_update_locks_the_
    # assessment_row uses — a direct check on what was sent to Postgres,
    # not on this module's own source.
    statements: list[str] = []
    engine = db.get_bind()

    def _capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    sqlalchemy.event.listen(engine, "before_cursor_execute", _capture)
    try:
        history = get_screening_history(business_process_id, db=db)
    finally:
        sqlalchemy.event.remove(engine, "before_cursor_execute", _capture)

    assert history.decisions[0].decided_by_display == "Josephine Wanjiru"
    assert history.decisions[1].decided_by_display == "Carol Mwangi"

    fidesuser_lookups = [s for s in statements if "FROM fidesuser" in s]
    assert len(fidesuser_lookups) == 1, (
        "the history route must resolve every decider in ONE companion "
        f"query, never one per decision — emitted: {fidesuser_lookups!r}"
    )


def test_the_list_route_issues_exactly_one_sql_statement_for_any_number_of_rows(
    db, triggers
):
    # The whole point of this route (its own module-level comment on
    # _LIST_SCREENING_STATUS_SQL): every business process, WITH its
    # decider's resolved name, in ONE round trip — never a per-row fetch,
    # and never a separate fidesuser lookup bolted on afterwards.
    process_ids = [
        _seed_business_process(db, f"Screening Perf Process {i}", "Card Operations")
        for i in range(5)
    ]
    user_id = _seed_fidesuser(db, first_name="Carol", last_name="Mwangi")
    for process_id in process_ids[:3]:
        _record_as(db, process_id, _fake_client(user_id, id="client_irrelevant"))

    statements: list[str] = []
    engine = db.get_bind()

    def _capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    sqlalchemy.event.listen(engine, "before_cursor_execute", _capture)
    try:
        response = list_screening_status(db=db)
    finally:
        sqlalchemy.event.remove(engine, "before_cursor_execute", _capture)

    for process_id in process_ids:
        row = _find(response.processes, process_id)
        assert row.decided_by_display in (None, "Carol Mwangi")

    assert len(statements) == 1, (
        "the list route must issue exactly one SQL statement regardless of "
        f"row count — emitted {len(statements)}: {statements!r}"
    )
    assert "FROM fidesuser" not in statements[0] or "LEFT JOIN fidesuser" in statements[0]


# --- Record decision route -------------------------------------------------


def test_record_returns_the_created_verdict(db, triggers, business_process_id):
    verdict = _record(
        db, business_process_id, triggered_keys=["new_technology", "large_scale"],
        justification=None,
    )

    assert verdict.business_process_id == business_process_id
    assert verdict.dpia_required is True
    assert verdict.triggered_keys == ["large_scale", "new_technology"]
    assert verdict.justification is None
    assert verdict.decided_by == "carol@example.com"
    assert verdict.decided_at is not None


def test_a_screen_out_without_a_justification_is_400(db, triggers, business_process_id):
    with pytest.raises(HTTPException) as caught:
        _record(db, business_process_id, triggered_keys=[], justification=None)
    assert caught.value.status_code == 400


def test_a_screen_out_with_a_blank_justification_is_400(db, triggers, business_process_id):
    with pytest.raises(HTTPException) as caught:
        _record(db, business_process_id, triggered_keys=[], justification="   ")
    assert caught.value.status_code == 400


def test_an_unknown_trigger_key_is_400(db, triggers, business_process_id):
    with pytest.raises(HTTPException) as caught:
        _record(db, business_process_id, triggered_keys=["not_a_real_trigger"], justification=None)
    assert caught.value.status_code == 400


def test_recording_against_an_unknown_business_process_is_404(db, triggers):
    with pytest.raises(HTTPException) as caught:
        _record(db, str(uuid.uuid4()), triggered_keys=["large_scale"], justification=None)
    assert caught.value.status_code == 404


def test_a_caller_cannot_pass_dpia_required(db, triggers, business_process_id):
    # ScreeningDecisionRequest carries no dpia_required field at all —
    # record_decision (gate.py) derives it from triggered_keys and does not
    # accept it as an argument. Pydantic's default extra="ignore" means a
    # stray dpia_required in the payload is silently dropped rather than
    # rejected outright, but the important guarantee is behavioural: a
    # caller cannot use it to override the derivation. Ticking a trigger
    # while also passing dpia_required=False must still come back required.
    request = ScreeningDecisionRequest(
        triggered_keys=["large_scale"], justification=None, dpia_required=False,
    )
    assert not hasattr(request, "dpia_required")

    verdict = record_screening_decision(
        business_process_id, request, db=db, client=_fake_client("carol@example.com"),
    )
    assert verdict.dpia_required is True


def test_a_failed_record_writes_nothing(db, triggers, business_process_id):
    # Final review fix: the route's ValueError path (screening.py) calls a
    # REAL db.rollback() — this file's `db` fixture only patches `commit`,
    # never `rollback` (unlike test_tasks.py's savepoint-based fixture,
    # which this file deliberately does not use — see test_api_risk.py's
    # sibling rollback-only fixture for the same shape). A real rollback()
    # here discards the WHOLE transaction, including this test's own
    # `business_process_id`/`triggers` fixture rows, on top of anything
    # record_decision itself wrote. That makes the follow-up count query
    # return 0 unconditionally — before this fix, moving record_decision's
    # INSERT ahead of its own validation checks left this test green.
    #
    # Stubbed locally with a plain attribute assignment, not the shared
    # `monkeypatch` fixture: `monkeypatch` here would be the SAME instance
    # the `db` fixture already used to patch `commit`, and both patches
    # would then be undone together, at monkeypatch's own teardown — which
    # runs AFTER the `db` fixture's finalizer (session.rollback(), fixture
    # teardown is LIFO and `db` was set up before `monkeypatch` could be
    # requested here). That would leave the fixture's own cleanup rollback
    # stubbed to flush too, and this test's writes would survive into the
    # real dev database. Manual save/restore in `finally` sidesteps that
    # entirely: the real method is back in place before this function
    # returns, so the `db` fixture's own teardown rolls back for real.
    real_rollback = db.rollback
    db.rollback = db.flush
    try:
        with pytest.raises(HTTPException):
            _record(db, business_process_id, triggered_keys=["not_a_real_trigger"], justification=None)

        remaining = db.execute(
            sqlalchemy.text(
                "SELECT count(*) FROM privacycare_screening_decision "
                "WHERE business_process_id = :id"
            ),
            {"id": business_process_id},
        ).scalar()
        assert remaining == 0
    finally:
        db.rollback = real_rollback


# --- Mapping read route (fix wave, item I-2) -------------------------------


def test_an_unmapped_process_is_a_200_with_no_mapping_not_a_404(db, business_process_id):
    response = get_data_mapping(business_process_id, db=db)
    assert response.business_process_id == business_process_id
    assert response.mapping is None


def test_an_unknown_business_process_id_is_404_on_get_mapping(db):
    with pytest.raises(HTTPException) as caught:
        get_data_mapping(str(uuid.uuid4()), db=db)
    assert caught.value.status_code == 404


def test_a_saved_mapping_is_readable_back(db, business_process_id):
    saved = save_data_mapping(
        business_process_id,
        DataMappingRequest(name="Fuel card KYC verification", data_categories=["user.contact.email"]),
        db=db,
        client=_fake_client("carol@example.com"),
    )

    response = get_data_mapping(business_process_id, db=db)
    assert response.mapping is not None
    assert response.mapping.privacy_declaration_id == saved.privacy_declaration_id
    assert response.mapping.name == "Fuel card KYC verification"
    assert response.mapping.data_categories == ["user.contact.email"]


def test_a_process_with_only_a_route_unowned_activity_reads_as_no_mapping(db, business_process_id):
    # A pre-existing real activity with no marker (mirrors bp_94d5439ced86's
    # own two real, route-unowned activities) is a real and different state
    # from "never mapped" — both must read as mapping=None, not conflated
    # with a 404, and not fabricated into a mapping this route never wrote.
    system_id = _seed_system(db, f"sys_{uuid.uuid4().hex[:8]}")
    declaration_id = _seed_declaration(db, system_id, "marketing")
    _link_declaration(db, business_process_id, declaration_id)

    response = get_data_mapping(business_process_id, db=db)
    assert response.mapping is None


# --- Route ordering ------------------------------------------------------


def test_the_triggers_route_is_matched_before_the_business_process_id_route():
    # GET /triggers would otherwise be swallowed by GET /{business_process_id}
    # (business_process_id="triggers"), and the triggers route would 404
    # forever against a route that demonstrably exists — same hazard, same
    # fix shape, as test_api_tasks.py's test_the_tasks_route_is_matched_
    # before_the_assessment_id_route. A comment at the decorators documents
    # the ordering; this test is the guard that actually catches a
    # regression (screening.py's route functions are called directly
    # everywhere else in this file, which proves each route WORKS but not
    # that FastAPI would ever reach it through the real dispatch path).
    #
    # route.path on this router carries the full PRIVACYCARE_SCREENING_PREFIX
    # prefix (Fides' APIRouter subclass applies it at add_api_route time,
    # same as PRIVACYCARE_PREFIX's own routes), so the paths compared here
    # must match that.
    from fides.api.privacycare.api.router import (
        PRIVACYCARE_SCREENING_PREFIX,
        privacycare_screening_router,
    )

    paths = [route.path for route in privacycare_screening_router.routes]
    assert paths.index(f"{PRIVACYCARE_SCREENING_PREFIX}/triggers") < paths.index(
        f"{PRIVACYCARE_SCREENING_PREFIX}/{{business_process_id}}"
    )


# --- Scopes -------------------------------------------------------------


def test_viewer_can_read_but_not_write_while_owner_and_contributor_do_both():
    # A screening decision names a business process (which triggers were
    # ticked, a free-text justification for a screen-out), never a data
    # subject — same reasoning PRIVACYCARE_RISK_READ's grant to Viewer
    # records (roles.py) — so Viewer gets the read scope outright; the
    # write scope does not.
    assert PRIVACYCARE_SCREENING_READ in ROLES_TO_SCOPES_MAPPING[VIEWER]
    assert PRIVACYCARE_SCREENING_CREATE not in ROLES_TO_SCOPES_MAPPING[VIEWER]

    assert PRIVACYCARE_SCREENING_READ in ROLES_TO_SCOPES_MAPPING[OWNER]
    assert PRIVACYCARE_SCREENING_CREATE in ROLES_TO_SCOPES_MAPPING[OWNER]

    assert PRIVACYCARE_SCREENING_READ in ROLES_TO_SCOPES_MAPPING[CONTRIBUTOR]
    assert PRIVACYCARE_SCREENING_CREATE in ROLES_TO_SCOPES_MAPPING[CONTRIBUTOR]


def test_every_screening_route_requires_its_declared_scope():
    from fides.api.oauth.utils import verify_oauth_client
    from fides.api.privacycare.api.router import PRIVACYCARE_SCREENING_PREFIX
    from fides.api.privacycare.asgi import app

    expected_scope_by_method = {
        "GET": PRIVACYCARE_SCREENING_READ,
        "POST": PRIVACYCARE_SCREENING_CREATE,
    }

    routes = [
        r for r in app.routes
        if getattr(r, "path", "").startswith(PRIVACYCARE_SCREENING_PREFIX)
    ]
    assert routes, "no screening routes found — registration itself is broken"

    checked = 0
    for route in routes:
        methods = getattr(route, "methods", None) or set()
        for method in methods - {"HEAD", "OPTIONS"}:
            expected_scope = expected_scope_by_method[method]
            deps = getattr(route, "dependencies", [])
            oauth_deps = [
                d for d in deps if getattr(d, "dependency", None) is verify_oauth_client
            ]
            assert oauth_deps, (
                f"{route.path} [{method}] has no verify_oauth_client "
                "dependency — unauthenticated"
            )
            assert any(
                expected_scope in getattr(d, "scopes", []) for d in oauth_deps
            ), f"{route.path} [{method}] does not require {expected_scope!r}"
            checked += 1

    # 7 logical routes as of the final fix wave, item I-2 (list every
    # process's status, triggers, current verdict, history, record
    # decision, save a data mapping, read back a data mapping), one HTTP
    # method apiece — but fides.api.util.api_router.APIRouter registers
    # BOTH a trailing-slash and a no-trailing-slash variant of every path as
    # separate route objects (same guard as test_every_risk_route_requires_
    # its_declared_scope / test_every_dsr_route_requires_its_declared_scope),
    # so app.routes holds two entries per logical route: 7 * 2 = 14.
    assert checked == 14, f"expected 14 screening route/method pairs, checked {checked}"


def test_the_demo_seed_marker_reads_as_demonstration_data_not_a_malfunction():
    """The seed has to put its marker in decided_by — the table's check
    constraint forbids a justification on an applicable decision, so it is the
    only free-text column all 40 seeded rows can carry one in, and --remove
    deletes by it.

    Fabricating a human decider instead would put a false name on the field
    that answers "who decided this, and is accountable" — the compliance
    artifact itself. So the identifier stays honest, and only the label is fit
    for showing: "System (automated) — not a named user" reads as a broken
    system to someone being shown the product; "Demonstration data" says what
    the row actually is.
    """
    from fides.api.privacycare.api.screening import (
        _CLIENT_DECIDED_BY_LABEL,
        _decided_by_display,
    )

    assert _decided_by_display("client:privacycare:demo_seed") == "Demonstration data"
    # A genuine automated decision is still labelled as one - the demo marker
    # must not swallow every client identifier.
    assert _decided_by_display("client:root-user") == _CLIENT_DECIDED_BY_LABEL
    # And a real person is still named.
    assert (
        _decided_by_display("fid_x", first_name="Carol", last_name="Mwangi")
        == "Carol Mwangi"
    )
