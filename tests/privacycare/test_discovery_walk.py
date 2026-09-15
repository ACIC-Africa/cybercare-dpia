"""The catalogue walk: what is in this datastore, and nothing about what is IN it.

Spec D-EX-1 — the scanner reads STRUCTURE and never a row of customer data. A
discovery scan runs against production systems belonging to a customer with a
statutory duty to protect what is in them, and reading personal data while
cataloguing it is the least defensible thing this could do. The test that
asserts no SELECT is issued is the most important test in this plan.
"""
import re
import uuid

import pytest
import sqlalchemy
from sqlalchemy.orm import Session

from fides.api.privacycare.discovery.walk import (
    CatalogueWalkError,
    FoundResource,
    walk_catalogue,
)

DB_URL = "postgresql://postgres:fides@127.0.0.1:5442/fides"
SCRATCH_KEY = "privacycare_scratch_local_postgres"

# F1 fix. The ORIGINAL guard here asserted a statement was safe if it
# contained "pg_catalog", "information_schema", OR the bare substring
# "pg_" -- and only ever looked at statements starting with "select". The
# customer is an LPG marketer: `SELECT id_number FROM public.lpg_customers`
# contains "pg_" (inside "lpg_customers") and so satisfied the guard that
# exists to stop exactly that read. See fix-wave-report.md for the
# deliberate-break proof below.
#
# This tightened version instead extracts every FROM/JOIN (and bare TABLE)
# read target from the statement and requires EACH one, individually, to be
# a catalogue relation: `pg_catalog.*`, `information_schema.*`, or an
# UNQUALIFIED relation whose own name starts with "pg_" (Postgres reserves
# the "pg_" prefix for schema names, so a qualified target like
# "public.lpg_customers" can never satisfy this by having a table merely
# CONTAINING "pg_" -- the schema segment must itself be pg_catalog /
# information_schema, or there must be no schema segment at all and the
# relation itself is bare-"pg_*"). This is a pragmatic regex-based scanner,
# not a full SQL parser -- it is deliberately over- rather than
# under-inclusive about what counts as a "target": scanning globally for
# every FROM/JOIN in the whole statement (not just the outermost one) means
# a target hidden inside a subquery is still caught, at the cost of also
# flagging an outer CTE-alias reference (e.g. "WITH x AS (...) SELECT * FROM
# x") -- handled by excluding names the statement itself defines as a CTE.
_READABLE_STATEMENT_RE = re.compile(
    r"^\s*(select|with|table|copy|fetch|explain)\b", re.IGNORECASE
)
_TARGET_RE = re.compile(
    r'\b(?:from|join)\s+('
    r'"[^"]+"(?:\."[^"]+")?'  # "schema"."table" or "table"
    r'|[a-zA-Z_][\w$]*(?:\.[a-zA-Z_][\w$]*)?'  # schema.table or table
    r")",
    re.IGNORECASE,
)
_TABLE_SHORTHAND_RE = re.compile(
    r'^\s*table\s+("[^"]+"(?:\."[^"]+")?|[a-zA-Z_][\w$.]*)', re.IGNORECASE
)
_CTE_NAME_RE = re.compile(
    r"\b(?:with|,)\s+([a-zA-Z_][\w$]*)\s+as\s*\(", re.IGNORECASE
)


def _read_targets(lowered_statement: str) -> list[str]:
    """Every FROM/JOIN target, plus a bare `TABLE <name>` shorthand's own
    target, found anywhere in `lowered_statement`."""
    targets = [match.group(1) for match in _TARGET_RE.finditer(lowered_statement)]
    table_shorthand = _TABLE_SHORTHAND_RE.match(lowered_statement)
    if table_shorthand:
        targets.append(table_shorthand.group(1))
    return targets


def _is_catalogue_relation(target: str) -> bool:
    """True only for `pg_catalog.*`, `information_schema.*`, or an
    unqualified relation whose own name starts with "pg_"."""
    name = target.strip('"').replace('"."', ".").replace('"', "")
    if name.startswith("pg_catalog.") or name.startswith("information_schema."):
        return True
    return "." not in name and name.startswith("pg_")


def _assert_statement_reads_only_the_catalogue(statement: str) -> None:
    """The tightened D-EX-1 guard, factored out so both the real-walk test
    below and the deliberate-break tests can drive the same logic. Raises
    AssertionError naming the offending target the moment it finds a
    read target that is not a catalogue relation.
    """
    lowered = " ".join(statement.lower().split())
    if not _READABLE_STATEMENT_RE.match(lowered):
        # Widened from the original's "startswith('select')" check: a
        # statement that cannot read rows at all (DDL, SET, BEGIN, ...)
        # needs no target inspection.
        return
    cte_names = {match.group(1) for match in _CTE_NAME_RE.finditer(lowered)}
    for target in _read_targets(lowered):
        bare = target.strip('"').replace('"."', ".").replace('"', "")
        if "." not in bare and bare in cte_names:
            # A CTE's own alias, not a real relation -- its DEFINITION was
            # already scanned above (the global FROM/JOIN sweep does not
            # stop at the CTE boundary), so this reference needs no check
            # of its own.
            continue
        assert _is_catalogue_relation(target), (
            f"the walk read a non-catalogue relation {target!r}: {statement}"
        )


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

    # F1 fix: a walk that issued NOTHING would previously have passed this
    # guard vacuously -- there being no statements to fail on is not the
    # same as proving every statement was catalogue-only.
    assert statements, "the walk issued no statements at all"

    for statement in statements:
        _assert_statement_reads_only_the_catalogue(statement)


def test_the_tightened_guard_rejects_a_read_of_the_customer_table():
    # F1 deliberate-break proof (see fix-wave-report.md). The customer is an
    # LPG marketer -- this exact statement is what the ORIGINAL guard's
    # "pg_" bare-substring check let through, because "lpg_customers"
    # contains "pg_" as a substring. It is fed straight to the tightened
    # helper the real test above now uses, never actually issued against a
    # connection, to prove the guard itself -- not the scratch fixture's
    # honesty -- is what catches it.
    statement = "select id_number from public.lpg_customers"

    with pytest.raises(AssertionError, match="non-catalogue relation") as caught:
        _assert_statement_reads_only_the_catalogue(statement)

    assert "public.lpg_customers" in str(caught.value)


@pytest.mark.parametrize(
    "statement",
    [
        # F1's second hole: the original guard only ever inspected
        # statements starting with "select". Each of these can read rows
        # and was never examined at all.
        pytest.param(
            "with x as (select id_number from public.lpg_customers) "
            "select * from x",
            id="cte",
        ),
        pytest.param("table public.lpg_customers", id="table-shorthand"),
        pytest.param(
            "copy (select id_number from public.lpg_customers) to stdout",
            id="copy-to-stdout",
        ),
        pytest.param(
            "explain analyze select id_number from public.lpg_customers",
            id="explain-analyze",
        ),
    ],
)
def test_the_tightened_guard_rejects_every_widened_statement_shape(statement):
    with pytest.raises(AssertionError, match="non-catalogue relation"):
        _assert_statement_reads_only_the_catalogue(statement)


def test_the_tightened_guard_does_not_false_positive_on_a_pure_catalogue_cte():
    # A CTE whose body only reads the catalogue, referenced by its own
    # alias in the outer query, must NOT be flagged -- the alias itself is
    # not a real relation.
    statement = "with x as (select nspname from pg_namespace) select * from x"
    _assert_statement_reads_only_the_catalogue(statement)  # must not raise


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

    with pytest.raises(CatalogueWalkError) as caught:
        walk_catalogue(unreachable, monitor_key="m1", databases=[], excluded_databases=[])
    assert unreachable.key in str(caught.value)

    # MINOR fix: `pytest.raises(Exception) + key-in-message` is satisfied by
    # ANY failure inside walk_catalogue -- every failure is wrapped in a
    # CatalogueWalkError that always names the key, so the original
    # assertion proved the wrapper exists, not that the socket itself
    # failed. Assert the wrapped cause is a real connection error.
    assert isinstance(caught.value.__cause__, sqlalchemy.exc.OperationalError), (
        f"expected a real connection failure as the cause, got "
        f"{caught.value.__cause__!r}"
    )


def test_views_and_materialised_views_are_walked_as_tables(scratch_connection):
    # F8: get_table_names() alone only returns ordinary/partitioned tables
    # (relkind 'r'/'p') and silently skips views and materialised views
    # (relkind 'v'/'m') entirely -- a view over a personal-data table is
    # exactly what discovery must find. StagedResourceType has no View
    # member and the shipped UI has no View concept, so a view is emitted
    # as a Table tagged via meta.table_type (written by reconcile.py, see
    # test_discovery_reconcile.py's own coverage of that).
    #
    # Same "two separate Postgres sessions" reasoning as
    # test_a_named_scope_limits_the_walk above: the probe objects are
    # created (and dropped, in `finally`) on their own AUTOCOMMIT
    # connection, genuinely committed, so the walk's own separate
    # connection actually sees them; nothing is left behind afterwards.
    suffix = uuid.uuid4().hex[:8]
    base_table = f"t11_view_base_{suffix}"
    plain_view = f"t11_plain_view_{suffix}"
    matview = f"t11_matview_{suffix}"
    probe_engine = sqlalchemy.create_engine(DB_URL, isolation_level="AUTOCOMMIT")
    try:
        with probe_engine.connect() as conn:
            conn.execute(
                sqlalchemy.text(
                    f'CREATE TABLE public."{base_table}" (id int, id_number text)'
                )
            )
            conn.execute(
                sqlalchemy.text(
                    f'CREATE VIEW public."{plain_view}" AS '
                    f'SELECT id, id_number FROM public."{base_table}"'
                )
            )
            conn.execute(
                sqlalchemy.text(
                    f'CREATE MATERIALIZED VIEW public."{matview}" AS '
                    f'SELECT id, id_number FROM public."{base_table}"'
                )
            )

        found = walk_catalogue(
            scratch_connection, monitor_key="m1", databases=["public"],
            excluded_databases=[],
        )
        tables_by_name = {
            r.name: r for r in found if r.resource_type == "Table"
        }

        assert plain_view in tables_by_name, "the plain view was never found"
        assert matview in tables_by_name, "the materialised view was never found"
        assert tables_by_name[plain_view].table_type == "view"
        assert tables_by_name[matview].table_type == "materialized_view"
        # An ordinary table is untagged -- see walk.py's FoundResource
        # docstring: None means "leave meta.table_type unset", not "table".
        assert tables_by_name[base_table].table_type is None

        view_urn = tables_by_name[plain_view].urn
        view_fields = {
            r.name for r in found
            if r.resource_type == "Field" and r.parent_urn == view_urn
        }
        assert view_fields == {"id", "id_number"}, (
            "the view's own columns were not walked -- get_columns() should "
            "work identically for a view as for a table"
        )
    finally:
        with probe_engine.connect() as conn:
            conn.execute(
                sqlalchemy.text(f'DROP MATERIALIZED VIEW IF EXISTS public."{matview}"')
            )
            conn.execute(
                sqlalchemy.text(f'DROP VIEW IF EXISTS public."{plain_view}"')
            )
            conn.execute(
                sqlalchemy.text(f'DROP TABLE IF EXISTS public."{base_table}" CASCADE')
            )
        probe_engine.dispose()
