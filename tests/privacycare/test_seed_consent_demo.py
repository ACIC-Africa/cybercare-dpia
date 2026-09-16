"""The demo notice that gives the stale-consent detector something true to
find.

Task 4, plan 16. The consent tables hold zero rows in the live database —
without this seed, `find_stale_consents` (fides.api.privacycare.consent.
detector) has nothing to find and this plan repeats the workstream's
recurring failure: a capability that exists in the schema and is inert in
the deployment.

This is a DEMONSTRATION notice, not a real one: keyed
`privacycare_demo_fuel_card_marketing`, named so nobody could mistake it for
real customer content. It is not, and must never become, Josephine's real
consent notices — those are hers and Carol's to author (see the script's own
module docstring).
"""
import importlib.util
import pathlib
import subprocess
import sys

import pytest
import sqlalchemy
from sqlalchemy.orm import Session

from fides.api.privacycare.consent.detector import find_stale_consents

DB_URL = "postgresql://postgres:fides@127.0.0.1:5442/fides"

_SCRIPT_PATH = (
    pathlib.Path(__file__).resolve().parents[2]
    / "scripts/privacycare/seed_consent_demo.py"
)

_CONSENT_TABLES = [
    "privacynotice",
    "noticetranslation",
    "privacynoticehistory",
    "privacypreferencehistory",
    "privacycare_consent_rule",
]


def _load_cli():
    spec = importlib.util.spec_from_file_location(
        "privacycare_seed_consent_demo", _SCRIPT_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def db(monkeypatch):
    engine = sqlalchemy.create_engine(DB_URL)
    with Session(engine) as session:
        monkeypatch.setattr(session, "commit", session.flush)
        # A clean slate for the rule table specifically: this suite proves
        # seed_consent_demo() seeds it FIRST (active_rule raises on an
        # empty table), which needs the table to genuinely start empty
        # inside this rolled-back transaction, regardless of whatever the
        # live table already holds.
        session.execute(sqlalchemy.text("DELETE FROM privacycare_consent_rule"))
        yield session
        session.rollback()


def _counts_by_table() -> dict:
    engine = sqlalchemy.create_engine(DB_URL)
    try:
        with Session(engine) as session:
            return {
                table: session.execute(
                    sqlalchemy.text(f"SELECT count(*) FROM {table}")
                ).scalar()
                for table in _CONSENT_TABLES
            }
    finally:
        engine.dispose()


def test_dry_run_writes_nothing():
    before = _counts_by_table()
    result = subprocess.run(
        [sys.executable, str(_SCRIPT_PATH)],
        capture_output=True, text=True, check=True,
    )
    assert "DRY RUN — nothing written" in result.stdout
    assert _counts_by_table() == before


def test_the_run_names_its_target_and_never_the_password():
    result = subprocess.run(
        [sys.executable, str(_SCRIPT_PATH)],
        capture_output=True, text=True, check=True,
    )
    out = result.stdout
    assert "target:" in out
    target_line = next(line for line in out.splitlines() if line.startswith("target:"))
    assert target_line.endswith("/fides"), "target line should end with the database name"
    # NOT test_seed_connection.py's count("fides") == 1 discriminator: this
    # script's own imports (fides.api.db.base, PrivacyPreferenceHistory,
    # the materiality module — all mandated by the brief, all needed for
    # the ORM-encryption and rule-table work below) pull in a heavier chain
    # than seed_connection.py's does, and *that* chain emits its own
    # UserWarning/DeprecationWarning noise on stdout at import time whose
    # message text embeds this repo's own path (".../src/fides/...") —
    # "fides" the package/directory name, nothing to do with the
    # credential. That collides with a same-string password ("fides", the
    # local dev default) hard enough that a raw count is not a reliable
    # signal here. The two checks below are the actually discriminating
    # ones: no plain log line ever coincidentally contains a credential-
    # bearing "user:password@" or a raw "postgresql://" URL, so their
    # absence is what "the password never leaked" really means.
    assert "postgres:fides@" not in out
    assert "postgresql://" not in out


def test_seed_consent_demo_seeds_the_rule_row_first(db):
    # find_stale_consents dies on its first call (active_rule raises) if
    # this hasn't happened -- the exact failure mode the brief calls out.
    before = db.execute(
        sqlalchemy.text("SELECT count(*) FROM privacycare_consent_rule")
    ).scalar()
    assert before == 0

    cli = _load_cli()
    cli.seed_consent_demo(db)

    after = db.execute(
        sqlalchemy.text("SELECT count(*) FROM privacycare_consent_rule")
    ).scalar()
    assert after == 1


def test_seeding_twice_does_not_duplicate(db):
    cli = _load_cli()
    cli.seed_consent_demo(db)
    cli.seed_consent_demo(db)

    notice_count = db.execute(
        sqlalchemy.text("SELECT count(*) FROM privacynotice WHERE notice_key = :k"),
        {"k": cli.NOTICE_KEY},
    ).scalar()
    assert notice_count == 1, "a second run created a second notice"

    version_count = db.execute(
        sqlalchemy.text(
            "SELECT count(*) FROM privacynoticehistory WHERE notice_key = :k"
        ),
        {"k": cli.NOTICE_KEY},
    ).scalar()
    assert version_count == 2, "a second run created extra history versions"

    preference_count = db.execute(
        sqlalchemy.text(
            "SELECT count(*) FROM privacypreferencehistory "
            "WHERE privacy_notice_history_id IN "
            "(SELECT id FROM privacynoticehistory WHERE notice_key = :k)"
        ),
        {"k": cli.NOTICE_KEY},
    ).scalar()
    assert preference_count == 1, "a second run created a second preference"

    rule_count = db.execute(
        sqlalchemy.text("SELECT count(*) FROM privacycare_consent_rule")
    ).scalar()
    assert rule_count == 1, "a second run created a second rule row"


def test_the_seeded_notice_has_exactly_two_versions_differing_by_the_gained_use(db):
    cli = _load_cli()
    cli.seed_consent_demo(db)

    rows = db.execute(
        sqlalchemy.text(
            "SELECT version, data_uses FROM privacynoticehistory "
            "WHERE notice_key = :k ORDER BY version"
        ),
        {"k": cli.NOTICE_KEY},
    ).fetchall()

    assert len(rows) == 2
    v1, v2 = rows
    assert v1.version == 1.0
    assert v2.version == 2.0
    assert set(v1.data_uses) == {"marketing.advertising"}
    assert set(v2.data_uses) == {
        "marketing.advertising",
        "marketing.advertising.third_party",
    }
    # Only gained a use -- nothing lost between v1 and v2.
    assert set(v1.data_uses) - set(v2.data_uses) == set()


def test_the_detector_finds_exactly_one_stale_consent_against_the_seed(db):
    cli = _load_cli()
    cli.seed_consent_demo(db)

    result = find_stale_consents(db, notice_key=cli.NOTICE_KEY)

    assert len(result) == 1
    stale = result[0]
    assert stale.notice_key == cli.NOTICE_KEY
    assert stale.subject == cli.DEMO_SUBJECT_EMAIL
    assert stale.subject_kind == "email"
    assert stale.preference == "opt_in"
    assert stale.consented_version == 1.0
    assert stale.live_version == 2.0
    assert stale.added_uses == ["marketing.advertising.third_party"]
