"""The one local connection a monitor can be demonstrated against.

D-DM-5: it points at our own fides-db, never a customer system. Nothing here
scans; the connection exists so the configuration screen has something real to
bind to.
"""
import importlib.util
import pathlib
import subprocess
import sys

import pytest
import sqlalchemy
from sqlalchemy.orm import Session

DB_URL = "postgresql://postgres:fides@127.0.0.1:5442/fides"
KEY = "privacycare_scratch_local_postgres"


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
        [sys.executable, "scripts/privacycare/seed_connection.py"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert "DRY RUN — nothing written" in out
    assert _count() == before


def test_the_run_names_its_target_and_never_the_password():
    out = subprocess.run(
        [sys.executable, "scripts/privacycare/seed_connection.py"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert "target:" in out
    assert "fides" in out            # the database name
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


def _delete_existing(db):
    # The live database already carries the authorised --commit'd row (this
    # is the ONE intended permanent write, per the brief). seed_connection()
    # only touches `secrets` on the INSERT path — a row that already exists
    # short-circuits before secrets are set — so a secrets-behaviour test
    # needs a clean slate to actually exercise that path. Deleting here is
    # safe: it happens inside the same `db` fixture's transaction, which
    # test teardown rolls back, so the live row is untouched once the test
    # ends (verified: `db` fixture patches commit to a no-op and always
    # rolls back).
    db.execute(sqlalchemy.text("DELETE FROM connectionconfig WHERE key = :k"), {"k": KEY})
    db.flush()


def test_the_seeded_secrets_describe_the_container_view_not_the_host_view(db):
    # Fix round 1, Finding 1: secrets.host/port are how the LIVE `fides` API
    # CONTAINER reaches fides-db (Docker-internal: fides-db:5432), NOT how
    # this script itself — running on the host, via _database_url() and
    # FIDES__DATABASE__SERVER/PORT — reaches it (127.0.0.1:5442). Reading
    # `secrets` back needs the ORM (it's an encrypted column; raw SQL would
    # return ciphertext), same as production code reads it.
    from fides.api.models.connectionconfig import ConnectionConfig

    _delete_existing(db)
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

    _delete_existing(db)
    cli = _load_cli()
    cli.seed_connection(db)
    connection = (
        db.query(ConnectionConfig).filter(ConnectionConfig.key == KEY).one()
    )
    assert connection.secrets["host"] == "some-other-host"
    assert connection.secrets["port"] == 6543


def _count() -> int:
    engine = sqlalchemy.create_engine(DB_URL)
    with Session(engine) as session:
        return session.execute(
            sqlalchemy.text("SELECT count(*) FROM connectionconfig WHERE key = :k"),
            {"k": KEY},
        ).scalar()
