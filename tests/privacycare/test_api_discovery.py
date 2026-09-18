"""The discovery-findings HTTP surface (2026-09-18 discovery-findings-API
brief).

Fixture and staging helpers are copied from test_discovery_findings.py
rather than imported — that module already proves the core (list's one
query, the mapped/ignored validation shape, append-only history); this
file proves only the HTTP envelope: scope enforcement, the 404-vs-empty
distinction for an unknown urn, and that a rejection from
discovery.findings maps to the right status code. Same relationship
test_api_risk.py already has to test_risk_register.py.
"""
import uuid

import pytest
import sqlalchemy
from fastapi import HTTPException
from sqlalchemy.orm import Session

from fides.api.oauth.roles import CONTRIBUTOR, OWNER, ROLES_TO_SCOPES_MAPPING, VIEWER
from fides.api.privacycare.api.discovery import (
    get_finding_history,
    list_discovery_findings,
    reconcile_discovery_finding,
)
from fides.api.privacycare.api.discovery_schemas import ReconcileFindingRequest
from fides.common.scope_registry import (
    PRIVACYCARE_DISCOVERY_READ,
    PRIVACYCARE_DISCOVERY_UPDATE,
)

DB_URL = "postgresql://postgres:fides@127.0.0.1:5442/fides"


class _FakeClient:
    """Stands in for ClientDetail — _created_by_from_client only ever reads
    .user_id and .id (see api/identity.py), so nothing else is needed."""

    user_id = None
    id = "test-discovery-client"


@pytest.fixture
def db(monkeypatch):
    engine = sqlalchemy.create_engine(DB_URL)
    with Session(engine) as session:
        # Same fixture pattern as test_api_risk.py / test_api_screening.py:
        # commit -> flush keeps writes visible within this one transaction;
        # rollback() on teardown discards everything, including any
        # stagedresource/reconciliation row this file writes.
        monkeypatch.setattr(session, "commit", session.flush)
        yield session
        session.rollback()


def _stage_resource(db, *, urn, name, resource_type, parent, monitor_key):
    db.execute(
        sqlalchemy.text(
            """
            INSERT INTO stagedresource (
                id, urn, name, resource_type, parent, monitor_config_id,
                diff_status, classifications, user_assigned_data_categories,
                children, meta
            ) VALUES (
                'sta_' || gen_random_uuid(), :urn, :name, :resource_type,
                :parent, :monitor_key, 'addition', '{}', '{}', '{}', '{}'::jsonb
            )
            """
        ),
        {
            "urn": urn, "name": name, "resource_type": resource_type,
            "parent": parent, "monitor_key": monitor_key,
        },
    )


@pytest.fixture
def table_urn(db) -> str:
    monitor_key = f"test_monitor_{uuid.uuid4().hex[:8]}"
    database_urn = f"{monitor_key}.db"
    schema_urn = f"{database_urn}.public"
    table_urn = f"{schema_urn}.customers"
    _stage_resource(db, urn=database_urn, name="db", resource_type="Database", parent=None, monitor_key=monitor_key)
    _stage_resource(db, urn=schema_urn, name="public", resource_type="Schema", parent=database_urn, monitor_key=monitor_key)
    _stage_resource(db, urn=table_urn, name="customers", resource_type="Table", parent=schema_urn, monitor_key=monitor_key)
    _stage_resource(db, urn=f"{table_urn}.email", name="email", resource_type="Field", parent=table_urn, monitor_key=monitor_key)
    return table_urn


@pytest.fixture
def system_id(db) -> str:
    row = db.execute(sqlalchemy.text("SELECT id FROM ctl_systems LIMIT 1")).first()
    assert row is not None
    return row.id


@pytest.fixture
def system_fides_key(db, system_id) -> str:
    # Same row system_id names, by its OTHER identifier — see
    # discovery_schemas.py's ReconcileFindingRequest docstring: id and
    # fides_key differ for every real system, so this must be a real
    # lookup, never system_id reused as a stand-in.
    row = db.execute(
        sqlalchemy.text("SELECT fides_key FROM ctl_systems WHERE id = :id"),
        {"id": system_id},
    ).first()
    assert row is not None
    return row.fides_key


# --- List route --------------------------------------------------------


def test_list_returns_the_staged_table_as_needs_review(db, table_urn):
    resp = list_discovery_findings(db=db)

    urns = {f.urn for f in resp.findings}
    assert table_urn in urns
    entry = next(f for f in resp.findings if f.urn == table_urn)
    assert entry.state == "needs_review"
    assert entry.field_count == 1


def test_list_filters_by_state(db, table_urn, system_id):
    reconcile_discovery_finding(
        table_urn,
        ReconcileFindingRequest(state="mapped", system_id=system_id),
        db=db,
        client=_FakeClient(),
    )

    mapped = list_discovery_findings(state="mapped", db=db)
    needs_review = list_discovery_findings(state="needs_review", db=db)

    assert table_urn in {f.urn for f in mapped.findings}
    assert table_urn not in {f.urn for f in needs_review.findings}


def test_list_with_an_unknown_state_filter_is_400_not_a_silent_empty_list(db):
    with pytest.raises(HTTPException) as caught:
        list_discovery_findings(state="bogus", db=db)
    assert caught.value.status_code == 400


# --- History route -------------------------------------------------------


def test_history_is_empty_for_a_real_table_never_reconciled(db, table_urn):
    resp = get_finding_history(table_urn, db=db)
    assert resp.urn == table_urn
    assert resp.reconciliations == []


def test_history_on_an_unknown_urn_is_404_not_a_silent_empty_list(db):
    # THE SAME DISTINCTION api/screening.py's decision_history already
    # keeps: an unknown urn (never discovered at all) must not read the
    # same as a real table nobody has reconciled yet.
    with pytest.raises(HTTPException) as caught:
        get_finding_history(str(uuid.uuid4()), db=db)
    assert caught.value.status_code == 404
    assert "no such finding" in caught.value.detail


def test_history_reflects_a_reconciliation_just_recorded(db, table_urn):
    reconcile_discovery_finding(
        table_urn,
        ReconcileFindingRequest(state="ignored", reason="internal-only table"),
        db=db,
        client=_FakeClient(),
    )

    resp = get_finding_history(table_urn, db=db)

    assert len(resp.reconciliations) == 1
    assert resp.reconciliations[0].state == "ignored"
    assert resp.reconciliations[0].reason == "internal-only table"
    assert resp.reconciliations[0].decided_by == "client:test-discovery-client"


# --- Reconcile route -----------------------------------------------------


def test_reconcile_mapped_round_trips_through_list_and_history(db, table_urn, system_id):
    result = reconcile_discovery_finding(
        table_urn,
        ReconcileFindingRequest(state="mapped", system_id=system_id),
        db=db,
        client=_FakeClient(),
    )

    assert result.urn == table_urn
    assert result.state == "mapped"
    assert result.system_id == system_id
    assert result.system_name is not None

    entry = next(f for f in list_discovery_findings(db=db).findings if f.urn == table_urn)
    assert entry.state == "mapped"
    assert entry.system_id == system_id

    history = get_finding_history(table_urn, db=db)
    assert len(history.reconciliations) == 1


def test_reconcile_on_an_unknown_urn_is_404_not_400(db):
    # findings.reconcile_finding raises the SAME ValueError type for "no
    # such finding" and for a bad mapped/ignored shape — this route must
    # not collapse the two into the same status code: the path segment
    # names a resource (the table) that does not exist, which is 404.
    with pytest.raises(HTTPException) as caught:
        reconcile_discovery_finding(
            "no-such-urn",
            ReconcileFindingRequest(state="ignored", reason="whatever"),
            db=db,
            client=_FakeClient(),
        )
    assert caught.value.status_code == 404
    assert "no such finding" in caught.value.detail


def test_reconcile_mapped_without_a_system_id_is_400(db, table_urn):
    with pytest.raises(HTTPException) as caught:
        reconcile_discovery_finding(
            table_urn,
            ReconcileFindingRequest(state="mapped"),
            db=db,
            client=_FakeClient(),
        )
    assert caught.value.status_code == 400


def test_reconcile_ignored_without_a_reason_is_400(db, table_urn):
    with pytest.raises(HTTPException) as caught:
        reconcile_discovery_finding(
            table_urn,
            ReconcileFindingRequest(state="ignored"),
            db=db,
            client=_FakeClient(),
        )
    assert caught.value.status_code == 400


def test_a_failed_reconcile_writes_nothing(db, table_urn):
    with pytest.raises(HTTPException):
        reconcile_discovery_finding(
            table_urn,
            ReconcileFindingRequest(state="mapped"),
            db=db,
            client=_FakeClient(),
        )

    # Not get_finding_history(...): the route's own db.rollback() on this
    # ValueError ends the CURRENT transaction, discarding this fixture's
    # own flushed (never committed) stagedresource rows too — the same
    # thing a real request's rollback does to its own uncommitted work.
    # Querying the reconciliation table directly, scoped to this urn,
    # proves "nothing written" without depending on the now-gone parent
    # row still resolving.
    remaining = db.execute(
        sqlalchemy.text(
            "SELECT count(*) FROM privacycare_discovery_reconciliation "
            "WHERE stagedresource_urn = :urn"
        ),
        {"urn": table_urn},
    ).scalar()
    assert remaining == 0


def test_reconcile_mapped_with_a_fides_key_resolves_to_the_internal_id(
    db, table_urn, system_id, system_fides_key
):
    # The 2026-09-18 fix: fides_key is the only identifier any read route
    # (SystemSelect, GET /system) ever puts on the wire for a system, so
    # this is what the shipped picker actually sends. It must resolve to
    # the SAME internal id a caller naming system_id directly would have
    # stored — same column, same downstream read (list/history), so a
    # fides_key-based reconciliation and an id-based one are
    # indistinguishable once recorded.
    result = reconcile_discovery_finding(
        table_urn,
        ReconcileFindingRequest(state="mapped", system_fides_key=system_fides_key),
        db=db,
        client=_FakeClient(),
    )

    assert result.system_id == system_id
    assert result.system_name is not None

    entry = next(f for f in list_discovery_findings(db=db).findings if f.urn == table_urn)
    assert entry.system_id == system_id


def test_reconcile_mapped_with_an_unknown_fides_key_is_rejected_by_name(db, table_urn):
    # A silent drop here would record a reconciliation nobody actually
    # made — same discipline screening/mapping.py's save_mapping already
    # keeps for an unknown data category.
    with pytest.raises(HTTPException) as caught:
        reconcile_discovery_finding(
            table_urn,
            ReconcileFindingRequest(state="mapped", system_fides_key="no-such-system"),
            db=db,
            client=_FakeClient(),
        )
    assert caught.value.status_code == 400
    assert "no-such-system" in caught.value.detail


def test_reconcile_rejects_both_system_id_and_system_fides_key(
    db, table_urn, system_id, system_fides_key
):
    # Ambiguous input is refused outright, not silently resolved by
    # preferring one — see _resolve_system_id's own docstring.
    with pytest.raises(HTTPException) as caught:
        reconcile_discovery_finding(
            table_urn,
            ReconcileFindingRequest(
                state="mapped", system_id=system_id, system_fides_key=system_fides_key
            ),
            db=db,
            client=_FakeClient(),
        )
    assert caught.value.status_code == 400


def test_reconciling_twice_appends_a_second_history_row(db, table_urn, system_id):
    reconcile_discovery_finding(
        table_urn,
        ReconcileFindingRequest(state="ignored", reason="first look"),
        db=db,
        client=_FakeClient(),
    )
    reconcile_discovery_finding(
        table_urn,
        ReconcileFindingRequest(state="mapped", system_id=system_id),
        db=db,
        client=_FakeClient(),
    )

    history = get_finding_history(table_urn, db=db)
    assert len(history.reconciliations) == 2


# --- Scopes ---------------------------------------------------------------


def test_viewer_can_read_but_not_reconcile_while_owner_and_contributor_do_both():
    assert PRIVACYCARE_DISCOVERY_READ in ROLES_TO_SCOPES_MAPPING[VIEWER]
    assert PRIVACYCARE_DISCOVERY_UPDATE not in ROLES_TO_SCOPES_MAPPING[VIEWER]

    assert PRIVACYCARE_DISCOVERY_READ in ROLES_TO_SCOPES_MAPPING[OWNER]
    assert PRIVACYCARE_DISCOVERY_UPDATE in ROLES_TO_SCOPES_MAPPING[OWNER]

    assert PRIVACYCARE_DISCOVERY_READ in ROLES_TO_SCOPES_MAPPING[CONTRIBUTOR]
    assert PRIVACYCARE_DISCOVERY_UPDATE in ROLES_TO_SCOPES_MAPPING[CONTRIBUTOR]


def test_every_discovery_findings_route_requires_its_declared_scope():
    from fides.api.oauth.utils import verify_oauth_client
    from fides.api.privacycare.api.router import PRIVACYCARE_DISCOVERY_FINDINGS_PREFIX
    from fides.api.privacycare.asgi import app

    expected_scope_by_method = {
        "GET": PRIVACYCARE_DISCOVERY_READ,
        "POST": PRIVACYCARE_DISCOVERY_UPDATE,
    }

    routes = [
        r for r in app.routes
        if getattr(r, "path", "").startswith(PRIVACYCARE_DISCOVERY_FINDINGS_PREFIX)
    ]
    assert routes, "no discovery-findings routes found — registration itself is broken"

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

    # 3 logical routes (list, history, reconcile), one HTTP method apiece,
    # times 2 for the trailing-slash/no-trailing-slash route-object pair
    # fides.api.util.api_router.APIRouter registers for every path (same
    # guard as test_every_risk_route_requires_its_declared_scope) = 6.
    assert checked == 6, f"expected 6 discovery-findings route/method pairs, checked {checked}"
