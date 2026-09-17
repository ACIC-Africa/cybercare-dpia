"""Carol's six screening triggers (spec 2026-09-16 D-W2-7, plan 18 Task 1).

Before a DPIA is generated, someone answers whether the activity needs one
at all. The six questions that screening answers are Carol's vocabulary,
not engineering's — this seed's only job is to reproduce her wording,
byte-for-byte, into privacycare_screening_trigger. Two of the six
(special_category, vulnerable_subjects) were recast on 2026-09-17 from a
university sample case into this customer's world; the other four are
unchanged from her prototype.

The highest-stakes assertions here are the exact-text ones: a truncation,
a smart-quote swap, or a well-meaning tidy of Carol's phrasing is exactly
the silent rewrite the brief forbids, and only a full-string comparison
(never a substring or a "contains") would catch it.

Same shape as test_seed_kenya_template.py: the CLI is exercised as a
subprocess for the dry-run/target/bad-url behaviour, and its seeding
function is called directly against a rolled-back session for content
assertions.
"""
import importlib.util
import os
import pathlib
import subprocess
import sys

import pytest
import sqlalchemy
from sqlalchemy.orm import Session

DB_URL = "postgresql://postgres:fides@127.0.0.1:5442/fides"

_SCRIPT_PATH = (
    pathlib.Path(__file__).resolve().parents[2]
    / "scripts/privacycare/seed_screening_triggers.py"
)

# The five KCA University terms no seeded row may ever carry (repo-wide
# rule; see the plan's Global Constraints).
_KCA_TERMS = ("HOFU", "Champion", "student", "exam", "LMS")

# Carol's six triggers, as ruled 2026-09-17 — transcribed here identically
# to how the seed script itself must transcribe them, so this test can
# assert full-string equality rather than a substring check. This tuple is
# NOT imported from the CLI under test: the whole point of this test is to
# catch a divergence between what the script seeds and what Carol actually
# said, so the expected text has to be typed out independently rather than
# read back from the module being verified.
CAROLS_TRIGGERS = (
    (
        "large_scale",
        "Large-scale processing",
        "High volume of data subjects, records, or geographic spread.",
    ),
    (
        "special_category",
        "Special category or highly sensitive data",
        "Any of the special categories under the Data Protection Act 2019 "
        "§2 — including health and HIV status, biometric and genetic "
        "data, conscience, belief, and well-being.",
    ),
    (
        "systematic_monitoring",
        "Systematic monitoring",
        "Ongoing observation, tracking, or profiling of individuals.",
    ),
    (
        "new_technology",
        "New or unproven technology",
        "AI/ML, biometric matching, or a system not previously deployed "
        "at this scale.",
    ),
    (
        "automated_decision",
        "Automated decision-making with legal or similarly significant "
        "effect",
        "Decisions made about a person with no meaningful human review.",
    ),
    (
        "vulnerable_subjects",
        "Processing involving vulnerable data subjects",
        "Minors, dependants, job applicants, individuals captured on "
        "CCTV, witnesses, complainants, suspected offenders, or anyone "
        "in a relationship of economic dependence on the company.",
    ),
)


def _load_cli():
    spec = importlib.util.spec_from_file_location(
        "privacycare_seed_screening_triggers", _SCRIPT_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def db(monkeypatch):
    engine = sqlalchemy.create_engine(DB_URL)
    with Session(engine) as session:
        # NEVER a no-op: base_class.persist_obj does add/commit/refresh, and
        # a no-op commit starves refresh(). This script is raw SQL
        # throughout so nothing here calls persist_obj directly, but the
        # fixture still follows the same idiom every other file in this
        # package uses.
        monkeypatch.setattr(session, "commit", session.flush)
        yield session
        session.rollback()


def _trigger_count(db_session) -> int:
    return db_session.execute(
        sqlalchemy.text("SELECT count(*) FROM privacycare_screening_trigger")
    ).scalar()


# Task 5 (spec 2026-09-17, plan 18): the authorized `--commit` seed has now
# run against this same live database, so every test below that calls
# cli.seed_screening_triggers(db) unmodified no longer exercises the INSERT
# at all — ON CONFLICT (trigger_key) DO NOTHING fires against the six
# already-committed rows before the test's own transaction ever begins, so
# `db.execute(_SEED_SQL, ...)` always returns rowcount 0 and every SELECT
# below just reads back Task 5's committed rows regardless of what
# SCREENING_TRIGGERS currently holds. Verified directly: monkeypatching
# SCREENING_TRIGGERS to a deliberately wrong description and calling
# seed_screening_triggers(db) still returns written=0 and reads back the
# real (uncorrupted) committed text — proof the create path never runs
# inside these tests any more.
#
# Same fix as test_seed_kenya_template.py's IMPORTANT-2 disarmament: patch
# the constant that ON CONFLICT keys off (KENYA_ASSESSMENT_TYPE there,
# SCREENING_TRIGGERS' own trigger_key here) to unused values so the INSERT
# actually executes inside this test's own rolled-back transaction, instead
# of short-circuiting on the row Task 5 already committed. Only trigger_key
# is suffixed — label and description are left exactly as Carol wrote them,
# copied from CAROLS_TRIGGERS (typed independently above, not read from the
# module under test), so a typo or a smart-quote swap in the module's own
# prose still fails these tests exactly as before.
_TEST_KEY_SUFFIX = "__test_content_copy"


def _patched_triggers(cli):
    return tuple(
        (key + _TEST_KEY_SUFFIX, label, description)
        for key, label, description in cli.SCREENING_TRIGGERS
    )


# --- The CLI, as a subprocess -----------------------------------------------


def test_dry_run_writes_nothing():
    engine = sqlalchemy.create_engine(DB_URL)
    try:
        with Session(engine) as session:
            before = _trigger_count(session)
    finally:
        engine.dispose()

    result = subprocess.run(
        [sys.executable, str(_SCRIPT_PATH)],
        capture_output=True, text=True, check=True,
    )

    assert "DRY RUN — nothing written" in result.stdout

    engine = sqlalchemy.create_engine(DB_URL)
    try:
        with Session(engine) as session:
            after = _trigger_count(session)
    finally:
        engine.dispose()

    assert after == before


def test_the_run_names_its_target_and_never_the_password():
    result = subprocess.run(
        [sys.executable, str(_SCRIPT_PATH)],
        capture_output=True, text=True, check=True,
    )
    out = result.stdout
    assert "target:" in out
    target_line = next(
        line for line in out.splitlines() if line.startswith("target:")
    )
    assert target_line.endswith("/fides")
    assert "postgres:fides@" not in out
    assert "postgresql://" not in out


def test_a_bad_database_url_exits_non_zero_with_no_traceback_and_no_credential():
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
    assert "target: baduser@127.0.0.1:1/fides" in result.stdout


# --- seed_screening_triggers(db), against the rolled-back fixture ----------


def test_seeding_twice_does_not_duplicate(db, monkeypatch):
    cli = _load_cli()
    monkeypatch.setattr(cli, "SCREENING_TRIGGERS", _patched_triggers(cli))
    cli.seed_screening_triggers(db)
    written_second_time = cli.seed_screening_triggers(db)

    assert written_second_time == 0
    assert _trigger_count(db) == 12, (
        "6 real (Task 5's committed rows) + 6 test-suffixed rows this test "
        "itself inserted"
    )


def test_exactly_six_triggers_are_seeded(db, monkeypatch):
    cli = _load_cli()
    monkeypatch.setattr(cli, "SCREENING_TRIGGERS", _patched_triggers(cli))
    written = cli.seed_screening_triggers(db)
    assert written == 6, "the create path must actually run inside this test"


def test_each_trigger_matches_carols_text_exactly(db, monkeypatch):
    cli = _load_cli()
    monkeypatch.setattr(cli, "SCREENING_TRIGGERS", _patched_triggers(cli))
    written = cli.seed_screening_triggers(db)
    assert written == 6, "the create path must actually run inside this test"

    rows = db.execute(
        sqlalchemy.text(
            "SELECT trigger_key, label, description FROM "
            "privacycare_screening_trigger WHERE trigger_key LIKE :pattern "
            "ORDER BY display_order"
        ),
        {"pattern": f"%{_TEST_KEY_SUFFIX}"},
    ).all()

    assert len(rows) == len(CAROLS_TRIGGERS)
    for row, (expected_key, expected_label, expected_description) in zip(
        rows, CAROLS_TRIGGERS
    ):
        # Full-string equality, not a substring check: a truncation or a
        # smart-quote swap must fail this test. trigger_key carries the
        # test's own suffix (see _patched_triggers) so the INSERT actually
        # runs instead of hitting Task 5's already-committed row; label and
        # description are asserted against Carol's real text, untouched.
        assert row.trigger_key == expected_key + _TEST_KEY_SUFFIX
        assert row.label == expected_label
        assert row.description == expected_description


def test_display_order_is_one_through_six_with_no_gaps(db, monkeypatch):
    cli = _load_cli()
    monkeypatch.setattr(cli, "SCREENING_TRIGGERS", _patched_triggers(cli))
    written = cli.seed_screening_triggers(db)
    assert written == 6, "the create path must actually run inside this test"

    orders = [
        row[0]
        for row in db.execute(
            sqlalchemy.text(
                "SELECT display_order FROM privacycare_screening_trigger "
                "WHERE trigger_key LIKE :pattern ORDER BY display_order"
            ),
            {"pattern": f"%{_TEST_KEY_SUFFIX}"},
        ).all()
    ]
    assert orders == [1, 2, 3, 4, 5, 6]


def test_special_category_trigger_names_dpa_2019_section_2_not_article_9(
    db, monkeypatch
):
    cli = _load_cli()
    monkeypatch.setattr(cli, "SCREENING_TRIGGERS", _patched_triggers(cli))
    written = cli.seed_screening_triggers(db)
    assert written == 6, "the create path must actually run inside this test"

    description = db.execute(
        sqlalchemy.text(
            "SELECT description FROM privacycare_screening_trigger "
            "WHERE trigger_key = :key"
        ),
        {"key": "special_category" + _TEST_KEY_SUFFIX},
    ).scalar()
    assert description is not None
    assert "Data Protection Act 2019" in description
    assert "§2" in description
    assert "Article 9" not in description


def test_vulnerable_subjects_trigger_does_not_mention_student(db, monkeypatch):
    cli = _load_cli()
    monkeypatch.setattr(cli, "SCREENING_TRIGGERS", _patched_triggers(cli))
    written = cli.seed_screening_triggers(db)
    assert written == 6, "the create path must actually run inside this test"

    row = db.execute(
        sqlalchemy.text(
            "SELECT label, description FROM privacycare_screening_trigger "
            "WHERE trigger_key = :key"
        ),
        {"key": "vulnerable_subjects" + _TEST_KEY_SUFFIX},
    ).mappings().first()
    assert row is not None
    haystack = f"{row['label']} {row['description']}".lower()
    assert "student" not in haystack


def test_no_kca_university_term_appears_in_any_seeded_row(db, monkeypatch):
    cli = _load_cli()
    monkeypatch.setattr(cli, "SCREENING_TRIGGERS", _patched_triggers(cli))
    written = cli.seed_screening_triggers(db)
    assert written == 6, "the create path must actually run inside this test"

    rows = db.execute(
        sqlalchemy.text(
            "SELECT trigger_key, label, description FROM "
            "privacycare_screening_trigger WHERE trigger_key LIKE :pattern"
        ),
        {"pattern": f"%{_TEST_KEY_SUFFIX}"},
    ).mappings().all()

    haystack = " ".join(
        f"{row['trigger_key']} {row['label']} {row['description']}"
        for row in rows
    ).lower()
    for term in _KCA_TERMS:
        assert term.lower() not in haystack, f"{term!r} found in a seeded row"
