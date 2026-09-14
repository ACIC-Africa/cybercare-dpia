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


def _count() -> int:
    engine = sqlalchemy.create_engine(DB_URL)
    with Session(engine) as session:
        return session.execute(
            sqlalchemy.text("SELECT count(*) FROM connectionconfig WHERE key = :k"),
            {"k": KEY},
        ).scalar()
