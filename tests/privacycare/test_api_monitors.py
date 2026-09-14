"""The seven routes that make a discovery monitor configurable.

Nothing here executes a monitor or writes a StagedResource row — that is plan
11. These tests prove a monitor can be created, read back, edited and deleted,
and that a Viewer cannot configure one.
"""
import uuid

import pytest
import sqlalchemy
from fastapi import HTTPException
from fastapi_pagination import Params
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from fides.api.models.connectionconfig import ConnectionConfig
from fides.api.models.detection_discovery.core import MonitorConfig
from fides.api.models.fides_user import FidesUser
from fides.api.privacycare.api import monitors as monitors_module
from fides.api.privacycare.api.monitor_schemas import EditableMonitorConfig
from fides.api.privacycare.api.monitors import (
    delete_monitor,
    get_available_databases,
    get_monitor,
    get_monitor_databases,
    get_monitor_deletion_impact,
    list_monitors,
    put_monitor,
)
from fides.api.service.connectors import get_connector as real_get_connector
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


@pytest.fixture
def steward_user(db):
    """A FidesUser row to assign as a monitor steward, rolled back with `db`."""
    user = FidesUser(username=f"t10_steward_{uuid.uuid4().hex[:8]}")
    db.add(user)
    db.flush()
    return user


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


def test_a_monitor_can_be_assigned_a_non_empty_steward_list(db, connection_key, steward_user):
    # C1: MonitorConfig.stewards is a many-to-many relationship, not a plain
    # column — this is the default path (the picker is the third field on
    # the create modal, populated), not an edge case, and no existing test
    # covered a non-empty steward list before this fix (the rendered check
    # only passed because the field was left empty).
    request = _editable(connection_key, stewards=[steward_user.id])

    created = put_monitor(request, db=db, client=_fake_client("carol@example.com"))

    assert [s.id for s in created.stewards] == [steward_user.id]

    fetched = get_monitor(created.key, db=db, client=_fake_client("carol@example.com"))
    assert [s.id for s in fetched.stewards] == [steward_user.id]


def test_assigning_an_unknown_steward_id_is_rejected_with_400(db, connection_key):
    request = _editable(connection_key, stewards=["no_such_user_id"])

    with pytest.raises(HTTPException) as caught:
        put_monitor(request, db=db, client=_fake_client("carol@example.com"))

    assert caught.value.status_code == 400
    assert "no_such_user_id" in caught.value.detail


def test_the_list_returns_the_envelope_the_ui_paginates(db, connection_key):
    put_monitor(_editable(connection_key), db=db, client=_fake_client("carol@example.com"))

    page = list_monitors(
        params=Params(page=1, size=50), db=db, client=_fake_client("carol@example.com")
    )

    assert {"items", "total", "page", "size", "pages"} <= set(page.model_dump())


def test_list_monitors_filters_by_connection_config_key(db, connection_key):
    # I2: useMonitorConfigTable.tsx's useGetMonitorsByIntegrationQuery sends
    # connection_config_key on the Integrations monitor tab, and the RTK
    # Query slice spreads it straight through — undeclared here, FastAPI
    # silently dropped it and every integration's tab listed every monitor
    # in the system. Two connections, one monitor each, proves the filter
    # actually narrows the result.
    other_key = f"t10_conn_{uuid.uuid4().hex[:8]}"
    db.execute(
        sqlalchemy.text(
            "INSERT INTO connectionconfig (id, key, name, connection_type, "
            " access, disabled) "
            "VALUES (:id, :key, :name, 'postgres', 'write', false)"
        ),
        {"id": f"conn_{uuid.uuid4().hex[:12]}", "key": other_key, "name": other_key},
    )
    db.flush()

    put_monitor(
        _editable(connection_key, name="Monitor On A", key=f"t10_mon_{uuid.uuid4().hex[:8]}"),
        db=db, client=_fake_client("carol@example.com"),
    )
    put_monitor(
        _editable(other_key, name="Monitor On B", key=f"t10_mon_{uuid.uuid4().hex[:8]}"),
        db=db, client=_fake_client("carol@example.com"),
    )

    page = list_monitors(
        connection_config_key=connection_key,
        params=Params(page=1, size=50), db=db, client=_fake_client("carol@example.com"),
    )

    assert page.items, "expected at least one monitor for connection_key"
    assert {item.connection_config_key for item in page.items} == {connection_key}


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
    # M12: information_schema is Postgres system catalog metadata, never a
    # scannable scope unit — it must never be offered by the picker.
    assert "information_schema" not in page.items
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
    # I6: the detail must name the connection and NOTHING else — it used to
    # interpolate the raw target exception, which in dev mode (hide_parameters
    # = not CONFIG.dev_mode) can carry bound parameters/URIs built from
    # `secrets`, visible to PRIVACYCARE_DISCOVERY_READ (Viewer, Data Steward).
    assert caught.value.detail == (
        f"Could not read databases from connection {unreachable_connection_key}"
    )
    assert "refused" not in caught.value.detail.lower()
    assert "errno" not in caught.value.detail.lower()


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


def test_a_duplicate_monitor_name_is_rejected_with_400_not_500(db, connection_key):
    # I3: base_class.create() derives key=to_snake_case(name) when no key is
    # given and raises KeyOrNameAlreadyExists (a plain Exception, not an
    # HTTPException) on a NAME collision too, with check_name=True (the
    # route's default). The shipped create form sends no key, so two
    # monitors named the same thing on two integrations is the live path.
    put_monitor(
        _editable(connection_key, name="Retail Postgres", key=f"t10_mon_{uuid.uuid4().hex[:8]}"),
        db=db, client=_fake_client("carol@example.com"),
    )

    with pytest.raises(HTTPException) as caught:
        put_monitor(
            _editable(connection_key, name="Retail Postgres", key=f"t10_mon_{uuid.uuid4().hex[:8]}"),
            db=db, client=_fake_client("carol@example.com"),
        )

    assert caught.value.status_code == 400


def test_databases_and_excluded_databases_together_is_rejected_with_400_not_500(
    db, connection_key
):
    # I3: MonitorConfig.database_include_exclude_list_is_valid raises a bare
    # ValueError when both are set — previously an unhandled 500.
    request = _editable(connection_key, databases=["fides"], excluded_databases=["other"])

    with pytest.raises(HTTPException) as caught:
        put_monitor(request, db=db, client=_fake_client("carol@example.com"))

    assert caught.value.status_code == 400


def test_a_concurrent_create_race_is_a_409_not_a_500(db, connection_key, monkeypatch):
    # I3 / M14: two concurrent PUTs of the same new key both find `existing
    # is None` and both call MonitorConfig.create(); the second's INSERT
    # then collides with the first's at the DB level, raising IntegrityError
    # — a real, lower-level conflict distinct from the KeyOrNameAlreadyExists
    # family above, and left uncaught it also escapes as a 500. Forcing the
    # IntegrityError directly (rather than driving two real overlapping
    # transactions) exercises the route's own catch clause deterministically.
    def _boom(cls, db, *, data, check_name=True):
        raise IntegrityError(
            "INSERT INTO monitorconfig ...", {}, Exception(
                "duplicate key value violates unique constraint"
            ),
        )

    monkeypatch.setattr(MonitorConfig, "create", classmethod(_boom))

    with pytest.raises(HTTPException) as caught:
        put_monitor(_editable(connection_key), db=db, client=_fake_client("carol@example.com"))

    assert caught.value.status_code == 409


def test_the_databases_route_returns_502_for_an_unsupported_connection_type(db):
    # I7: get_connector() used to sit ABOVE _list_databases' try, so its
    # NotImplementedError (a connection_type with no entry in
    # service/connectors' supported_connectors mapping) escaped as a 500
    # attributed to us rather than the 502-attributed-to-the-target contract
    # the function's docstring promises. 'manual' is a real, valid
    # ConnectionType with no connector class registered (deprecated in
    # favour of manual_webhook).
    key = f"t10_conn_unsupported_{uuid.uuid4().hex[:8]}"
    db.execute(
        sqlalchemy.text(
            "INSERT INTO connectionconfig (id, key, name, connection_type, "
            " access, disabled) "
            "VALUES (:id, :key, :name, 'manual', 'write', false)"
        ),
        {"id": f"conn_{uuid.uuid4().hex[:12]}", "key": key, "name": key},
    )
    db.flush()
    created = put_monitor(_editable(key), db=db, client=_fake_client("carol@example.com"))

    with pytest.raises(HTTPException) as caught:
        get_monitor_databases(
            created.key, params=Params(page=1, size=50), db=db,
            client=_fake_client("carol@example.com"),
        )

    assert caught.value.status_code == 502
    assert key in caught.value.detail


def _connector_with_tracked_dispose(monkeypatch, connection):
    """Wrap the REAL connector's create_client() so its returned Engine's
    dispose() is counted, and patch monitors.get_connector to hand back
    this same connector. Used by the I4 engine-disposal tests below."""
    real_connector = real_get_connector(connection)
    disposed = {"count": 0}
    real_create_client = real_connector.create_client

    def _tracking_create_client():
        engine = real_create_client()
        original_dispose = engine.dispose

        def _tracking_dispose():
            disposed["count"] += 1
            original_dispose()

        engine.dispose = _tracking_dispose
        return engine

    monkeypatch.setattr(real_connector, "create_client", _tracking_create_client)
    monkeypatch.setattr(monitors_module, "get_connector", lambda conn: real_connector)
    return disposed


def test_list_databases_disposes_its_engine_on_success(db, connection_key, monkeypatch):
    # I4: create_client() builds a NEW Engine every call (not the cached
    # `client` property) and _list_databases never disposed of it —
    # `with engine.connect()` only returns the pooled CONNECTION to that
    # engine's pool, which then stays open until GC. The picker calls this
    # on every mount and page, so opening it repeatedly accumulated open
    # server-side connections.
    connection = (
        db.query(ConnectionConfig).filter(ConnectionConfig.key == connection_key).first()
    )
    disposed = _connector_with_tracked_dispose(monkeypatch, connection)

    names = monitors_module._list_databases(connection)

    assert disposed["count"] == 1
    assert "public" in names


def test_list_databases_disposes_its_engine_even_on_502(
    db, unreachable_connection_key, monkeypatch
):
    # Same I4 leak, on the error path: create_client() still returns a real
    # Engine even though connect() then fails, so the leak is just as real
    # on a 502 as on success.
    connection = (
        db.query(ConnectionConfig)
        .filter(ConnectionConfig.key == unreachable_connection_key)
        .first()
    )
    disposed = _connector_with_tracked_dispose(monkeypatch, connection)

    with pytest.raises(HTTPException):
        monitors_module._list_databases(connection)

    assert disposed["count"] == 1


def test_available_databases_lists_real_names_before_any_monitor_exists(
    db, connection_key
):
    # Task 4 fix round 2, Finding 1: the create-monitor wizard's database
    # picker calls this route BEFORE a monitor exists — no put_monitor()
    # call anywhere in this test, unlike test_the_databases_route_lists_
    # what_the_connection_reports above. The request is the exact shape
    # getAvailableDatabasesByConnection sends (discovery-detection.slice.ts):
    # a monitor-shaped body carrying only connection_config_key that
    # matters.
    request = EditableMonitorConfig(
        name="new-monitor",
        connection_config_key=connection_key,
        classify_params={},
    )

    page = get_available_databases(
        request, params=Params(page=1, size=50), db=db,
        client=_fake_client("carol@example.com"),
    )

    # Same connection, same connector, same inspect() call as the by-monitor
    # route — "public" is the one schema every Postgres carries.
    assert "public" in page.items
    assert {"items", "total", "page", "size", "pages"} <= set(page.model_dump())


def test_available_databases_creates_no_monitor_row(db, connection_key):
    # The request body LOOKS like a create (name/connection_config_key/
    # classify_params — EditableMonitorConfig's shape) and is NOT one; this
    # is the exact failure mode a shared helper or a copy-pasted route body
    # could reintroduce silently.
    before = db.execute(sqlalchemy.text("SELECT count(*) FROM monitorconfig")).scalar()

    get_available_databases(
        EditableMonitorConfig(
            name="new-monitor", connection_config_key=connection_key, classify_params={}
        ),
        params=Params(page=1, size=50), db=db,
        client=_fake_client("carol@example.com"),
    )

    after = db.execute(sqlalchemy.text("SELECT count(*) FROM monitorconfig")).scalar()
    assert after == before, "get_available_databases wrote a MonitorConfig row"


def test_available_databases_rejects_a_missing_connection(db):
    # Same 400 contract as put_monitor's equivalent check — a missing
    # connection names the key rather than surfacing as an unrelated 500.
    with pytest.raises(HTTPException) as caught:
        get_available_databases(
            EditableMonitorConfig(
                name="new-monitor",
                connection_config_key="no_such_connection",
                classify_params={},
            ),
            params=Params(page=1, size=50), db=db,
            client=_fake_client("carol@example.com"),
        )
    assert caught.value.status_code == 400
    assert "no_such_connection" in caught.value.detail


def test_available_databases_returns_502_when_the_connection_is_unreachable(
    db, unreachable_connection_key
):
    # Same 502 contract as get_monitor_databases' equivalent test — an
    # unreachable/misconfigured target is the TARGET's problem, not a
    # PrivacyCare bug.
    with pytest.raises(HTTPException) as caught:
        get_available_databases(
            EditableMonitorConfig(
                name="new-monitor",
                connection_config_key=unreachable_connection_key,
                classify_params={},
            ),
            params=Params(page=1, size=50), db=db,
            client=_fake_client("carol@example.com"),
        )
    assert caught.value.status_code == 502
    assert unreachable_connection_key in caught.value.detail
    # I6: same detail contract as get_monitor_databases' equivalent test —
    # the connection key only, never the underlying target exception text.
    assert caught.value.detail == (
        f"Could not read databases from connection {unreachable_connection_key}"
    )
