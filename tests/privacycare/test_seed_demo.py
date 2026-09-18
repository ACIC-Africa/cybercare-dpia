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
seed_demo() and remove_demo() themselves. Screening and mapping are pure
raw SQL with no ORM persist_obj call to trip; DSR requests are raw SQL
too (dsr.register.record_request). Consent is the one exception —
seed_consent() calls PrivacyPreferenceHistory.create(), which DOES call
persist_obj's own unconditional db.commit() — but the `db` fixture's own
monkeypatch already absorbs that into a flush, the same guard seed_demo.py's
own main() now carries for real (see seed_demo.py's own module docstring,
"SCREENING AND MAPPING ARE RAW SQL; DSR AND CONSENT ARE NOT").

DSR/CONSENT ADDITIONS (D-SEED-8/D-SEED-9) ARE ALSO DB-STATE-INDEPENDENT.
The live database already carries 0 `privacycare_dsr_request` rows and
exactly 1 pre-existing `privacypreferencehistory` row (seed_consent_demo.py's
own DEMO_SUBJECT_EMAIL, opted in at v1) before this task's own `--commit`
lands, same as the DPIA-spine counts above — so these tests use the same
`written + skipped == N` and marker-scoped-count idioms, not a bare count
against an assumed-empty table.
"""
import importlib.util
import os
import pathlib
import subprocess
import sys
from datetime import datetime, timezone

import pytest
import sqlalchemy
from sqlalchemy.orm import Session

from fides.api.privacycare.consent.detector import find_stale_consents
from fides.api.privacycare.dsr.alerts import alert_due
from fides.api.privacycare.dsr.timelines import KENYAN_RIGHTS, timeline_days

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
    ground from the USABLE processing grounds only. Checked against the
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
    # OQ-W2-4 was 11 usable grounds; Carol has since ruled on 9 more of the
    # originally-unmapped 12 (WhatsApp, two rounds on 2026-09-18) and "N/A"
    # was retired, taking usable from 11 to 20 (of 22 total grounds — the 2
    # still unmapped, "Legitimate Activities by a Foundation..." and
    # "Research", are deliberate, not an oversight). This seed's own
    # MAPPINGS table was authored before that ruling and still only uses
    # the original 11, which remain a subset of the 20 usable grounds today
    # — that containment is what the loop below actually checks.
    assert len(usable_grounds) == 20, "expected exactly 20 usable grounds post-ruling (was 11, OQ-W2-4)"

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
    assert first["dsr_requests_written"] + first["dsr_requests_skipped"] == 7
    assert (
        first["consent_preferences_written"] + first["consent_preferences_skipped"]
        == 2
    )

    second = cli.seed_demo(db)
    assert second["decisions_written"] == 0
    assert second["decisions_skipped"] == 38
    assert second["mappings_written"] == 0
    assert second["mappings_skipped"] == 23
    assert second["dsr_requests_written"] == 0
    assert second["dsr_requests_skipped"] == 7
    assert second["consent_preferences_written"] == 0
    assert second["consent_preferences_skipped"] == 2


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
    assert after_seed["dsr_requests_demo_marked"] == 7
    assert after_seed["privacypreferencehistory_demo_marked"] == 2
    # seed_consent_demo.py's own pre-existing row is the +1 this script
    # never creates and never removes — proved directly, not just via the
    # round-trip equality below.
    assert (
        after_seed["privacypreferencehistory_total"]
        == before["privacypreferencehistory_total"] + 2
    )

    cli.remove_demo(db)
    after_remove = cli.gather_counts(db)

    assert after_remove == before


def test_remove_never_touches_prior_art_rows(db):
    """The single highest-stakes assertion in this file: the 2 pre-existing
    demonstration decisions and 4 processing activities / 3 systems from
    plan 20 Task 5 — PLUS seed_consent_demo.py's own pre-existing
    preference row — carry NEITHER marker this script uses, and must
    survive seed_demo() + remove_demo() by id, not just by count."""
    cli = _load_cli()

    # Captured BEFORE this test's own seed_demo() call, identified the same
    # way remove_demo() itself would never select it: no DEMO_FEATURE_MARKER
    # in url_recorded. There is exactly one such row in the live database
    # (seed_consent_demo.py's own DEMO_SUBJECT_EMAIL preference).
    prior_art_preference_ids = [
        row[0]
        for row in db.execute(
            sqlalchemy.text(
                "SELECT id FROM privacypreferencehistory WHERE url_recorded IS NULL"
            )
        ).all()
    ]
    assert len(prior_art_preference_ids) == 1

    cli.seed_demo(db)
    cli.remove_demo(db)

    for preference_id in prior_art_preference_ids:
        row = db.execute(
            sqlalchemy.text(
                "SELECT id FROM privacypreferencehistory WHERE id = :id"
            ),
            {"id": preference_id},
        ).first()
        assert row is not None, preference_id

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


# --- DSR requests (D-SEED-8) --------------------------------------------


def test_dsr_requests_are_idempotent_by_exact_subject_identifier(db):
    """The DSR analogue of test_seed_demo_then_seed_demo_again... above,
    but calling seed_dsr_requests() directly so a failure here can't be
    confused with a screening/mapping regression."""
    cli = _load_cli()
    first = cli.seed_dsr_requests(db)
    assert first["dsr_requests_written"] == 7
    assert first["dsr_requests_skipped"] == 0

    second = cli.seed_dsr_requests(db)
    assert second["dsr_requests_written"] == 0
    assert second["dsr_requests_skipped"] == 7


def test_dsr_requests_span_all_six_kenyan_rights_with_objection_unclocked(db):
    """D-SEED-8's own spread requirement, proved against the live register:
    all six Kenyan rights are represented (access appears twice), and the
    objection row's deadline_at is NULL — the one request D-SEED-8 asks for
    explicitly, to prove the unclocked case renders as "no deadline", never
    a false one."""
    cli = _load_cli()
    cli.seed_dsr_requests(db)

    rows = db.execute(
        sqlalchemy.text(
            "SELECT \"right\", deadline_at FROM privacycare_dsr_request "
            "WHERE subject_identifier LIKE :pattern"
        ),
        {"pattern": f"%{cli.DEMO_FEATURE_MARKER}%"},
    ).all()
    assert len(rows) == 7

    rights_seen = {row[0] for row in rows}
    assert rights_seen == set(KENYAN_RIGHTS)

    objection_rows = [row for row in rows if row[0] == "objection"]
    assert len(objection_rows) == 1
    assert objection_rows[0][1] is None  # deadline_at


def test_dsr_requests_demonstrate_comfortably_inside_close_to_breach_and_overdue(db):
    """Proves the spread against dsr/alerts.py's REAL warn_threshold, not a
    guess at it — the same discipline this module's own docstring applies
    to D-SEED-9's consent detector, applied here to D-SEED-8's alerting."""
    cli = _load_cli()
    cli.seed_dsr_requests(db)
    now = datetime.now(timezone.utc)

    rows = db.execute(
        sqlalchemy.text(
            "SELECT \"right\", deadline_at FROM privacycare_dsr_request "
            "WHERE subject_identifier LIKE :pattern"
        ),
        {"pattern": f"%{cli.DEMO_FEATURE_MARKER}%"},
    ).all()

    kinds = set()
    unclocked_present = False
    for right, deadline_at in rows:
        if deadline_at is None:
            unclocked_present = True
            continue
        days_allowed = timeline_days(db, right)
        kind = alert_due(
            deadline_at=deadline_at,
            days_allowed=days_allowed,
            now=now,
            already_sent=frozenset(),
        )
        kinds.add(kind)  # None ("comfortably inside"), "approaching", "breached"

    assert None in kinds  # at least one comfortably inside
    assert "approaching" in kinds  # at least one close to breach
    assert "breached" in kinds  # at least one overdue
    assert unclocked_present  # the objection row


def test_dsr_requests_are_realistic_kenyan_identities_not_placeholders(db):
    """"Never Test User 1, foo, or lorem ipsum" (the brief's own words),
    and every identity carries a full name plus a contact detail — checked
    against DSR_REQUESTS itself (the source of truth this module's
    docstring documents), not against a second, hand-typed copy."""
    cli = _load_cli()
    _forbidden = ("test user", "foo", "lorem", "ipsum", "placeholder", "example user")
    for entry in cli.DSR_REQUESTS:
        name_lower = entry["name"].lower()
        for bad in _forbidden:
            assert bad not in name_lower, entry["name"]
        assert " " in entry["name"].strip()  # a full name, not a single token
        assert entry["contact"]  # a phone number or email, never blank

        subject_identifier = cli._dsr_subject_identifier(
            entry["name"], entry["contact"]
        )
        assert cli.DEMO_FEATURE_MARKER in subject_identifier
        assert "synthetic demo requester" in subject_identifier


def test_dsr_erasure_request_is_left_unlinked_demonstrating_the_unowned_path(db):
    """Peter Kamau's erasure request carries no business_process_id and no
    explicit owner_email — demonstrating dsr.register.resolve_owner's
    configured_dpo/unassigned branch and alert_job.py's own "unowned"
    counter, the other end of D-DSR-8's fallback chain from the five
    business-process-linked requests."""
    cli = _load_cli()
    cli.seed_dsr_requests(db)

    row = db.execute(
        sqlalchemy.text(
            "SELECT business_process_id, owner_source FROM privacycare_dsr_request "
            "WHERE \"right\" = 'erasure' AND subject_identifier LIKE :pattern"
        ),
        {"pattern": f"%{cli.DEMO_FEATURE_MARKER}%"},
    ).first()
    assert row is not None
    assert row[0] is None  # business_process_id
    assert row[1] in ("configured_dpo", "unassigned")  # owner_source


def test_remove_dsr_pattern_anchors_on_the_full_marker_not_a_fragment(db):
    """The DSR analogue of test_remove_is_exact_marker_match_not_a_substring:
    the LIKE pattern is built from the FULL literal DEMO_FEATURE_MARKER,
    never a fragment of it, so this remains a marker match, not a guess."""
    cli = _load_cli()
    compiled = str(cli._SELECT_DEMO_DSR_REQUEST_IDS_SQL)
    assert "subject_identifier LIKE :pattern" in compiled
    assert cli.DEMO_FEATURE_MARKER == "privacycare:demo_seed"


# --- Consent (D-SEED-9) --------------------------------------------------


def test_consent_seed_is_idempotent(db):
    cli = _load_cli()
    first = cli.seed_consent(db)
    assert first["consent_preferences_written"] == 2
    assert first["consent_preferences_skipped"] == 0

    second = cli.seed_consent(db)
    assert second["consent_preferences_written"] == 0
    assert second["consent_preferences_skipped"] == 2


def test_consent_fresh_is_not_flagged_stale_is_and_prior_art_still_is(db):
    """The detector's real mechanism (version lag, never elapsed time —
    see this module's own docstring, "D-SEED-9"), proved by actually
    calling find_stale_consents(): the new fresh row (opted in at v2, the
    live version) is absent from the report; the new stale row (opted in
    at v1) is present; and seed_consent_demo.py's own pre-existing stale
    row is STILL present too — proving this script added a finding
    without disturbing the one that already existed."""
    cli = _load_cli()
    demo_consent = cli._seed_consent_demo_module()
    cli.seed_consent(db)

    findings = find_stale_consents(db)
    subjects = {finding.subject for finding in findings}

    fresh_entry = next(e for e in cli.CONSENT_ADDITIONS if e["label"] == "fresh")
    stale_entry = next(e for e in cli.CONSENT_ADDITIONS if e["label"] == "stale")

    assert fresh_entry["email"] not in subjects
    assert stale_entry["email"] in subjects
    assert demo_consent.DEMO_SUBJECT_EMAIL in subjects


def test_remove_removes_only_this_scripts_two_new_preference_rows(db):
    """Acceptance: --remove takes the count from 3 (1 prior art + 2 new)
    back to 1, never to 0 — proving seed_consent_demo.py's own row survives
    by count here, complementing test_remove_never_touches_prior_art_rows'
    by-id proof above."""
    cli = _load_cli()
    before = db.execute(
        sqlalchemy.text("SELECT count(*) FROM privacypreferencehistory")
    ).scalar()

    cli.seed_consent(db)
    after_seed = db.execute(
        sqlalchemy.text("SELECT count(*) FROM privacypreferencehistory")
    ).scalar()
    assert after_seed == before + 2

    cli.remove_demo(db)
    after_remove = db.execute(
        sqlalchemy.text("SELECT count(*) FROM privacypreferencehistory")
    ).scalar()
    assert after_remove == before
