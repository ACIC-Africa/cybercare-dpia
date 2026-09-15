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
    everything = walk_catalogue(scratch_connection, monitor_key="m1",
                                databases=[], excluded_databases=[])
    a_schema = next(r for r in everything if r.resource_type == "Schema")
    schema_name = a_schema.name

    scoped = walk_catalogue(scratch_connection, monitor_key="m1",
                            databases=[schema_name], excluded_databases=[])

    assert {r.name for r in scoped if r.resource_type == "Schema"} == {schema_name}


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

    with pytest.raises(Exception) as caught:
        walk_catalogue(unreachable, monitor_key="m1", databases=[], excluded_databases=[])
    assert unreachable.key in str(caught.value)
