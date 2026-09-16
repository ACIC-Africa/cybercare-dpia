"""The Kenya DPA 2019 DPIA template seed (spec 2026-09-16 D-W2-2, Task 5).

The Kenyan taxonomy has no DPIA template of its own — assessment_template
holds 13 rows today (see seed_kenya_template.py's own GDPR_TEMPLATE_ID
comment for the full list, and why two of them are easy to confuse), none
region='Kenya'. Without this seed, api/risk.py's register has DPIAs to
score but no Kenyan questionnaire to score them against, the same "exists
in the schema, inert in the deployment" gap every other PrivacyCare seed
script in this package exists to close.

parent_template_id inherits nothing — it is a self-referential FK with a
relationship (AssessmentTemplate.parent_template) and no code anywhere
reads it (confirmed: `grep -rn "parent_template_id\\|parent_template"
src/ --include=*.py` finds only its own Column/relationship declaration in
models.py). So this seed COPIES the 24 questions from the GDPR DPIA
template (ast_b5b6e569-0b77-439b-b1b8-8f9d77506f40) unedited, and sets
parent_template_id purely as provenance, not inheritance.

Same shape as scripts/privacycare/seed_dsr.py and seed_consent_demo.py:
one argparse CLI, one session, dry-run by default, --commit to persist.
_database_url() and _target_description() are copied verbatim from those
scripts rather than imported — see load_taxonomy.py's module docstring for
why importing env.py isn't safe here.

Unlike test_seed_consent_demo.py, this suite never commits to the live
database (no "authorized Step 4 run" happened for this task — see the
Task 5 report), so there is no live demo row to protect and no
delete-then-restore dance: `test_dry_run_writes_nothing` only needs to
confirm the live database does not already carry a Kenya template before
it runs, the same "setup: ..." guard idea, simplified because there is
nothing to put back.
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
    / "scripts/privacycare/seed_kenya_template.py"
)

# The five KCA University terms this seed must never carry — see the
# Task 5 brief.
_KCA_TERMS = ("HOFU", "Champion", "student", "exam", "LMS")


def _load_cli():
    spec = importlib.util.spec_from_file_location(
        "privacycare_seed_kenya_template", _SCRIPT_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def db(monkeypatch):
    engine = sqlalchemy.create_engine(DB_URL)
    with Session(engine) as session:
        # NEVER a no-op: base_class.persist_obj does add/commit/refresh, and
        # a no-op commit starves refresh(). flush() gives write visibility
        # within the transaction without making it durable; rollback() on
        # teardown discards everything this test wrote. (This script itself
        # never calls an ORM .create() — it is raw SQL throughout, per the
        # brief's "prefer raw SQL through the session" instruction — but the
        # fixture still follows the same idiom every other file in this
        # package uses.)
        monkeypatch.setattr(session, "commit", session.flush)
        yield session
        session.rollback()


def _kenya_template_count() -> int:
    cli = _load_cli()
    engine = sqlalchemy.create_engine(DB_URL)
    try:
        with Session(engine) as session:
            return session.execute(
                sqlalchemy.text(
                    "SELECT count(*) FROM assessment_template WHERE assessment_type = :t"
                ),
                {"t": cli.KENYA_ASSESSMENT_TYPE},
            ).scalar()
    finally:
        engine.dispose()


# --- The CLI, as a subprocess -----------------------------------------------


def test_dry_run_writes_nothing():
    before = _kenya_template_count()
    assert before == 0, (
        "a Kenya template already exists in the live database — this test "
        "cannot prove a dry run writes nothing against a pre-populated row"
    )

    result = subprocess.run(
        [sys.executable, str(_SCRIPT_PATH)],
        capture_output=True, text=True, check=True,
    )

    assert "DRY RUN — nothing written" in result.stdout
    assert _kenya_template_count() == before == 0


def test_the_run_names_its_target_and_never_the_password():
    result = subprocess.run(
        [sys.executable, str(_SCRIPT_PATH)],
        capture_output=True, text=True, check=True,
    )
    out = result.stdout
    assert "target:" in out
    target_line = next(line for line in out.splitlines() if line.startswith("target:"))
    assert target_line.endswith("/fides")
    assert "postgres:fides@" not in out
    assert "postgresql://" not in out


def test_a_bad_database_url_exits_non_zero_with_no_traceback_and_no_credential():
    # Same probe as test_seed_consent_demo.py's identically-named test:
    # PRIVACYCARE_DATABASE_URL takes precedence in _database_url(), so a
    # syntactically valid but unreachable address reaches the
    # SQLAlchemyError handler without needing a real bad password against
    # the live server.
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


# --- seed_kenya_template(db), against the rolled-back fixture ---------------


def test_seeding_twice_does_not_duplicate(db):
    cli = _load_cli()
    cli.seed_kenya_template(db)
    cli.seed_kenya_template(db)

    template_count = db.execute(
        sqlalchemy.text(
            "SELECT count(*) FROM assessment_template WHERE assessment_type = :t"
        ),
        {"t": cli.KENYA_ASSESSMENT_TYPE},
    ).scalar()
    assert template_count == 1, "a second run created a second template"

    template_id = db.execute(
        sqlalchemy.text(
            "SELECT id FROM assessment_template WHERE assessment_type = :t"
        ),
        {"t": cli.KENYA_ASSESSMENT_TYPE},
    ).scalar()
    question_count = db.execute(
        sqlalchemy.text(
            "SELECT count(*) FROM assessment_question WHERE template_id = :tid"
        ),
        {"tid": template_id},
    ).scalar()
    assert question_count == 24, "a second run duplicated the copied questions"


def test_the_kenya_template_ends_with_exactly_24_questions_identical_to_the_gdpr_ones(db):
    cli = _load_cli()
    cli.seed_kenya_template(db)

    template_id = db.execute(
        sqlalchemy.text(
            "SELECT id FROM assessment_template WHERE assessment_type = :t"
        ),
        {"t": cli.KENYA_ASSESSMENT_TYPE},
    ).scalar()

    kenya_questions = db.execute(
        sqlalchemy.text(
            "SELECT question_key, question_text FROM assessment_question "
            "WHERE template_id = :tid ORDER BY group_order, question_order"
        ),
        {"tid": template_id},
    ).all()
    assert len(kenya_questions) == 24

    gdpr_questions = db.execute(
        sqlalchemy.text(
            "SELECT question_key, question_text FROM assessment_question "
            "WHERE template_id = :tid ORDER BY group_order, question_order"
        ),
        {"tid": cli.GDPR_TEMPLATE_ID},
    ).all()
    assert len(gdpr_questions) == 24

    # Copied unedited: Carol adapts the text afterwards (OQ-W2-2), so this
    # seed's own job is to reproduce the GDPR text byte-for-byte, not to
    # improve on it.
    assert [q.question_text for q in kenya_questions] == [
        q.question_text for q in gdpr_questions
    ]
    assert [q.question_key for q in kenya_questions] == [
        q.question_key for q in gdpr_questions
    ]


def test_parent_template_id_points_at_the_gdpr_template(db):
    cli = _load_cli()
    cli.seed_kenya_template(db)

    parent_id = db.execute(
        sqlalchemy.text(
            "SELECT parent_template_id FROM assessment_template WHERE assessment_type = :t"
        ),
        {"t": cli.KENYA_ASSESSMENT_TYPE},
    ).scalar()
    assert parent_id == cli.GDPR_TEMPLATE_ID


def test_the_template_names_kenya_odpc_and_the_dpa_2019(db):
    cli = _load_cli()
    cli.seed_kenya_template(db)

    row = db.execute(
        sqlalchemy.text(
            "SELECT region, authority, legal_reference FROM assessment_template "
            "WHERE assessment_type = :t"
        ),
        {"t": cli.KENYA_ASSESSMENT_TYPE},
    ).mappings().first()
    assert row is not None
    assert row["region"] == "Kenya"
    assert row["authority"] == "ODPC"
    assert "31" in row["legal_reference"]
    assert "2019" in row["legal_reference"]
    assert "2021" in row["legal_reference"]


def test_no_kca_university_term_appears_anywhere_in_the_seeded_rows(db):
    cli = _load_cli()
    cli.seed_kenya_template(db)

    template_id = db.execute(
        sqlalchemy.text(
            "SELECT id FROM assessment_template WHERE assessment_type = :t"
        ),
        {"t": cli.KENYA_ASSESSMENT_TYPE},
    ).scalar()

    template_text = " ".join(
        str(v)
        for v in db.execute(
            sqlalchemy.text(
                "SELECT name, version, region, authority, legal_reference, "
                "description FROM assessment_template WHERE id = :id"
            ),
            {"id": template_id},
        ).mappings().first().values()
        if v is not None
    )
    question_rows = db.execute(
        sqlalchemy.text(
            "SELECT requirement_key, requirement_title, question_key, "
            "question_text, guidance FROM assessment_question "
            "WHERE template_id = :tid"
        ),
        {"tid": template_id},
    ).mappings().all()
    question_text = " ".join(
        str(v) for row in question_rows for v in row.values() if v is not None
    )

    haystack = (template_text + " " + question_text).lower()
    for term in _KCA_TERMS:
        assert term.lower() not in haystack, f"{term!r} found in the seeded rows"


def test_seed_kenya_template_raises_if_the_gdpr_template_is_missing(db, monkeypatch):
    # Defensive: if the GDPR template this seed copies from were ever
    # deleted or renamed, silently seeding an empty (or wrong) Kenya
    # template would be worse than refusing outright.
    #
    # Not a real DELETE of the live GDPR row: the live database's GDPR
    # template is referenced by privacy_assessment rows committed by real
    # usage (privacy_assessment.template_id has no ON DELETE), so deleting
    # it aborts the whole transaction on a ForeignKeyViolation — confirmed
    # by trying exactly that first. Monkeypatching the CLI's own
    # GDPR_TEMPLATE_ID constant to an id that names no row proves the same
    # "cannot find the template to copy from" path without touching a row
    # something else in the live database depends on.
    cli = _load_cli()
    fake_id = "ast_00000000-0000-0000-0000-000000000000"
    monkeypatch.setattr(cli, "GDPR_TEMPLATE_ID", fake_id)

    with pytest.raises(ValueError, match=fake_id):
        cli.seed_kenya_template(db)
