"""scripts/privacycare/seed_demo.py — the PrivacyCare DPIA-spine demo seed
(spec 2026-09-18, "PrivacyCare demo seed — design").

This script is unlike every other PrivacyCare seed script in this package:
it writes 38 screening decisions and 23 data mappings onto a LIVE table
that ALSO holds the customer's own 87 real business processes and, per
plan 20 Task 5, 2 pre-existing demonstration screening decisions / 4
processing activities / 3 systems / 5 links the demo-rows inventory
(docs/demo/privacycare-demo-rows.md) documents and asks not to be
disturbed. So this test file's highest-stakes assertions are not "does it
write the right number of rows" (though several tests below do check
that) but "does it leave everything it must not touch exactly alone" —
tested by name, against known ids, not by count arithmetic alone.

DB-STATE-INDEPENDENT BY THE SAME DISCIPLINE test_seed_kenya_template.py's
and test_seed_screening_triggers.py's own module comments describe: by
the time this task is done, the authorised `--commit` run WILL have
happened against this same live database (the brief's own last step:
"leave the database seeded... run --commit once at the end"), so any test
here that assumed a from-empty register would be wrong the moment that
commit lands. Every test below is written to hold BOTH before and after
that commit — mostly by asserting `written + skipped == N` rather than a
bare `written == N`, and by using the real, committed marker
(DEMO_FEATURE_MARKER / DECIDED_BY_MARKER) as the thing under test rather
than a synthetic substitute, since a synthetic substitute could not prove
the one thing that actually matters here: that --remove finds ONLY rows
this script created.

Same fixture shape as test_seed_screening_triggers.py: `db` monkeypatches
session.commit to session.flush and always rolls back at the end, so
nothing any test below does is ever permanent — including calls to
seed_demo() and remove_demo() themselves, both of which are pure raw SQL
with no ORM persist_obj call to trip (see seed_demo.py's own module
docstring, "NO ORM WRITES, SO NO COMMIT TRAP").
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
    / "scripts/privacycare/seed_demo.py"
)

# Known, permanent ids from plan 20 Task 5 (docs/demo/privacycare-demo-rows.md)
# — carry NEITHER marker this script uses, and must survive every test
# below untouched.
_PRIOR_ART_DECISION_IDS = (
    "a5a042cc-8482-4e9e-82c0-de8b474b5553",  # Fuel Card Issuance
    "0358bdb5-1fe1-4c48-aacc-883131bfde6e",  # CSR Planning & Execution
)
_PRIOR_ART_DECLARATION_IDS = (
    "pri_7b31daa0-0ed3-41d2-8ff0-1ad0afd16b11",
    "pri_e44c17f7-3345-4e98-8c48-d81b67f97fb3",
    "pri_dc8264d2-49b3-4064-acf0-34e67a9454a0",
    "pri_94fd798c-18c8-4705-a760-1196a9123a4d",
)
_PRIOR_ART_SYSTEM_IDS = (
    "sys_4481f3505f6b",
    "sys_d7ddf76d0c8f",
    "ctl_ef9cadb3-828b-4838-a0a2-ea7c68dfed07",
)


def _load_cli():
    spec = importlib.util.spec_from_file_location(
        "privacycare_seed_demo", _SCRIPT_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def db(monkeypatch):
    engine = sqlalchemy.create_engine(DB_URL)
    with Session(engine) as session:
        # NEVER a no-op: base_class.persist_obj does add/commit/refresh —
        # but nothing in seed_demo.py calls it (all raw SQL). The fixture
        # still follows the same idiom every other file in this package
        # uses, in case a future edit adds an ORM write.
        monkeypatch.setattr(session, "commit", session.flush)
        yield session
        session.rollback()


# --- The CLI, as a subprocess ------------------------------------------


def test_dry_run_writes_nothing():
    engine = sqlalchemy.create_engine(DB_URL)
    try:
        with Session(engine) as session:
            before = session.execute(
                sqlalchemy.text("SELECT count(*) FROM privacycare_screening_decision")
            ).scalar()
            before_mappings = session.execute(
                sqlalchemy.text("SELECT count(*) FROM privacydeclaration")
            ).scalar()
    finally:
        engine.dispose()

    result = subprocess.run(
        [sys.executable, str(_SCRIPT_PATH)],
        capture_output=True, text=True, check=True,
    )
    assert "DRY RUN — nothing written" in result.stdout
    assert "mode: SEED" in result.stdout

    engine = sqlalchemy.create_engine(DB_URL)
    try:
        with Session(engine) as session:
            after = session.execute(
                sqlalchemy.text("SELECT count(*) FROM privacycare_screening_decision")
            ).scalar()
            after_mappings = session.execute(
                sqlalchemy.text("SELECT count(*) FROM privacydeclaration")
            ).scalar()
    finally:
        engine.dispose()

    assert after == before
    assert after_mappings == before_mappings


def test_remove_dry_run_writes_nothing():
    result = subprocess.run(
        [sys.executable, str(_SCRIPT_PATH), "--remove"],
        capture_output=True, text=True, check=True,
    )
    assert "DRY RUN — nothing written" in result.stdout
    assert "mode: REMOVE" in result.stdout


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


# --- Content shape: SCREENING_DECISIONS / MAPPINGS, no DB required ---------


def test_thirty_eight_decisions_twenty_three_applicable_fifteen_not():
    cli = _load_cli()
    assert len(cli.SCREENING_DECISIONS) == 38
    applicable = [e for e in cli.SCREENING_DECISIONS if e["applicable"]]
    not_applicable = [e for e in cli.SCREENING_DECISIONS if not e["applicable"]]
    assert len(applicable) == 23
    assert len(not_applicable) == 15


def test_process_names_are_unique_within_each_table():
    cli = _load_cli()
    names = [e["process"] for e in cli.SCREENING_DECISIONS]
    assert len(names) == len(set(names))
    mapping_names = [e["process"] for e in cli.MAPPINGS]
    assert len(mapping_names) == len(set(mapping_names))


def test_every_mapped_process_is_marked_applicable():
    """Data mappings only make sense for processes the screen found
    applicable — a mapping for a not-applicable process would be exactly
    the "writes rows the product would not have written" failure the brief
    warns against."""
    cli = _load_cli()
    applicable_names = {e["process"] for e in cli.SCREENING_DECISIONS if e["applicable"]}
    for entry in cli.MAPPINGS:
        assert entry["process"] in applicable_names, entry["process"]


def test_applicable_decisions_carry_no_justification_and_at_least_one_trigger():
    """gate.record_decision's own CheckConstraint forbids a justification
    on an applicable (dpia_required=true) row — a value here would make
    every applicable decision in this seed fail at INSERT time, not
    silently succeed."""
    cli = _load_cli()
    for entry in cli.SCREENING_DECISIONS:
        if entry["applicable"]:
            assert entry["justification"] is None, entry["process"]
            assert len(entry["triggers"]) >= 1, entry["process"]
        else:
            assert entry["triggers"] == (), entry["process"]
            assert entry["justification"] is not None, entry["process"]


def test_not_applicable_justifications_are_not_thin():
    """"A thin justification teaches the wrong lesson" — the brief's own
    words. 300 characters is well short of every justification actually
    written (each runs 500-700), but long enough to catch a placeholder
    like the CSR row's own one-line predecessor the demo-rows inventory
    warns about."""
    cli = _load_cli()
    for entry in cli.SCREENING_DECISIONS:
        if not entry["applicable"]:
            assert len(entry["justification"]) > 300, entry["process"]


def test_not_applicable_justifications_name_all_six_triggers_by_their_live_label(db):
    """Argues against the six triggers BY NAME, per D-SEED-5 — checked
    against the LIVE privacycare_screening_trigger table's own label text,
    not a copy retyped here, so a rename of Carol's wording would fail
    this test rather than silently going stale alongside it."""
    labels = [
        row[0]
        for row in db.execute(
            sqlalchemy.text(
                "SELECT label FROM privacycare_screening_trigger ORDER BY display_order"
            )
        ).all()
    ]
    assert len(labels) == 6, "the six triggers must already be seeded for this test to mean anything"

    cli = _load_cli()
    for entry in cli.SCREENING_DECISIONS:
        if entry["applicable"]:
            continue
        justification = entry["justification"]
        for label in labels:
            assert label in justification, (entry["process"], label)


def test_not_applicable_justifications_self_disclose_as_demo_content():
    cli = _load_cli()
    for entry in cli.SCREENING_DECISIONS:
        if entry["applicable"]:
            continue
        justification = entry["justification"]
        assert "privacycare:demo_seed" in justification, entry["process"]
        assert "demonstration content" in justification.lower(), entry["process"]


def test_mappings_use_only_live_vocabularies(db):
    """D-SEED-3: subjects/categories/purpose come from the loaded taxonomy,
    ground from the 11 USABLE processing grounds only. Checked against the
    live tables, not a second hand-typed list."""
    cli = _load_cli()

    subjects = {
        row[0]
        for row in db.execute(
            sqlalchemy.text("SELECT fides_key FROM ctl_data_subjects")
        ).all()
    }
    categories = {
        row[0]
        for row in db.execute(
            sqlalchemy.text("SELECT fides_key FROM ctl_data_categories")
        ).all()
    }
    purposes = {
        row[0]
        for row in db.execute(
            sqlalchemy.text("SELECT fides_key FROM ctl_data_uses")
        ).all()
    }
    usable_grounds = {
        row[0]
        for row in db.execute(
            sqlalchemy.text(
                "SELECT ground FROM privacycare_processing_ground "
                "WHERE fides_legal_basis IS NOT NULL"
            )
        ).all()
    }
    assert len(usable_grounds) == 11, "expected exactly 11 usable grounds (OQ-W2-4)"

    for entry in cli.MAPPINGS:
        for subject in entry["data_subjects"]:
            assert subject in subjects, (entry["process"], subject)
        for category in entry["data_categories"]:
            assert category in categories, (entry["process"], category)
        assert entry["purpose"] in purposes, (entry["process"], entry["purpose"])
        assert entry["ground"] in usable_grounds, (entry["process"], entry["ground"])


def test_every_named_business_process_exists_and_is_not_soft_deleted(db):
    cli = _load_cli()
    names = {e["process"] for e in cli.SCREENING_DECISIONS} | {
        e["process"] for e in cli.MAPPINGS
    }
    for name in names:
        row = db.execute(
            sqlalchemy.text(
                "SELECT id FROM privacycare_business_process "
                "WHERE name = :name AND deleted_at IS NULL"
            ),
            {"name": name},
        ).first()
        assert row is not None, name


# --- seed_demo(db) / remove_demo(db), against the rolled-back fixture ------


def test_seed_demo_then_seed_demo_again_writes_nothing_the_second_time(db):
    """The core idempotency claim (D-SEED-1): "Running --commit twice must
    leave the same state as running it once." Both calls run inside the
    SAME rolled-back session, so the second call sees the first call's own
    (uncommitted) writes — this holds regardless of whether the real
    --commit has already happened against this database or not."""
    cli = _load_cli()
    first = cli.seed_demo(db)
    assert first["decisions_written"] + first["decisions_skipped"] == 38
    assert first["mappings_written"] + first["mappings_skipped"] == 23

    second = cli.seed_demo(db)
    assert second["decisions_written"] == 0
    assert second["decisions_skipped"] == 38
    assert second["mappings_written"] == 0
    assert second["mappings_skipped"] == 23


def test_seed_then_remove_returns_every_count_to_its_pre_seed_value(db):
    """Acceptance criterion 2, proved directly: capture counts before,
    seed, capture after, remove, capture after removal — before must equal
    after-removal.

    NORMALISES FIRST, via its own remove_demo(db) call, before capturing
    "before". This matters once the real --commit has landed permanently
    (as it now has — this task's own last step): without normalising,
    "before" (captured at the top of this test's own transaction) would
    already include the real, permanently-committed demo rows, seed_demo()
    would then be a no-op (everything already exists), and remove_demo()
    would still strip the database back to the TRUE pristine baseline —
    correctly, but leaving "before" (seeded) and "after removal" (pristine)
    unequal for a reason that has nothing to do with a defect in
    remove_demo() itself. Calling remove_demo(db) once up front makes this
    test's own "before" the same pristine baseline regardless of what has
    or hasn't been committed outside this rolled-back session — a
    same-marker-scoped DELETE that finds nothing to delete is a harmless
    no-op either way.
    """
    cli = _load_cli()

    cli.remove_demo(db)  # normalise to a known, marker-free baseline
    before = cli.gather_counts(db)

    cli.seed_demo(db)
    after_seed = cli.gather_counts(db)
    assert after_seed["demo_marked_declarations"] == 23
    assert after_seed["demo_marked_decisions"] == 38

    cli.remove_demo(db)
    after_remove = cli.gather_counts(db)

    assert after_remove == before


def test_remove_never_touches_prior_art_rows(db):
    """The single highest-stakes assertion in this file: the 2 pre-existing
    demonstration decisions and 4 processing activities / 3 systems from
    plan 20 Task 5 carry NEITHER marker this script uses, and must survive
    seed_demo() + remove_demo() by id, not just by count."""
    cli = _load_cli()
    cli.seed_demo(db)
    cli.remove_demo(db)

    for decision_id in _PRIOR_ART_DECISION_IDS:
        row = db.execute(
            sqlalchemy.text(
                "SELECT id FROM privacycare_screening_decision WHERE id = :id"
            ),
            {"id": decision_id},
        ).first()
        assert row is not None, decision_id

    for declaration_id in _PRIOR_ART_DECLARATION_IDS:
        row = db.execute(
            sqlalchemy.text("SELECT id FROM privacydeclaration WHERE id = :id"),
            {"id": declaration_id},
        ).first()
        assert row is not None, declaration_id

    for system_id in _PRIOR_ART_SYSTEM_IDS:
        row = db.execute(
            sqlalchemy.text("SELECT id FROM ctl_systems WHERE id = :id"),
            {"id": system_id},
        ).first()
        assert row is not None, system_id


def test_remove_is_exact_marker_match_not_a_substring():
    """decided_by = 'client:privacycare:demo_seed' must be an EXACT match,
    not a LIKE/substring — a row whose decided_by merely CONTAINS the
    marker (e.g. a hypothetical future 'client:privacycare:demo_seed:v2')
    must not be swept up by this script's own removal."""
    cli = _load_cli()
    assert cli.DECIDED_BY_MARKER == "client:privacycare:demo_seed"
    # The removal statement itself must use exact equality.
    from sqlalchemy import text as _text  # local import, mirrors cli's own
    compiled = str(cli._DELETE_SCREENING_DECISIONS_SQL)
    assert "decided_by = :decided_by" in compiled
    assert "LIKE" not in compiled.upper()


def test_appended_marker_never_replaces_the_mapping_routes_own_marker(db):
    """WHY THE MARKER IS APPENDED, NOT SUBSTITUTED (this script's own
    module docstring): save_mapping()'s own idempotency depends on
    'privacycare:mapping_route' staying in features. Prove it directly —
    seed once, read back a mapped declaration's features, and require both
    markers to be present."""
    cli = _load_cli()
    cli.seed_demo(db)

    row = db.execute(
        sqlalchemy.text(
            "SELECT pd.features FROM privacycare_process_declaration link "
            "JOIN privacydeclaration pd ON pd.id = link.privacy_declaration_id "
            "JOIN privacycare_business_process bp ON bp.id = link.business_process_id "
            "WHERE bp.name = :name AND :marker = ANY(pd.features)"
        ),
        {"name": "Payroll Administration", "marker": cli.DEMO_FEATURE_MARKER},
    ).first()
    assert row is not None
    features = list(row[0])
    assert "privacycare:demo_seed" in features
    assert "privacycare:mapping_route" in features


def test_reused_system_is_never_tagged_by_this_seed(db):
    """Fuel card application processing already has a REAL system
    (fuel_card_crm) linked via its two pre-existing, unmarked activities.
    This seed's new mapping for that same process must reuse that system,
    not provision (and therefore never mark) a new one."""
    cli = _load_cli()
    cli.seed_demo(db)

    row = db.execute(
        sqlalchemy.text("SELECT tags FROM ctl_systems WHERE id = :id"),
        {"id": "ctl_ef9cadb3-828b-4838-a0a2-ea7c68dfed07"},
    ).first()
    assert row is not None
    tags = list(row[0]) if row[0] else []
    assert cli.DEMO_FEATURE_MARKER not in tags


def test_gather_counts_matches_forty_decided_after_a_fresh_seed(db):
    """Ties this task's own arithmetic back to the design doc: 2 existing
    + 38 new = 40 decided (24 applicable / 16 not applicable), 87 - 40 = 47
    still Not screened."""
    cli = _load_cli()
    cli.seed_demo(db)
    counts = cli.gather_counts(db)
    assert counts["screening_decisions_total"] == 40
    assert counts["screening_decisions_applicable"] == 24
    assert counts["screening_decisions_not_applicable"] == 16

    total_processes = db.execute(
        sqlalchemy.text(
            "SELECT count(*) FROM privacycare_business_process WHERE deleted_at IS NULL"
        )
    ).scalar()
    assert total_processes == 87
    assert total_processes - counts["screening_decisions_total"] == 47
