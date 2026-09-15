"""The catalogue walk: what is in a datastore, never what is IN its rows.

SPEC D-EX-1. Everything below goes through `sqlalchemy.inspect()`, which on
Postgres reads `information_schema` / `pg_catalog` — never a customer's own
table. `walk_catalogue` writes nothing: no StagedResource row, no commit, no
side effect on the target beyond the read-only catalogue queries `inspect()`
issues itself. It opens a connection, introspects, and returns a plain list
for the caller (Task 2's reconciler) to stage. Test coverage for the no-SELECT
guarantee lives in tests/privacycare/test_discovery_walk.py and is, by design,
the most important test in this plan.

Connection path is the same one `api/monitors.py`'s `_list_databases` already
uses — `get_connector(connection_config)`, `connector.create_client()`,
`sqlalchemy.inspect()` — and the engine is disposed in a `finally` for the
same reason that function's docstring gives: `create_client()` builds a new
Engine every call, and `with engine.connect()` only returns the connection to
that engine's own pool, which otherwise stays open until GC.
"""
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import sqlalchemy
from loguru import logger
from sqlalchemy.engine.reflection import Inspector

from fides.api.models.connectionconfig import ConnectionConfig
from fides.api.models.detection_discovery.core import StagedResourceType
from fides.api.service.connectors import get_connector

# D-EX-8 / M12 (api/monitors.py's _list_databases filters the same schema
# from the monitor's picker, for the same reason): nothing personal lives in
# a catalogue, so it is never offered as a scope unit and never walked.
# SQLAlchemy's get_schema_names() already omits "pg_%" on Postgres but not
# this one.
_EXCLUDED_SYSTEM_SCHEMAS = frozenset({"information_schema"})


class CatalogueWalkError(Exception):
    """The target could not be introspected. Message names the connection key,
    never the underlying exception — see the same reasoning in
    api/monitors.py's `_list_databases` (I6 fix): in dev mode SQLAlchemy's
    error strings can carry the connection URI, built from `secrets`,
    including the password."""


@dataclass(frozen=True)
class FoundResource:
    """One node the walk found — a database, schema, table or field.

    Frozen and produced by a function that writes nothing: this is a
    reporting shape, not a persisted one. Task 2's reconciler is the first
    thing that turns a list of these into a database write.
    """

    urn: str
    name: str
    resource_type: str  # a StagedResourceType value: Database | Schema | Table | Field
    parent_urn: Optional[str]
    field_type: Optional[str] = None  # the column's SQL type, Field only


def walk_catalogue(
    connection_config: ConnectionConfig,
    *,
    monitor_key: str,
    databases: List[str],
    excluded_databases: List[str],
) -> List[FoundResource]:
    """Introspect `connection_config`'s catalogue; return every resource found.

    NAMING TRAP — read before touching `databases` / `excluded_databases`.
    Despite the name, `MonitorConfig.databases` holds Postgres SCHEMA names,
    not database names: plan 10's picker populates it from
    `get_schema_names()`, and its own test asserts "public" and
    "information_schema" come back. A single Postgres connection is already
    scoped to one database, so there is no database-level filter for this
    function to apply even though the parameter name suggests one — both
    lists below are matched against SCHEMA names, exactly like the monitor
    config they are read from. This is Ethyca's name, not ours, and it
    misleads; it is kept verbatim here only because it is the shape plan
    10's `MonitorConfig` already exposes to this function's caller.

    URN shape (D-EX-2): "<monitor_key>.<database>.<schema>.<table>.<field>",
    five dot-separated segments assigned by position so the shape is stable
    across runs regardless of what changed since the last walk. The shipped
    admin UI parses exactly this: `findProjectFromUrn` reads segment [1] as
    the project, and `getResourceName` documents the shape as
    "monitor.project?.dataset.table.field". All four levels are emitted,
    including the database — a Postgres connection is already scoped to one,
    but without that level a field's five-segment URN could not be
    assembled.
    """
    engine = None
    try:
        connector = get_connector(connection_config)
        engine = connector.create_client()
        with engine.connect() as sql_connection:
            inspector = sqlalchemy.inspect(sql_connection)
            # engine.url.database, not connection_config.secrets["dbname"]:
            # this reads back whatever database the engine actually opened,
            # rather than assuming every connector family shapes its secrets
            # the same way Postgres does.
            database_name = engine.url.database or connection_config.key
            return _walk_database(
                inspector,
                monitor_key=monitor_key,
                database_name=database_name,
                databases=databases,
                excluded_databases=excluded_databases,
            )
    except Exception as error:  # noqa: BLE001 — the target's failure, not ours
        logger.warning(
            "Could not walk catalogue for connection {}: {}",
            connection_config.key,
            error,
        )
        raise CatalogueWalkError(
            f"Could not walk catalogue for connection {connection_config.key}"
        ) from error
    finally:
        # Plan 10's final review found exactly this leak in the adjacent
        # _list_databases: create_client() returns a fresh Engine every
        # call, and closing the connection alone leaves it pooled and open.
        if engine is not None:
            engine.dispose()


def _walk_database(
    inspector: Inspector,
    *,
    monitor_key: str,
    database_name: str,
    databases: List[str],
    excluded_databases: List[str],
) -> List[FoundResource]:
    """The actual walk, once a connected `Inspector` exists. Split out from
    `walk_catalogue` so the connection lifecycle (open/dispose) stays in one
    place and the traversal — the part with scope-filtering to get right —
    stays in another."""
    found: List[FoundResource] = []

    database_urn = f"{monitor_key}.{database_name}"
    found.append(
        FoundResource(
            urn=database_urn,
            name=database_name,
            resource_type=StagedResourceType.DATABASE.value,
            parent_urn=None,
        )
    )

    for schema_name in inspector.get_schema_names():
        if schema_name in _EXCLUDED_SYSTEM_SCHEMAS:
            continue
        if databases and schema_name not in databases:
            continue
        if schema_name in excluded_databases:
            continue

        found.extend(
            _walk_schema(inspector, database_urn=database_urn, schema_name=schema_name)
        )

    return found


def _walk_schema(
    inspector: Inspector, *, database_urn: str, schema_name: str
) -> List[FoundResource]:
    """One schema: itself, its tables, and each table's fields."""
    found: List[FoundResource] = []

    schema_urn = f"{database_urn}.{schema_name}"
    found.append(
        FoundResource(
            urn=schema_urn,
            name=schema_name,
            resource_type=StagedResourceType.SCHEMA.value,
            parent_urn=database_urn,
        )
    )

    for table_name in inspector.get_table_names(schema=schema_name):
        table_urn = f"{schema_urn}.{table_name}"
        found.append(
            FoundResource(
                urn=table_urn,
                name=table_name,
                resource_type=StagedResourceType.TABLE.value,
                parent_urn=schema_urn,
            )
        )

        for column in inspector.get_columns(table_name, schema=schema_name):
            found.append(_field_resource(table_urn=table_urn, column=column))

    return found


def _field_resource(*, table_urn: str, column: Dict[str, Any]) -> FoundResource:
    """One column. `column["type"]` is a SQLAlchemy TypeEngine, not a plain
    string — str() renders its SQL type name (e.g. "VARCHAR(50)")."""
    name = column["name"]
    return FoundResource(
        urn=f"{table_urn}.{name}",
        name=name,
        resource_type=StagedResourceType.FIELD.value,
        parent_urn=table_urn,
        field_type=str(column["type"]),
    )
