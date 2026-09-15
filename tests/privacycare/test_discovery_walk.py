"""The catalogue walk: what is in this datastore, and nothing about what is IN it.

Spec D-EX-1 — the scanner reads STRUCTURE and never a row of customer data. A
discovery scan runs against production systems belonging to a customer with a
statutory duty to protect what is in them, and reading personal data while
cataloguing it is the least defensible thing this could do. The test that
asserts no SELECT is issued is the most important test in this plan.
"""
import uuid

import pytest
import sqlalchemy
from sqlalchemy.orm import Session

from fides.api.privacycare.discovery.walk import FoundResource, walk_catalogue

DB_URL = "postgresql://postgres:fides@127.0.0.1:5442/fides"
SCRATCH_KEY = "privacycare_scratch_local_postgres"


@pytest.fixture
def db(monkeypatch):
    engine = sqlalchemy.create_engine(DB_URL)
    with Session(engine) as session:
        monkeypatch.setattr(session, "commit", session.flush)
        yield session
        session.rollback()


@pytest.fixture
def scratch_connection(db):
    """The seeded local connection (plan 10, D-DM-5). Our own database, access='read'."""
    from fides.api.models.connectionconfig import ConnectionConfig

    connection = ConnectionConfig.get_by(db, field="key", value=SCRATCH_KEY)
    if connection is None:
        pytest.skip(f"{SCRATCH_KEY} is not seeded; run scripts/privacycare/seed_connection.py --commit")

    # TWO NETWORK VIEWS, ON PURPOSE (see scripts/privacycare/seed_connection.py's
    # own "TWO DIFFERENT NETWORK VIEWS" docstring section). The row on disk
    # deliberately holds the CONTAINER's view of fides-db (host="fides-db",
    # port=5432) — that is what the live `fides` API container needs to
    # reach this same datastore in production, and it is Docker-internal
    # DNS, not host DNS. Host-side pytest is not on that network and cannot
    # resolve "fides-db" at all. Rather than depending on a host
    # `/etc/hosts` entry (wrong on any other machine, wrong in CI, and
    # stale the moment the container is recreated with a new bridge IP),
    # override the in-memory secrets to the HOST's view (127.0.0.1:5442,
    # the docker-compose port mapping — same host/port as this file's own
    # DB_URL) for the life of this test only. This mutates the ORM instance
    # inside the `db` fixture's session, which is rolled back at teardown,
    # so the row on disk keeps the container view production needs; only
    # this in-memory copy, local to the test process, sees the host view.
    connection.secrets = {**connection.secrets, "host": "127.0.0.1", "port": 5442}
    return connection


def test_the_walk_never_selects_from_a_scanned_table(scratch_connection):
    # SPEC D-EX-1. This is the assertion the whole design rests on. If it ever
    # fails, the scanner has started reading customer data and the fix is not
    # to relax the test.
    statements = []

    @sqlalchemy.event.listens_for(sqlalchemy.engine.Engine, "before_cursor_execute")
    def _record(conn, cursor, statement, *args):  # noqa: ANN001
        statements.append(statement)

    try:
        walk_catalogue(scratch_connection, monitor_key="m1", databases=[], excluded_databases=[])
    finally:
        sqlalchemy.event.remove(sqlalchemy.engine.Engine, "before_cursor_execute", _record)

    for statement in statements:
        lowered = " ".join(statement.lower().split())
        # Catalogue reads are SELECTs against pg_catalog / information_schema.
        # A SELECT against anything else is a read of customer data.
        if lowered.startswith("select"):
            assert ("pg_catalog" in lowered or "information_schema" in lowered
                    or "pg_" in lowered), f"the walk read a non-catalogue table: {statement}"


def test_the_walk_returns_all_four_resource_levels(scratch_connection):
    # The URN has five segments (D-EX-2), so the database is a level the walk
    # must emit even though a Postgres connection is already scoped to one --
    # otherwise a field's URN cannot be assembled and the UI's segment [1]
    # lookup finds a schema where it expects a project.
    found = walk_catalogue(scratch_connection, monitor_key="m1",
                           databases=[], excluded_databases=[])

    kinds = {r.resource_type for r in found}
    assert kinds == {"Database", "Schema", "Table", "Field"}


def test_urns_are_dot_separated_and_start_with_the_monitor_key(scratch_connection):
    # D-EX-2: the shipped UI parses this shape — findProjectFromUrn takes
    # segment [1], getResourceName documents "monitor.project?.dataset.table.field".
    found = walk_catalogue(scratch_connection, monitor_key="m1",
                           databases=[], excluded_databases=[])

    for resource in found:
        assert resource.urn.startswith("m1.")

    a_field = next(r for r in found if r.resource_type == "Field")
    assert len(a_field.urn.split(".")) == 5, a_field.urn


def test_every_resource_names_its_parent_except_the_root(scratch_connection):
    found = walk_catalogue(scratch_connection, monitor_key="m1",
                           databases=[], excluded_databases=[])
    by_urn = {r.urn: r for r in found}

    for resource in found:
        if resource.parent_urn is not None:
            assert resource.parent_urn in by_urn, f"{resource.urn} names a parent nobody produced"


def test_information_schema_is_never_walked(scratch_connection):
    # D-EX-8. Nothing personal lives in a catalogue, and offering it as a
    # scannable scope unit only invites a confusing choice.
    found = walk_catalogue(scratch_connection, monitor_key="m1",
                           databases=[], excluded_databases=[])

    assert not any("information_schema" in r.urn for r in found)


def test_a_named_scope_limits_the_walk(scratch_connection):
    # D-EX-8: a monitor naming databases scans only those.
    #
    # The scratch database's only non-system schema is "public" -- an
    # unscoped walk always returns exactly {"public"}, so scoping to
    # "public" and asserting the result is {"public"} would hold whether the
    # inclusion filter is present, inverted, or deleted: there is nothing
    # for a broken filter to wrongly include. A second, genuinely distinct
    # schema is required so the test has something to prove the filter
    # excludes.
    #
    # `db`'s own transaction (rolled back at teardown) is NOT enough to
    # create it: walk_catalogue opens its own connection via
    # get_connector()/create_client(), a separate Postgres backend session
    # from `db`'s, and Postgres never lets one session see another
    # session's uncommitted writes -- verified directly (see task-1-report.md):
    # a schema created and flushed, but not committed, in one connection
    # does not appear in get_schema_names() on a second, independent
    # connection to the same database. So the probe schema below is created
    # on its own autocommit connection -- genuinely committed, so the walk's
    # own separate connection actually sees it -- and dropped again in a
    # `finally` on that same connection. That is a real create-then-drop,
    # not a transactional rollback, but the net effect is identical: nothing
    # is left behind. The DB enumeration check (schema count back to
    # baseline) is the proof.
    probe_schema = f"t11_scope_probe_{uuid.uuid4().hex[:8]}"
    probe_engine = sqlalchemy.create_engine(DB_URL, isolation_level="AUTOCOMMIT")
    try:
        with probe_engine.connect() as conn:
            conn.execute(sqlalchemy.text(f'CREATE SCHEMA "{probe_schema}"'))

        everything = walk_catalogue(scratch_connection, monitor_key="m1",
                                    databases=[], excluded_databases=[])
        schema_names = {r.name for r in everything if r.resource_type == "Schema"}
        assert probe_schema in schema_names, "probe schema was not visible to the walk"

        scoped = walk_catalogue(scratch_connection, monitor_key="m1",
                                databases=["public"], excluded_databases=[])

        scoped_schema_names = {r.name for r in scoped if r.resource_type == "Schema"}
        assert scoped_schema_names == {"public"}, scoped_schema_names
    finally:
        with probe_engine.connect() as conn:
            conn.execute(sqlalchemy.text(f'DROP SCHEMA IF EXISTS "{probe_schema}" CASCADE'))
        probe_engine.dispose()


def test_an_excluded_scope_removes_it(scratch_connection):
    everything = walk_catalogue(scratch_connection, monitor_key="m1",
                                databases=[], excluded_databases=[])
    a_schema = next(r for r in everything if r.resource_type == "Schema")

    remaining = walk_catalogue(scratch_connection, monitor_key="m1",
                               databases=[], excluded_databases=[a_schema.name])

    assert a_schema.name not in {r.name for r in remaining if r.resource_type == "Schema"}


def test_fields_carry_their_sql_type(scratch_connection):
    found = walk_catalogue(scratch_connection, monitor_key="m1",
                           databases=[], excluded_databases=[])

    fields = [r for r in found if r.resource_type == "Field"]
    assert fields, "no fields were found at all"
    assert all(f.field_type for f in fields)


def test_an_unreachable_connection_raises_naming_the_connection(db):
    # Same contract as _list_databases in api/monitors.py: the target's failure
    # is the target's, and the message names which connection could not be read.
    from fides.api.models.connectionconfig import ConnectionConfig

    unreachable = ConnectionConfig(
        key=f"t11_unreachable_{uuid.uuid4().hex[:8]}",
        name="unreachable",
        connection_type="postgres",
        access="read",
    )
    unreachable.secrets = {"host": "127.0.0.1", "port": 1, "dbname": "nope",
                           "username": "u", "password": "p"}
    db.add(unreachable)
    db.flush()
    # A plain attribute assignment (connection_type="postgres" above) never
    # goes through the Enum column's result-processing -- only a DB load
    # does -- so without this, `connection_type` stays the raw str
    # "postgres" rather than becoming ConnectionType.postgres, and
    # get_connector() dies one step earlier than intended, at
    # `conn_config.connection_type.value` ('str' object has no attribute
    # 'value'), before anything ever tries to open a socket. db.refresh()
    # reloads the row from the DB within this same (uncommitted, still
    # rolled back at teardown) transaction, which does run it through the
    # column's result processor and coerces connection_type to the real
    # enum -- letting get_connector() succeed and the connect attempt to
    # 127.0.0.1:1 actually happen.
    db.refresh(unreachable)

    with pytest.raises(Exception) as caught:
        walk_catalogue(unreachable, monitor_key="m1", databases=[], excluded_databases=[])
    assert unreachable.key in str(caught.value)
