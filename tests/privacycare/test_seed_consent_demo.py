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
import os
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
        # Fix round 1, Finding 1: the authorised Step 4 --commit run makes
        # the live database permanently satisfy every idempotency check in
        # _find_or_create_notice/_find_or_create_version -- every test
        # below that calls cli.seed_consent_demo(db) would otherwise
        # always take the "already exists" branch and never touch an
        # INSERT statement at all. Clearing these four tables here too
        # (same idiom as the rule table just above, and the one
        # test_consent_detector.py/test_api_consent.py already use) makes
        # the create path genuinely run inside this rolled-back
        # transaction; the real, permanently committed demo row is
        # untouched once teardown rolls back.
        session.execute(sqlalchemy.text("DELETE FROM privacypreferencehistory"))
        session.execute(sqlalchemy.text("DELETE FROM privacynoticehistory"))
        session.execute(sqlalchemy.text("DELETE FROM noticetranslation"))
        session.execute(sqlalchemy.text("DELETE FROM privacynotice"))
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


def _delete_live_demo_rows(notice_key: str) -> None:
    """A REAL, committed delete of the demo notice/translation/history/
    preference rows -- deliberately NOT inside a rolled-back session.

    test_dry_run_writes_nothing below runs the seed script as a genuinely
    separate process with its own database connection. An uncommitted
    DELETE on a different connection is invisible to it (Postgres MVCC
    isolation) -- nothing short of a real commit here can make that
    subprocess's create path anything other than dead code for the
    duration of the test. Always paired with _restore_live_demo_seed
    below, in a finally block, so the authorised Step 4 seed this task
    committed survives the test either way.
    """
    engine = sqlalchemy.create_engine(DB_URL)
    try:
        with engine.begin() as conn:
            conn.execute(
                sqlalchemy.text(
                    "DELETE FROM privacypreferencehistory "
                    "WHERE privacy_notice_history_id IN "
                    "(SELECT id FROM privacynoticehistory WHERE notice_key = :k)"
                ),
                {"k": notice_key},
            )
            conn.execute(
                sqlalchemy.text(
                    "DELETE FROM privacynoticehistory WHERE notice_key = :k"
                ),
                {"k": notice_key},
            )
            conn.execute(
                sqlalchemy.text(
                    "DELETE FROM noticetranslation WHERE privacy_notice_id IN "
                    "(SELECT id FROM privacynotice WHERE notice_key = :k)"
                ),
                {"k": notice_key},
            )
            conn.execute(
                sqlalchemy.text("DELETE FROM privacynotice WHERE notice_key = :k"),
                {"k": notice_key},
            )
    finally:
        engine.dispose()


def _restore_live_demo_seed() -> None:
    """Re-run the real CLI with --commit to put the authorised Step 4 demo
    row back. Asserts success rather than silently swallowing a failure --
    a restore that silently fails would leave the live database exactly in
    the inert state this whole plan exists to fix."""
    result = subprocess.run(
        [sys.executable, str(_SCRIPT_PATH), "--commit"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, (
        "failed to restore the authorised demo seed after "
        f"test_dry_run_writes_nothing: {result.stderr}"
    )
    assert "COMMITTED" in result.stdout


def test_dry_run_writes_nothing():
    # Fix round 1, Finding 1: after the authorised --commit run, every
    # idempotency check the script makes takes the "already exists" branch
    # against the live database forever -- this test would not fail even
    # if the --commit gate were deleted and replaced with an unconditional
    # db.commit(), because there was nothing left for either branch to
    # write. Confirmed by actually doing that locally: with the gate
    # replaced, this test failed (the deleted rows came back committed
    # instead of staying at zero); restored, it passes again. See the fix
    # report for the transcript.
    #
    # Fixed by genuinely deleting the demo rows from the live database (a
    # real, committed delete, not the rolled-back `db` fixture other tests
    # in this file use -- see _delete_live_demo_rows's own docstring for
    # why a rolled-back session can't work here) immediately before the
    # dry run, and genuinely restoring them in a finally block afterward.
    cli = _load_cli()
    _delete_live_demo_rows(cli.NOTICE_KEY)
    try:
        before = _counts_by_table()
        assert before["privacynotice"] == 0, (
            "setup failed to clear the demo notice -- the create path "
            "below would not be exercised"
        )

        result = subprocess.run(
            [sys.executable, str(_SCRIPT_PATH)],
            capture_output=True, text=True, check=True,
        )
        assert "DRY RUN — nothing written" in result.stdout

        after = _counts_by_table()
        # Still zero: the subprocess's own create path ran (there was
        # something to create -- the row was genuinely gone), then rolled
        # back rather than committing. If the --commit gate were broken to
        # commit unconditionally, `after` would show the notice/version/
        # preference rows created for real instead.
        assert after == before
    finally:
        _restore_live_demo_seed()

    restored = _counts_by_table()
    assert restored["privacynotice"] == 1
    assert restored["privacynoticehistory"] == 2
    assert restored["privacypreferencehistory"] == 1


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


def test_a_bad_database_url_exits_non_zero_with_no_traceback_and_no_credential():
    # Fix round 1, Finding 2: main() only ever caught ValueError, but
    # nothing in seed_consent_demo's call graph raises one -- a real
    # failure (the database unreachable, an IntegrityError) produced a raw
    # traceback, exactly what the brief forbids. PRIVACYCARE_DATABASE_URL
    # takes precedence over every other env var in _database_url(), so
    # pointing it at a syntactically valid but unreachable address (a port
    # nothing listens on) reaches the new SQLAlchemyError handler in
    # main() without needing a real bad password against the live server.
    #
    # The password embedded in this URL is a fake, distinctive string
    # chosen specifically so it is easy to prove absent from the output --
    # not a credential that works or that matters if it leaked, but if
    # THIS particular literal string appeared in stdout or stderr it could
    # only be because the script printed the raw connection string or an
    # exception's text that embedded it.
    fake_password = "CorrectHorseBatteryStaple9000"
    bad_url = f"postgresql://baduser:{fake_password}@127.0.0.1:1/fides"
    env = dict(os.environ)
    env["PRIVACYCARE_DATABASE_URL"] = bad_url

    result = subprocess.run(
        [sys.executable, str(_SCRIPT_PATH)],
        capture_output=True, text=True, env=env,
    )

    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "Traceback" not in combined
    assert fake_password not in combined
    assert bad_url not in combined
    # The target line is still expected -- it never carries the password
    # in the first place, so printing it is not the leak this test guards
    # against.
    assert "target: baduser@127.0.0.1:1/fides" in result.stdout


def test_a_notice_whose_english_translation_is_gone_gets_it_back(db):
    # Final review, minor: _FIND_TRANSLATION_SQL matches language = 'en'
    # only, and Ethyca's delete_notice_translations (called by
    # PrivacyNotice.update for every translation an update request omits)
    # will happily remove it. The lookup's None used to flow straight into
    # the inserts, which then produced orphaned translation_id-NULL history
    # rows needing a manual DB fix -- _FIND_VERSION_SQL can never match
    # them again, because `translation_id = NULL` is never true.
    cli = _load_cli()
    cli.seed_consent_demo(db)
    notice_id = db.execute(
        sqlalchemy.text("SELECT id FROM privacynotice WHERE notice_key = :k"),
        {"k": cli.NOTICE_KEY},
    ).scalar()

    # The FK is ondelete="SET NULL", so this is exactly what Ethyca itself
    # does to the history rows when a translation is deleted.
    db.execute(
        sqlalchemy.text(
            "DELETE FROM noticetranslation WHERE privacy_notice_id = :nid"
        ),
        {"nid": notice_id},
    )
    orphaned = db.execute(
        sqlalchemy.text(
            "SELECT count(*) FROM privacynoticehistory "
            "WHERE notice_key = :k AND translation_id IS NULL"
        ),
        {"k": cli.NOTICE_KEY},
    ).scalar()
    assert orphaned == 2, "setup: Ethyca's SET NULL should have orphaned both versions"

    cli.seed_consent_demo(db)

    translations = db.execute(
        sqlalchemy.text(
            "SELECT count(*) FROM noticetranslation WHERE privacy_notice_id = :nid"
        ),
        {"nid": notice_id},
    ).scalar()
    assert translations == 1, "the English translation was not recreated"

    still_orphaned = db.execute(
        sqlalchemy.text(
            "SELECT count(*) FROM privacynoticehistory "
            "WHERE notice_key = :k AND translation_id IS NULL"
        ),
        {"k": cli.NOTICE_KEY},
    ).scalar()
    # Only the two Ethyca's own SET NULL orphaned. The rerun added NONE of
    # its own -- before the fix it inserted two more, unreachable forever.
    assert still_orphaned == 2

    # And the seed is coherent again: the detector finds the demo subject.
    result = find_stale_consents(db, notice_key=cli.NOTICE_KEY)
    assert [item.subject for item in result] == [cli.DEMO_SUBJECT_EMAIL]
