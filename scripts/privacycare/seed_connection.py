#!/usr/bin/env python3
"""Seed the one local ConnectionConfig a discovery monitor can bind to.

D-DM-5: exactly one ConnectionConfig, connection_type='postgres', key
`privacycare_scratch_local_postgres`, pointing at our own already-running
fides-db — never a customer system. Pointing a monitor at anything of
Josephine's stays gated on her authorisation, and nothing in this plan
scans; the row exists purely so the discovery-monitor configuration screen
has something real to bind to and list databases against.

Same shape as scripts/privacycare/import_processes.py and load_taxonomy.py:
one argparse CLI, one session, dry-run by default, `--commit` to persist.
`_database_url()` and `_target_description()` are copied verbatim from
import_processes.py (itself copied from migrations/env.py) rather than
imported — see load_taxonomy.py's module docstring for why importing
env.py isn't safe here.

Secrets come from the environment at run time and are never written into
the repo or printed — `main()` only ever prints the `target:` line
(host/port/database, never the password, never the raw URL).

TWO DIFFERENT NETWORK VIEWS, ON PURPOSE (fix round 1, Finding 1). This
script's OWN session — `_database_url()`, FIDES__DATABASE__* — is how THIS
PROCESS, running on the host, reaches fides-db to write the seed row. That
is the host's view: fides-db is published to 127.0.0.1:5442.
`connection.secrets`, by contrast, is how the LIVE `fides` API CONTAINER
will later reach the same datastore when it serves get_monitor_databases
(and, eventually, monitor execution) — and inside that container,
127.0.0.1 is the container itself, not fides-db; only the Docker-internal
hostname (`fides-db`, port 5432 — the same values `.fides/fides.toml`'s own
[database] section uses) is reachable. Conflating the two — seeding
`secrets` with the script's own host-side FIDES__DATABASE__* values — was
the exact bug fix round 1 found: unit tests (which run on the host, same
network view as the script) never caught it; only the rendered check,
driving the actual container, did. So `secrets`' host/port get their OWN
env vars, PRIVACYCARE_SCRATCH_DB_HOST / PRIVACYCARE_SCRATCH_DB_PORT,
defaulting to the container view (fides-db:5432) — deliberately NOT
FIDES__DATABASE__SERVER/PORT, which mean the other, host-side thing.
Username/password/dbname are unaffected: same Postgres instance, same
credentials, regardless of which network path reaches it — those still
come from FIDES__DATABASE__USER/PASSWORD/DB, same as everywhere else.

`seed_connection(db)` never commits — the caller's session boundary decides,
matching load_taxonomy.py and import_processes.py. It is idempotent on the
key: a second call against a session that already has the row does nothing.
The row itself is inserted with raw SQL (id/key/name/connection_type/
access/disabled — none of it sensitive), but `secrets` is set through the
ORM afterwards: connectionconfig.secrets is
`MutableDict.as_mutable(encrypted_type(...))`, and the column's own comment
in src/fides/api/models/connectionconfig.py says "Avoid bulk/raw SQL
updates to secrets; use ORM instance-level updates to ensure events fire."
Task 3's test fixture (tests/privacycare/test_api_monitors.py) follows the
same split; this does the same.
"""
import argparse
import os
import sys
import uuid

import sqlalchemy
from sqlalchemy.engine.url import make_url
from sqlalchemy.orm import Session

import fides.api.db.base  # noqa: F401 — see below
from fides.api.models.connectionconfig import ConnectionConfig

KEY = "privacycare_scratch_local_postgres"

# `fides.api.db.base` is imported (but not from) purely for its side effect:
# it is Alembic's single central import point that pulls in every mapped
# SQLAlchemy class before any of them are used. Without it, SQLAlchemy fails
# to resolve ConnectionConfig's relationships (e.g. FidesUser ->
# SystemManager) the first time the mapper configures, because those other
# classes were never imported and registered. Every other privacycare
# script imports through a module that transitively pulls in
# fides.api.db.base already; this is the first one that touches a model
# directly, so it needs the import spelled out. `ConnectionConfig` itself
# is then imported from its own module, not re-exported from
# fides.api.db.base, which mypy would otherwise flag as an implicit
# re-export.


def _database_url() -> str:
    # Copied from scripts/privacycare/import_processes.py's
    # _database_url() verbatim (same precedence, same env vars, same
    # defaults) rather than imported — see load_taxonomy.py's module
    # docstring for why importing env.py isn't safe here.
    return os.environ.get(
        "PRIVACYCARE_DATABASE_URL",
        "postgresql://{u}:{p}@{h}:{port}/{db}".format(
            u=os.environ.get("FIDES__DATABASE__USER", "postgres"),
            p=os.environ.get("FIDES__DATABASE__PASSWORD", "fides"),
            h=os.environ.get("FIDES__DATABASE__SERVER", "127.0.0.1"),
            port=os.environ.get("FIDES__DATABASE__PORT", "5442"),
            db=os.environ.get("FIDES__DATABASE__DB", "fides"),
        ),
    )


def _target_description(database_url: str) -> str:
    """Render `database_url` as `user@host:port/db` for the pre-write
    target line. NEVER includes the password, and never prints the raw
    URL — a URL with embedded credentials is exactly what a consultant
    should not paste into a terminal transcript or a ticket.
    """
    parsed = make_url(database_url)
    user = parsed.username or ""
    location = parsed.host or ""
    if parsed.port:
        location = f"{location}:{parsed.port}"
    database = parsed.database or ""
    return f"{user}@{location}/{database}"


def seed_connection(db: Session) -> None:
    """Insert the seeded connection if it is absent; do nothing if present.

    Never commits — the caller's session boundary decides. Secrets are set
    through the ORM instance, per the column comment on
    ConnectionConfig.secrets (see module docstring).
    """
    existing = db.query(ConnectionConfig).filter(ConnectionConfig.key == KEY).first()
    if existing is not None:
        return

    db.execute(
        sqlalchemy.text(
            "INSERT INTO connectionconfig (id, key, name, connection_type, "
            " access, disabled) "
            "VALUES (:id, :key, :name, 'postgres', 'write', false)"
        ),
        {"id": f"conn_{uuid.uuid4().hex[:12]}", "key": KEY, "name": KEY},
    )
    db.flush()

    # .one(), not .first(): the row was just inserted above in this same
    # transaction, so it must exist — and .one() (unlike .first()) types as
    # non-Optional, which is what lets `connection.secrets = ...` below
    # satisfy mypy without an assert.
    connection = db.query(ConnectionConfig).filter(ConnectionConfig.key == KEY).one()
    # host/port describe how the LIVE `fides` API CONTAINER reaches
    # fides-db, NOT how this script (running on the host) reaches it — see
    # the module docstring's "TWO DIFFERENT NETWORK VIEWS" section. Default
    # to the Docker-internal address (fides-db:5432, matching
    # .fides/fides.toml's own [database] section), overridable by their own
    # dedicated env vars for a deployment where that address differs.
    # Deliberately NOT FIDES__DATABASE__SERVER/PORT — those name the host's
    # view (127.0.0.1:5442) and reusing them here reproduces fix round 1's
    # bug: a discovery monitor bound to this connection would 502 on every
    # database listing, because 127.0.0.1 inside the container is the
    # container itself, not fides-db.
    connection.secrets = {
        "host": os.environ.get("PRIVACYCARE_SCRATCH_DB_HOST", "fides-db"),
        "port": int(os.environ.get("PRIVACYCARE_SCRATCH_DB_PORT", "5432")),
        "username": os.environ.get("FIDES__DATABASE__USER", "postgres"),
        "password": os.environ.get("FIDES__DATABASE__PASSWORD", "fides"),
        "dbname": os.environ.get("FIDES__DATABASE__DB", "fides"),
    }
    db.flush()


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Seed the one local ConnectionConfig a discovery "
        f"monitor can bind to ({KEY}), pointing at our own fides-db."
    )
    parser.add_argument(
        "--commit",
        action="store_true",
        help="Actually commit the transaction. Without this, the run is a "
        "dry run (rolled back, nothing written).",
    )
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = _parse_args(argv)

    database_url = _database_url()
    # Name the target BEFORE opening a session or writing anything, so a
    # consultant pointing this at the wrong database finds out immediately.
    # Never the password, never the raw URL.
    print(f"target: {_target_description(database_url)}")

    engine = sqlalchemy.create_engine(database_url)
    # Not `with Session(engine) as db:` — the sqlmypy plugin (still 1.x-era,
    # per pyproject.toml's [tool.mypy] plugins) doesn't see Session's
    # context-manager protocol and flags __enter__/__exit__ as missing.
    # try/finally gets the same close-on-exit guarantee without tripping it.
    db = Session(engine)
    try:
        seed_connection(db)

        if args.commit:
            db.commit()
            print("COMMITTED")
        else:
            db.rollback()
            print("DRY RUN — nothing written")
    finally:
        db.close()

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
