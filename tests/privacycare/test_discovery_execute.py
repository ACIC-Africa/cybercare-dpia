"""Join the walk (Task 1) and the reconciler (Task 2) into one execution,
and the HTTP route that queues it — the route plan 10 deliberately left out.

SPEC D-EX-9 is the load-bearing rule: every run is recorded, including the
ones that die halfway. The failure-path test below is the one a happy-path
suite silently omits, and it is the one that proves the most dangerous
thing this module could get wrong -- calling `reconcile()` after a FAILED
walk, which would read an empty result as "the whole catalogue vanished"
and mark a customer's entire estate `removal`.
"""
import contextlib
import json
import uuid
from datetime import datetime
from typing import List, Optional

import pytest
import sqlalchemy
from fastapi import HTTPException
from sqlalchemy.orm import Session
from starlette import status
from starlette.testclient import TestClient

from fides.api.models.connectionconfig import ConnectionConfig
from fides.api.privacycare.discovery import execute as execute_module
from fides.api.privacycare.discovery.execute import (
    EmptyScopeError,
    execute_monitor_task,
    run_monitor,
)
from tests.privacycare.test_api_assessments import _fake_client

DB_URL = "postgresql://postgres:fides@127.0.0.1:5442/fides"
SCRATCH_KEY = "privacycare_scratch_local_postgres"


@pytest.fixture
def db(monkeypatch):
    engine = sqlalchemy.create_engine(DB_URL)
    with Session(engine) as session:
        # Same fixture pattern as test_discovery_walk.py / test_discovery_
        # reconcile.py / test_api_monitors.py: commit -> flush keeps rows
        # visible to later statements in this SAME transaction, while
        # rollback() at teardown discards the whole transaction, so nothing
        # written here ever reaches a separate connection.
        monkeypatch.setattr(session, "commit", session.flush)
        yield session
        session.rollback()


def _a_monitor_against_the_scratch_connection(
    db: Session, *, databases: Optional[List[str]] = None
) -> str:
    """A real MonitorConfig row pointed at the seeded scratch connection
    (plan 10, D-DM-5), with its secrets overridden to the HOST's view of
    fides-db -- the same override test_discovery_walk.py's own
    `scratch_connection` fixture applies, and for the same reason (see that
    fixture's "TWO NETWORK VIEWS" comment): the row on disk holds the
    CONTAINER's view (fides-db:5432), and host-side pytest is not on that
    Docker network. This mutates only the in-memory ORM instance inside
    this session, rolled back at teardown.

    `databases` defaults to unscoped (the column's own NOT NULL '{}'
    default) -- pass it to build a monitor scoped to specific schema names,
    e.g. for F4's "scope matched nothing" case.

    Returns the monitor's `key` -- what `monitor_config_id` means
    everywhere on this surface (api/monitors.py's own module docstring).
    """
    connection = ConnectionConfig.get_by(db, field="key", value=SCRATCH_KEY)
    if connection is None:
        pytest.skip(
            f"{SCRATCH_KEY} is not seeded; run "
            "scripts/privacycare/seed_connection.py --commit"
        )
    connection.secrets = {**connection.secrets, "host": "127.0.0.1", "port": 5442}
    db.flush()

    monitor_key = f"t11_exec_mon_{uuid.uuid4().hex[:8]}"
    db.execute(
        sqlalchemy.text(
            "INSERT INTO monitorconfig (id, name, key, connection_config_id, databases) "
            "VALUES (:id, :name, :key, :connection_config_id, "
            "CAST(:databases AS character varying[]))"
        ),
        {
            "id": f"mnt_{uuid.uuid4().hex[:12]}",
            "name": monitor_key,
            "key": monitor_key,
            "connection_config_id": connection.id,
            "databases": databases or [],
        },
    )
    db.flush()
    return monitor_key


def test_a_successful_run_writes_resources_and_closes_out_its_execution_record(db):
    # The happy path this plan exists to deliver: a run against a real
    # (scratch) target actually stages resources, and the run's own
    # MonitorExecution row shows it started and finished cleanly.
    monitor_key = _a_monitor_against_the_scratch_connection(db)

    summary = run_monitor(db, monitor_config_id=monitor_key)

    assert summary.added > 0, "the scratch connection's public schema is never empty"

    staged_count = db.execute(
        sqlalchemy.text(
            "SELECT count(*) FROM stagedresource WHERE monitor_config_id = :key"
        ),
        {"key": monitor_key},
    ).scalar()
    assert staged_count == summary.added

    row = db.execute(
        sqlalchemy.text(
            "SELECT status, started, completed, messages FROM monitorexecution "
            "WHERE monitor_config_key = :key"
        ),
        {"key": monitor_key},
    ).mappings().first()
    assert row is not None
    assert row["started"] is not None
    assert row["completed"] is not None
    assert row["status"] == "Completed"
    assert list(row["messages"]) == []


def test_a_failed_run_still_records_that_it_started_and_how_it_ended(db, monkeypatch):
    # D-EX-9. A scan that dies halfway must leave evidence that it ran and
    # stopped, not silence -- an operator asking "did discovery run last
    # night?" deserves an answer either way. And the resources it DID write
    # stay written: they are true observations, and the next run reconciles
    # them.
    monitor_key = _a_monitor_against_the_scratch_connection(db)

    def _explode(*args, **kwargs):
        raise RuntimeError("catalogue read failed")

    monkeypatch.setattr(
        "fides.api.privacycare.discovery.execute.walk_catalogue", _explode
    )

    with pytest.raises(RuntimeError):
        run_monitor(db, monitor_config_id=monitor_key)

    row = db.execute(
        sqlalchemy.text(
            "SELECT status, started, completed, messages FROM monitorexecution "
            "WHERE monitor_config_key = :key"
        ),
        {"key": monitor_key},
    ).mappings().first()
    assert row is not None, "a failed run left no execution record at all"
    assert row["started"] is not None
    assert row["completed"] is not None, "a failed run must still be closed out"
    assert any("catalogue read failed" in m for m in row["messages"])


def test_a_failed_walk_never_reaches_reconcile(db, monkeypatch):
    # The most dangerous path in this plan, proven directly rather than
    # inferred from the exception test above: reconcile() must not even be
    # CALLED when the walk fails, because reconcile() marks anything not in
    # `found` as `removal` -- an empty list from a failed walk would read as
    # "the whole catalogue disappeared".
    monitor_key = _a_monitor_against_the_scratch_connection(db)

    # Seed one already-staged resource for this monitor, the way an earlier,
    # successful run would have left behind.
    db.execute(
        sqlalchemy.text(
            """
            INSERT INTO stagedresource (
                id, urn, name, resource_type, monitor_config_id, diff_status,
                classifications, user_assigned_data_categories, children, meta
            ) VALUES (
                'sta_' || gen_random_uuid(), :urn, :urn, 'Table',
                :monitor_config_id, 'addition', '{}', '{}', '{}', '{}'::jsonb
            )
            """
        ),
        {"urn": f"{monitor_key}.db.public.orders", "monitor_config_id": monitor_key},
    )
    db.flush()

    reconcile_calls = []
    monkeypatch.setattr(
        execute_module,
        "reconcile",
        lambda *a, **k: reconcile_calls.append((a, k)),
    )

    def _explode(*args, **kwargs):
        raise RuntimeError("catalogue read failed")

    monkeypatch.setattr(execute_module, "walk_catalogue", _explode)

    with pytest.raises(RuntimeError):
        run_monitor(db, monitor_config_id=monitor_key)

    assert reconcile_calls == [], "reconcile() was called after a failed walk"

    still_staged = db.execute(
        sqlalchemy.text(
            "SELECT diff_status FROM stagedresource WHERE urn = :urn"
        ),
        {"urn": f"{monitor_key}.db.public.orders"},
    ).scalar()
    assert still_staged == "addition", (
        "the pre-existing resource must be untouched -- reconcile() never ran"
    )


def test_a_failed_reconcile_still_closes_the_execution_record(db, monkeypatch):
    # F2. execute.py's try/except previously wrapped ONLY walk_catalogue --
    # a unique violation from a concurrent run, a deadlock, or any other
    # DBAPI error inside reconcile() escaped with the row still
    # `In progress` and `completed` NULL forever, contradicting D-EX-9's own
    # promise (and this module's docstring, which claimed otherwise).
    monitor_key = _a_monitor_against_the_scratch_connection(db)

    def _explode(*args, **kwargs):
        raise RuntimeError("reconcile blew up")

    monkeypatch.setattr(execute_module, "reconcile", _explode)

    with pytest.raises(RuntimeError):
        run_monitor(db, monitor_config_id=monitor_key)

    row = db.execute(
        sqlalchemy.text(
            "SELECT status, started, completed, messages FROM monitorexecution "
            "WHERE monitor_config_key = :key"
        ),
        {"key": monitor_key},
    ).mappings().first()
    assert row is not None
    assert row["started"] is not None
    assert row["completed"] is not None, (
        "a failed reconcile() must still close the execution record"
    )
    assert row["status"] == "Errored"
    assert any("reconcile blew up" in m for m in row["messages"])

    # The session itself must still be usable afterwards -- a DBAPI-level
    # failure inside reconcile() would otherwise leave `db`'s transaction
    # aborted and this very query would fail with "current transaction is
    # aborted" if the SAVEPOINT around reconcile() were missing.
    still_alive = db.execute(sqlalchemy.text("SELECT 1")).scalar()
    assert still_alive == 1


def test_a_scope_that_matches_nothing_errors_out_instead_of_reconciling(db):
    # F4. A *successful* walk whose `databases` scope matches no schema
    # returns just the lone Database node -- reaching reconcile() there
    # would mark every one of this monitor's OTHER already-staged resources
    # `removal`, with the execution row reading `Completed` and `messages`
    # empty: a silent false "your whole estate vanished" finding for what
    # is far more likely a renamed schema or a stale/typo'd scope.
    monitor_key = _a_monitor_against_the_scratch_connection(
        db, databases=["t11_never_a_real_schema"]
    )

    # A resource an earlier, correctly-scoped run staged, which must survive
    # untouched.
    db.execute(
        sqlalchemy.text(
            """
            INSERT INTO stagedresource (
                id, urn, name, resource_type, monitor_config_id, diff_status,
                classifications, user_assigned_data_categories, children, meta
            ) VALUES (
                'sta_' || gen_random_uuid(), :urn, :urn, 'Table',
                :monitor_config_id, 'addition', '{}', '{}', '{}', '{}'::jsonb
            )
            """
        ),
        {"urn": f"{monitor_key}.db.public.orders", "monitor_config_id": monitor_key},
    )
    db.flush()

    with pytest.raises(EmptyScopeError, match="t11_never_a_real_schema"):
        run_monitor(db, monitor_config_id=monitor_key)

    row = db.execute(
        sqlalchemy.text(
            "SELECT status, completed, messages FROM monitorexecution "
            "WHERE monitor_config_key = :key"
        ),
        {"key": monitor_key},
    ).mappings().first()
    assert row is not None
    assert row["completed"] is not None
    assert row["status"] == "Errored"
    assert any("t11_never_a_real_schema" in m for m in row["messages"])

    still_staged = db.execute(
        sqlalchemy.text("SELECT diff_status FROM stagedresource WHERE urn = :urn"),
        {"urn": f"{monitor_key}.db.public.orders"},
    ).scalar()
    assert still_staged == "addition", (
        "reconcile() must never run when the scope matched nothing -- the "
        "pre-existing resource would otherwise be marked removal"
    )


def test_the_route_returns_4xx_for_an_unknown_monitor(db):
    from fides.api.privacycare.api.monitors import execute_monitor

    with pytest.raises(HTTPException) as caught:
        execute_monitor(
            "no_such_monitor_at_all", db=db, client=_fake_client("carol@example.com")
        )
    assert 400 <= caught.value.status_code < 500


def test_the_route_refuses_to_queue_a_second_scan_while_one_is_in_progress(db):
    # F3 (route half). A double-clicked Scan button, or a retry racing a
    # still-running execution, must not queue a second walk against the
    # same monitor while one is already `In progress` -- both would walk
    # the same catalogue from the same pre-state concurrently.
    from fides.api.privacycare.api.monitors import execute_monitor

    monitor_key = _a_monitor_against_the_scratch_connection(db)
    db.execute(
        sqlalchemy.text(
            "INSERT INTO monitorexecution (id, monitor_config_key, status, started, "
            "classification_instances, messages) VALUES "
            "(:id, :key, 'In progress', now(), '{}', '{}')"
        ),
        {"id": f"mxn_{uuid.uuid4().hex[:12]}", "key": monitor_key},
    )
    db.flush()

    with pytest.raises(HTTPException) as caught:
        execute_monitor(monitor_key, db=db, client=_fake_client("carol@example.com"))

    assert caught.value.status_code == status.HTTP_409_CONFLICT
    assert monitor_key in caught.value.detail


def test_the_route_requires_the_update_scope():
    # Same bar as test_api_http.py's test_put_discovery_monitor_rejects_a_
    # read_only_scope: a real ClientDetail row holding ONLY
    # privacycare_discovery:read, driven through the real ASGI app, must be
    # refused with 403 by this write route. Running a scan is a privileged
    # act, not a read (spec: scope PRIVACYCARE_DISCOVERY_UPDATE) -- this is
    # the one route plan 10 deliberately left out for exactly that reason.
    from fides.api.cryptography.schemas.jwt import (
        JWE_ISSUED_AT,
        JWE_PAYLOAD_CLIENT_ID,
        JWE_PAYLOAD_SCOPES,
    )
    from fides.api.models.client import ClientDetail
    from fides.api.oauth.jwt import generate_jwe
    from fides.api.privacycare.api.router import PRIVACYCARE_MONITORS_PREFIX
    from fides.api.privacycare.asgi import app
    from fides.common.scope_registry import PRIVACYCARE_DISCOVERY_READ
    from fides.config import CONFIG

    engine = sqlalchemy.create_engine(DB_URL)
    session = Session(engine)
    read_only_client = ClientDetail(
        hashed_secret="not-a-real-secret-t11-execute-test",
        salt="not-a-real-salt-t11-execute-test",
        scopes=[PRIVACYCARE_DISCOVERY_READ],
    )
    session.add(read_only_client)
    session.commit()
    client_id = read_only_client.id
    try:
        payload = {
            JWE_PAYLOAD_SCOPES: [PRIVACYCARE_DISCOVERY_READ],
            JWE_PAYLOAD_CLIENT_ID: client_id,
            JWE_ISSUED_AT: datetime.now().isoformat(),
        }
        jwe = generate_jwe(json.dumps(payload), CONFIG.security.app_encryption_key)
        with TestClient(app) as client:
            response = client.post(
                f"{PRIVACYCARE_MONITORS_PREFIX}/does-not-matter/execute",
                headers={"Authorization": f"Bearer {jwe}"},
            )
        assert response.status_code == status.HTTP_403_FORBIDDEN, (
            f"{PRIVACYCARE_MONITORS_PREFIX}/does-not-matter/execute did not "
            f"reject a read-only-scoped caller (got {response.status_code} "
            f"{response.text!r})"
        )
    finally:
        session.query(ClientDetail).filter(ClientDetail.id == client_id).delete()
        session.commit()
        session.close()
        engine.dispose()


def test_the_celery_task_is_a_thin_wrapper_calling_the_synchronous_core(
    db, monkeypatch
):
    # "The task itself needs no test beyond confirming it calls the core" --
    # the brief's own words. Patch DatabaseTask.get_new_session (the same
    # technique test_tasks.py's _run_wrapper uses for generate_assessments)
    # so `.run(...)` drives this test's own rolled-back session, and patch
    # run_monitor itself to observe exactly what the wrapper passed it.
    from fides.api.tasks import DatabaseTask

    calls = []
    monkeypatch.setattr(
        execute_module,
        "run_monitor",
        lambda session, *, monitor_config_id: calls.append(
            (session, monitor_config_id)
        ),
    )

    @contextlib.contextmanager
    def _session(_self):
        yield db

    original = DatabaseTask.get_new_session
    DatabaseTask.get_new_session = _session
    try:
        execute_monitor_task.run("some-monitor-key")
    finally:
        DatabaseTask.get_new_session = original

    assert calls == [(db, "some-monitor-key")]
