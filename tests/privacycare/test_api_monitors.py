"""The six routes that make a discovery monitor configurable.

Nothing here executes a monitor or writes a StagedResource row — that is plan
11. These tests prove a monitor can be created, read back, edited and deleted,
and that a Viewer cannot configure one.
"""
import uuid

import pytest
import sqlalchemy
from fastapi import HTTPException
from fastapi_pagination import Params
from sqlalchemy.orm import Session

from fides.api.privacycare.api.monitor_schemas import EditableMonitorConfig
from fides.api.privacycare.api.monitors import (
    delete_monitor,
    get_monitor,
    get_monitor_databases,
    get_monitor_deletion_impact,
    list_monitors,
    put_monitor,
)
from tests.privacycare.test_api_assessments import _fake_client

DB_URL = "postgresql://postgres:fides@127.0.0.1:5442/fides"


@pytest.fixture
def db(monkeypatch):
    engine = sqlalchemy.create_engine(DB_URL)
    with Session(engine) as session:
        # DEVIATION FROM THE BRIEF, discovered by TDD: the brief's own text
        # for this fixture patches commit to a bare `lambda: None`. That
        # breaks the two routes below that use MonitorConfig.create()/
        # .update() (the ONLY ORM-CRUD-helper write path anywhere in
        # privacycare/ — every other route writes raw SQL, which is why this
        # never surfaced before). Base's persist_obj() runs
        # `db.add(resource); db.commit(); db.refresh(resource)`; refresh()
        # validates the instance is "persistent" (flushed) BEFORE it would
        # ever autoflush, so a true no-op commit makes refresh() raise
        # `InvalidRequestError: ... is not persistent within this Session`
        # for every brand-new row (reproduced directly against MonitorConfig
        # .create() outside the route, isolating this from route logic).
        # session.flush() sends the pending INSERT/UPDATE within the SAME
        # open, uncommitted transaction — persist_obj's refresh() then finds
        # the row and succeeds — while never issuing an actual COMMIT, so
        # `session.rollback()` below still discards every row exactly as the
        # brief intends. Verified directly: after a create+rollback under
        # this fixture, `select count(*) from monitorconfig` back through a
        # SEPARATE connection is 0.
        monkeypatch.setattr(session, "commit", session.flush)
        yield session
        session.rollback()


@pytest.fixture
def connection_key(db):
    """A ConnectionConfig to hang monitors off, rolled back with the session."""
    key = f"t10_conn_{uuid.uuid4().hex[:8]}"
    db.execute(
        sqlalchemy.text(
            "INSERT INTO connectionconfig (id, key, name, connection_type, "
            " access, disabled) "
            "VALUES (:id, :key, :name, 'postgres', 'write', false)"
        ),
        {"id": f"conn_{uuid.uuid4().hex[:12]}", "key": key, "name": key},
    )
    # DEVIATION FROM THE BRIEF, discovered by TDD:
    # test_the_databases_route_lists_what_the_connection_reports drives
    # get_monitor_databases, which opens a REAL connection through
    # connector.create_client() — it needs a `secrets` value that resolves
    # to a reachable database, and the brief's INSERT above never sets one.
    # `connectionconfig.secrets` is `MutableDict.as_mutable(encrypted_type(...))`
    # (models/connectionconfig.py) — the column's own comment says "Avoid
    # bulk/raw SQL updates to secrets; use ORM instance-level updates" — so
    # this sets it through the ORM object (encrypted correctly on flush),
    # not with a second raw INSERT/UPDATE. Pointed at the same fides-db this
    # whole test file already talks to via DB_URL, which is why "public"
    # is a real, correct answer for
    # test_the_databases_route_lists_what_the_connection_reports.
    from fides.api.models.connectionconfig import ConnectionConfig

    connection = db.query(ConnectionConfig).filter(ConnectionConfig.key == key).first()
    # Fix round 1, Finding 4: these are throwaway local-only Postgres
    # credentials for the docker-compose `fides-db` container this whole
    # test file already talks to via DB_URL above (see privacycare.ports.env)
    # — not a real credential, and not something that could leak anything
    # if read out of context.
    connection.secrets = {
        "host": "127.0.0.1",
        "port": 5442,
        "username": "postgres",
        "password": "fides",
        "dbname": "fides",
    }
    db.flush()
    return key


@pytest.fixture
def unreachable_connection_key(db):
    """A ConnectionConfig whose secrets point at nothing listening.

    Fix round 1, Finding 2: covers get_monitor_databases' 502 path, which
    nothing in the original 8 tests exercised. Port 1 on localhost fails
    with ECONNREFUSED in well under a millisecond (verified directly against
    connector.create_client() before wiring this into a test), so this adds
    no meaningful time to the suite and needs no mock/network stub.
    """
    key = f"t10_conn_unreachable_{uuid.uuid4().hex[:8]}"
    db.execute(
        sqlalchemy.text(
            "INSERT INTO connectionconfig (id, key, name, connection_type, "
            " access, disabled) "
            "VALUES (:id, :key, :name, 'postgres', 'write', false)"
        ),
        {"id": f"conn_{uuid.uuid4().hex[:12]}", "key": key, "name": key},
    )
    from fides.api.models.connectionconfig import ConnectionConfig

    connection = db.query(ConnectionConfig).filter(ConnectionConfig.key == key).first()
    connection.secrets = {
        "host": "127.0.0.1",
        "port": 1,
        "username": "postgres",
        "password": "fides",
        "dbname": "fides",
    }
    db.flush()
    return key


def _editable(connection_key, **kwargs):
    return EditableMonitorConfig(
        name=kwargs.pop("name", "Retail Postgres"),
        key=kwargs.pop("key", f"t10_mon_{uuid.uuid4().hex[:8]}"),
        connection_config_key=connection_key,
        **kwargs,
    )


def test_a_monitor_can_be_created_and_read_back(db, connection_key):
    request = _editable(connection_key, databases=["fides"])

    created = put_monitor(request, db=db, client=_fake_client("carol@example.com"))

    assert created.name == "Retail Postgres"
    assert created.connection_config_key == connection_key
    assert created.databases == ["fides"]

    fetched = get_monitor(created.key, db=db, client=_fake_client("carol@example.com"))
    assert fetched.key == created.key


def test_putting_the_same_key_edits_rather_than_duplicating(db, connection_key):
    # The UI's save button PUTs the whole config every time; a second PUT of an
    # existing key is an edit, not a second monitor.
    request = _editable(connection_key, name="First name")
    created = put_monitor(request, db=db, client=_fake_client("carol@example.com"))

    request.name = "Second name"
    edited = put_monitor(request, db=db, client=_fake_client("carol@example.com"))

    assert edited.key == created.key
    assert edited.name == "Second name"
    count = db.execute(
        sqlalchemy.text("SELECT count(*) FROM monitorconfig WHERE key = :k"),
        {"k": created.key},
    ).scalar()
    assert count == 1, "the monitor was duplicated rather than edited"


def test_the_list_returns_the_envelope_the_ui_paginates(db, connection_key):
    put_monitor(_editable(connection_key), db=db, client=_fake_client("carol@example.com"))

    page = list_monitors(
        params=Params(page=1, size=50), db=db, client=_fake_client("carol@example.com")
    )

    assert {"items", "total", "page", "size", "pages"} <= set(page.model_dump())


def test_an_unknown_monitor_is_404_not_500(db):
    with pytest.raises(HTTPException) as caught:
        get_monitor("no_such_monitor", db=db, client=_fake_client("carol@example.com"))
    assert caught.value.status_code == 404


def test_deletion_impact_reports_zero_while_nothing_has_scanned(db, connection_key):
    # D-DM-3: structurally zero today, but the UI calls this before it will
    # allow a delete, so a missing route blocks deletion entirely.
    created = put_monitor(
        _editable(connection_key), db=db, client=_fake_client("carol@example.com")
    )

    impact = get_monitor_deletion_impact(
        created.key, db=db, client=_fake_client("carol@example.com")
    )

    assert impact.staged_resource_count == 0
    assert impact.linked_datasets == []


def test_deleting_removes_the_monitor_and_reports_the_count(db, connection_key):
    created = put_monitor(
        _editable(connection_key), db=db, client=_fake_client("carol@example.com")
    )

    result = delete_monitor(created.key, db=db, client=_fake_client("carol@example.com"))

    assert result.count == 1
    remaining = db.execute(
        sqlalchemy.text("SELECT count(*) FROM monitorconfig WHERE key = :k"),
        {"k": created.key},
    ).scalar()
    assert remaining == 0


def test_the_databases_route_lists_what_the_connection_reports(db, connection_key):
    # D-DM-4: a live read against the target through SQLAlchemy's inspect(),
    # the same call sql_connector.py already makes. It lists database NAMES so a
    # consultant can choose include/exclude. It writes nothing and reads no row.
    created = put_monitor(
        _editable(connection_key), db=db, client=_fake_client("carol@example.com")
    )

    page = get_monitor_databases(
        created.key, params=Params(page=1, size=50), db=db,
        client=_fake_client("carol@example.com"),
    )

    # Postgres reports schemas here, not database names: `inspect().get_schema_names()`
    # is what a connection actually exposes, and `public` is the one every
    # Postgres carries. Fides' MonitorConfig.databases holds whatever the
    # connector calls a scope unit, which differs per datasource (a BigQuery
    # project, a Postgres schema) — so the test asserts what THIS connector
    # reports rather than a name borrowed from another datasource's vocabulary.
    assert "public" in page.items
    assert {"items", "total", "page", "size", "pages"} <= set(page.model_dump())


def test_the_databases_route_returns_502_when_the_connection_is_unreachable(
    db, unreachable_connection_key
):
    # Fix round 1, Finding 2: the third of the three error contracts the spec
    # names (missing connection -> 400, unknown monitor -> 404, unreachable
    # target -> 502) had no test. An unreachable/misconfigured target is the
    # TARGET's problem, not a PrivacyCare bug, so this must be a 502 naming
    # the connection — not a 500, and not a silent empty page.
    created = put_monitor(
        _editable(unreachable_connection_key), db=db, client=_fake_client("carol@example.com")
    )

    with pytest.raises(HTTPException) as caught:
        get_monitor_databases(
            created.key, params=Params(page=1, size=50), db=db,
            client=_fake_client("carol@example.com"),
        )

    assert caught.value.status_code == 502
    assert unreachable_connection_key in caught.value.detail


def test_a_monitor_naming_a_missing_connection_is_rejected(db):
    # connection_config_id is NOT NULL with a foreign key; without this check the
    # failure is an IntegrityError 500 rather than a message naming the key.
    with pytest.raises(HTTPException) as caught:
        put_monitor(
            _editable("no_such_connection"), db=db,
            client=_fake_client("carol@example.com"),
        )
    assert caught.value.status_code == 400
    assert "no_such_connection" in caught.value.detail
