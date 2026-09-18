"""The one local connection a monitor can be demonstrated against.

D-DM-5: it points at our own fides-db, never a customer system. Nothing here
scans; the connection exists so the configuration screen has something real to
bind to.
"""
import importlib.util
import pathlib
import subprocess
import sys
import uuid

import pytest
import sqlalchemy
from sqlalchemy.orm import Session

DB_URL = "postgresql://postgres:fides@127.0.0.1:5442/fides"
KEY = "privacycare_scratch_local_postgres"

# M15 (test hygiene): resolved from __file__ rather than passed as a
# relative string, so the two subprocess tests below pass regardless of
# pytest's current working directory — previously they only passed when
# pytest ran from the repo root.
_SCRIPT_PATH = (
    pathlib.Path(__file__).resolve().parents[2]
    / "scripts/privacycare/seed_connection.py"
)


@pytest.fixture
def db(monkeypatch):
    engine = sqlalchemy.create_engine(DB_URL)
    with Session(engine) as session:
        monkeypatch.setattr(session, "commit", lambda: None)
        yield session
        session.rollback()


def _load_cli():
    path = (
        pathlib.Path(__file__).resolve().parents[2]
        / "scripts/privacycare/seed_connection.py"
    )
    spec = importlib.util.spec_from_file_location("privacycare_seed_connection", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_dry_run_writes_nothing():
    before = _count()
    out = subprocess.run(
        [sys.executable, str(_SCRIPT_PATH)],
        capture_output=True, text=True, check=True,
    ).stdout
    assert "DRY RUN — nothing written" in out
    assert _count() == before


def test_the_run_names_its_target_and_never_the_password():
    # M15: this used to assert bare `"fides" in out`, which the DATABASE
    # NAME satisfies — but "fides" is ALSO _database_url()'s own default
    # FIDES__DATABASE__PASSWORD literal, so the assertion could not actually
    # distinguish "the db name was printed" (legitimate) from "the password
    # leaked" (not). Overriding FIDES__DATABASE__PASSWORD to something
    # distinctive isn't an option here — the live fides-db genuinely
    # requires the real password to authenticate, and this test needs the
    # run to actually succeed. Counting occurrences is the discriminator
    # instead: the default password and the default db name are the SAME
    # string ("fides"), so if the password ever leaked too, "fides" would
    # appear MORE than once — the default username (postgres) and the
    # host/port never contain it.
    out = subprocess.run(
        [sys.executable, str(_SCRIPT_PATH)],
        capture_output=True, text=True, check=True,
    ).stdout
    assert "target:" in out
    target_line = next(line for line in out.splitlines() if line.startswith("target:"))
    assert target_line.endswith("/fides"), "target line should end with the database name"
    assert out.count("fides") == 1, (
        "'fides' should appear exactly once (the database name); "
        f"got {out.count('fides')} — the password may have leaked"
    )
    assert "postgres:fides@" not in out   # never a credential-bearing URL
    assert "postgresql://" not in out


def test_seeding_is_idempotent_on_the_key(db):
    cli = _load_cli()
    cli.seed_connection(db)
    cli.seed_connection(db)
    count = db.execute(
        sqlalchemy.text("SELECT count(*) FROM connectionconfig WHERE key = :k"),
        {"k": KEY},
    ).scalar()
    assert count == 1, "a second run created a second connection"


def test_the_seeded_connection_is_a_postgres_one(db):
    cli = _load_cli()
    cli.seed_connection(db)
    row = db.execute(
        sqlalchemy.text(
            "SELECT connection_type FROM connectionconfig WHERE key = :k"
        ),
        {"k": KEY},
    ).scalar()
    assert row == "postgres"


def test_the_seeded_connection_is_read_only(db):
    # I5 (final review): the seeded connection points at our OWN fides-db,
    # which holds connectionconfig (every other connection's encrypted
    # secrets), client (OAuth secrets) and fidesuser. 'write' access
    # (AccessLevel's own comment: "we can update/delete items in the
    # connected database") on our own application database is a live
    # exposure once plan 11 executes monitors or attaches a DSR policy to
    # this connection. 'read' demonstrates the exact same configuration
    # screen with none of that risk.
    #
    # seed_connection() is idempotent-by-short-circuit (an existing row is
    # never updated), so this needs a clean slate — same as the two secrets
    # tests above — to actually exercise the INSERT path this assertion is
    # about, rather than reading back whatever the live row already holds.
    _park_existing(db)
    cli = _load_cli()
    cli.seed_connection(db)
    access = db.execute(
        sqlalchemy.text("SELECT access FROM connectionconfig WHERE key = :k"),
        {"k": KEY},
    ).scalar()
    assert access == "read"


def _park_existing(db):
    # RENAMED AWAY FROM A DELETE (2026-09-18 fix). The live database
    # carries the authorised --commit'd row (the ONE intended permanent
    # write, per the brief) — but a real discovery scan (2026-09-18) has
    # since created a `monitorconfig` row that FK-references THIS row's
    # `id` (`monitorconfig_connection_config_id_fkey`). `DELETE FROM
    # connectionconfig WHERE key = :k` now raises ForeignKeyViolation on
    # the DELETE statement itself — Postgres checks the constraint
    # immediately, before this transaction's own rollback at teardown
    # ever gets a chance to matter. That monitorconfig row is legitimate
    # demonstration data (1881 staged resources depend on the same scan)
    # and must not be deleted to make this test pass.
    #
    # seed_connection() only needs a row that does NOT match `key == KEY`
    # to fall onto its own INSERT path — it never looks at `id`. Renaming
    # `key` aside (a plain unique column, NOT the FK's target, which is
    # `id`) achieves exactly that without touching the row the FK
    # references at all: the UPDATE below never conflicts with anything.
    # This still happens inside the same `db` fixture's transaction, which
    # test teardown rolls back, so the live row's `key` is restored once
    # the test ends (verified: the `db` fixture patches commit to a no-op
    # and always rolls back).
    db.execute(
        sqlalchemy.text("UPDATE connectionconfig SET key = :parked WHERE key = :k"),
        {"parked": f"{KEY}_parked_{uuid.uuid4().hex[:8]}", "k": KEY},
    )
    db.flush()


def test_the_seeded_secrets_describe_the_container_view_not_the_host_view(db):
    # Fix round 1, Finding 1: secrets.host/port are how the LIVE `fides` API
    # CONTAINER reaches fides-db (Docker-internal: fides-db:5432), NOT how
    # this script itself — running on the host, via _database_url() and
    # FIDES__DATABASE__SERVER/PORT — reaches it (127.0.0.1:5442). Reading
    # `secrets` back needs the ORM (it's an encrypted column; raw SQL would
    # return ciphertext), same as production code reads it.
    from fides.api.models.connectionconfig import ConnectionConfig

    _park_existing(db)
    cli = _load_cli()
    cli.seed_connection(db)
    connection = (
        db.query(ConnectionConfig).filter(ConnectionConfig.key == KEY).one()
    )
    assert connection.secrets["host"] == "fides-db"
    assert connection.secrets["port"] == 5432
    # Pin the failure mode fix round 1 actually hit: the host-side view must
    # never creep back in as the default.
    assert connection.secrets["host"] != "127.0.0.1"
    assert connection.secrets["port"] != 5442


def test_the_seeded_secrets_host_and_port_are_overridable(db, monkeypatch):
    # The dedicated env vars are for a deployment where the container-view
    # address differs from the fides-db:5432 default — deliberately separate
    # from FIDES__DATABASE__SERVER/PORT, which mean the host's view.
    monkeypatch.setenv("PRIVACYCARE_SCRATCH_DB_HOST", "some-other-host")
    monkeypatch.setenv("PRIVACYCARE_SCRATCH_DB_PORT", "6543")
    from fides.api.models.connectionconfig import ConnectionConfig

    _park_existing(db)
    cli = _load_cli()
    cli.seed_connection(db)
    connection = (
        db.query(ConnectionConfig).filter(ConnectionConfig.key == KEY).one()
    )
    assert connection.secrets["host"] == "some-other-host"
    assert connection.secrets["port"] == 6543


def _count() -> int:
    # M15: dispose the engine this helper creates on every call — same
    # leaked-connection pattern I4 fixes in the route code, here in test code.
    engine = sqlalchemy.create_engine(DB_URL)
    try:
        with Session(engine) as session:
            return session.execute(
                sqlalchemy.text("SELECT count(*) FROM connectionconfig WHERE key = :k"),
                {"k": KEY},
            ).scalar()
    finally:
        engine.dispose()
