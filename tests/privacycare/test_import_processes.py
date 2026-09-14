# Imports a parsed business-process register (the JSON shape produced by
# docs/DataPrivacyManenos/converted/*_process_and_data_map.json) into
# privacycare_business_process. Task 2's CLI owns reading the file from disk
# and the dry-run/--commit switch; this module's contract is "take data, do
# the write, never decide whether to keep it" — same split as
# taxonomy/loader.py's load_kenyan_taxonomy, and the same db fixture shape.
#
# D-IMP-2: the live database already holds rows this suite did not create (1
# privacycare_business_process, 3 privacycare_process_declaration at the time
# this was written). Every count assertion below is therefore scoped to the
# external_refs the test itself created via _ref(), never a whole-table
# count.
import json
import pathlib
import uuid

import pytest
import sqlalchemy
from sqlalchemy.orm import Session

from fides.api.privacycare.importers.processes import ImportSummary, import_processes

DB_URL = "postgresql://postgres:fides@127.0.0.1:5442/fides"


@pytest.fixture
def db(monkeypatch):
    engine = sqlalchemy.create_engine(DB_URL)
    with Session(engine) as session:
        # import_processes never commits (D-IMP-1: the caller's session
        # boundary decides that, same as load_kenyan_taxonomy) but nothing in
        # a test may commit either.
        monkeypatch.setattr(session, "commit", lambda: None)
        yield session
        session.rollback()


def _ref(n) -> str:
    # A unique external_ref per call so two tests (or two calls within one
    # test) never collide with each other or with the 1 pre-existing live
    # row (external_ref NULL).
    return f"t08-{uuid.uuid4().hex[:6]}-{n}"


def _register(processes, with_mapping=()):
    return {
        "processes": processes,
        "processes_with_data_mapping_detail": list(with_mapping),
    }


def test_every_process_in_the_register_is_created(db):
    ref1, ref2 = _ref(1), _ref(2)
    register = _register(
        [
            {
                "number": ref1,
                "name": "Customer Account Management",
                "description": "Manage customer profiles.",
                "business_cycle": "Customer Service",
                "applicable": "1",
            },
            {
                "number": ref2,
                "name": "Customer Complaints Handling",
                "description": "Resolve complaints.",
                "business_cycle": "Customer Service",
                "applicable": "0",
            },
        ]
    )

    summary = import_processes(db, register)

    assert summary.created == 2
    names = (
        db.execute(
            sqlalchemy.text(
                "SELECT name FROM privacycare_business_process "
                "WHERE external_ref = ANY(:refs) ORDER BY name"
            ),
            {"refs": [ref1, ref2]},
        )
        .scalars()
        .all()
    )
    assert names == ["Customer Account Management", "Customer Complaints Handling"]


def test_re_running_updates_rather_than_duplicating(db):
    # D-IMP-2: the consultant WILL re-run this after the customer revises the
    # workbook. A second run that doubles the register is worse than useless.
    ref = _ref(1)
    register = _register(
        [
            {
                "number": ref,
                "name": "Customer Account Management",
                "description": "First description.",
                "business_cycle": "Customer Service",
                "applicable": "1",
            }
        ]
    )
    import_processes(db, register)

    register["processes"][0]["description"] = "Revised description."
    summary = import_processes(db, register)

    assert summary.created == 0 and summary.updated == 1
    rows = (
        db.execute(
            sqlalchemy.text(
                "SELECT description FROM privacycare_business_process "
                "WHERE external_ref = :ref"
            ),
            {"ref": ref},
        )
        .scalars()
        .all()
    )
    assert rows == ["Revised description."], "the register was duplicated, not updated"


def test_re_running_with_no_changes_reports_unchanged(db):
    # A matched row whose stored fields already equal the register is neither
    # created nor updated: a re-run over an unrevised register must not touch
    # updated_at on every single row.
    ref = _ref(1)
    register = _register(
        [
            {
                "number": ref,
                "name": "Customer Account Management",
                "description": "Same description.",
                "business_cycle": "Customer Service",
                "applicable": "1",
            }
        ]
    )
    import_processes(db, register)

    summary = import_processes(db, register)

    assert (summary.created, summary.updated, summary.unchanged) == (0, 0, 1)


def test_blank_applicability_is_counted_not_guessed(db):
    # D-IMP-3: blank means "the customer has not said", not "no". It imports
    # as not-critical because the column is NOT NULL, and the count is
    # reported so the unanswered prioritisation stays visible.
    r1, r2, r3 = _ref(1), _ref(2), _ref(3)
    register = _register(
        [
            {"number": r1, "name": "A", "business_cycle": "Finance & Accounting", "applicable": "1"},
            {"number": r2, "name": "B", "business_cycle": "Finance & Accounting", "applicable": ""},
            {"number": r3, "name": "C", "business_cycle": "Finance & Accounting", "applicable": None},
        ]
    )

    summary = import_processes(db, register)

    assert summary.blank_applicability == 2
    rows = db.execute(
        sqlalchemy.text(
            "SELECT external_ref, is_critical FROM privacycare_business_process "
            "WHERE external_ref = ANY(:r)"
        ),
        {"r": [r1, r2, r3]},
    ).all()
    critical = dict(rows)
    assert critical == {r1: True, r2: False, r3: False}


def test_no_declaration_links_are_invented(db):
    # D-IMP-4: 85 of 86 processes have no mapping. A ROPA that claims
    # processing the customer never described is the one thing it must not
    # be. Scoped to the row this test created (Ruling 2) — the live database
    # already holds 3 privacycare_process_declaration rows.
    ref = _ref(1)
    register = _register([{"number": ref, "name": "A", "business_cycle": "Finance & Accounting"}])

    import_processes(db, register)

    links = db.execute(
        sqlalchemy.text(
            "SELECT COUNT(*) FROM privacycare_process_declaration "
            "WHERE business_process_id IN ("
            "  SELECT id FROM privacycare_business_process WHERE external_ref = ANY(:refs)"
            ")"
        ),
        {"refs": [ref]},
    ).scalar()
    assert links == 0


def test_the_summary_reports_the_size_of_the_remaining_work(db):
    # D-IMP-5: the point of this import is as much showing what is left as
    # loading the rows.
    refs = [_ref(i) for i in range(1, 6)]
    register = _register(
        [
            {"number": refs[i - 1], "name": f"P{i}", "business_cycle": "Finance & Accounting"}
            for i in range(1, 6)
        ],
        with_mapping=["P1"],
    )

    summary = import_processes(db, register)

    assert summary.without_data_mapping == 4
    assert summary.cycles == {"Finance & Accounting": 5}


def test_a_process_with_no_name_is_rejected_not_skipped(db):
    # A nameless row is a malformed register, not a process. Skipping it
    # silently would make the import counts disagree with the workbook.
    register = _register([{"number": _ref(1), "name": "", "business_cycle": "Finance"}])

    with pytest.raises(ValueError, match="name"):
        import_processes(db, register)


def test_a_process_with_no_number_is_rejected(db):
    # external_ref is str(number).strip(); a process with no number at all
    # cannot be matched on re-run, so it is malformed the same way a blank
    # name is.
    register = _register([{"name": "A", "business_cycle": "Finance"}])

    with pytest.raises(ValueError, match="number"):
        import_processes(db, register)


def test_a_malformed_register_writes_nothing(db):
    # Validate the whole register before writing anything, so a malformed
    # file leaves the session untouched rather than half-importing.
    good_ref = _ref(1)
    register = _register(
        [
            {"number": good_ref, "name": "Good One", "business_cycle": "Finance"},
            {"number": _ref(2), "name": "", "business_cycle": "Finance"},
        ]
    )

    with pytest.raises(ValueError, match="name"):
        import_processes(db, register)

    count = db.execute(
        sqlalchemy.text(
            "SELECT COUNT(*) FROM privacycare_business_process WHERE external_ref = :ref"
        ),
        {"ref": good_ref},
    ).scalar()
    assert count == 0, "a malformed register must not write the rows that precede the bad one"


def test_multiple_live_rows_sharing_external_ref_raises(db):
    # There is no unique index on external_ref and none may be added (no
    # migration in this task). If more than one live row already shares the
    # ref the importer is trying to match, it must not guess which one to
    # update.
    ref = _ref(1)
    register = _register([{"number": ref, "name": "A", "business_cycle": "Finance"}])
    import_processes(db, register)
    db.execute(
        sqlalchemy.text(
            "INSERT INTO privacycare_business_process "
            "(id, name, business_cycle, is_critical, external_ref) "
            "VALUES (:id, 'Duplicate', 'Finance', false, :ref)"
        ),
        {"id": f"bp_{uuid.uuid4().hex[:12]}", "ref": ref},
    )

    with pytest.raises(ValueError, match=ref):
        import_processes(db, register)


REAL_REGISTER_PATH = pathlib.Path(
    "/home/shikoli/Cybota/LightHouse/docs/DataPrivacyManenos/converted/"
    "06_oil_marketer_process_and_data_map.json"
)


def test_importing_the_real_oil_marketer_register(db):
    # The one real-data assertion in this task: the actual customer register
    # imports to the shape a consultant would expect. Read-only against the
    # source file; the fixture's rollback discards everything this writes.
    if not REAL_REGISTER_PATH.exists():
        pytest.skip(f"{REAL_REGISTER_PATH} not present")
    register = json.loads(REAL_REGISTER_PATH.read_text())

    summary = import_processes(db, register)

    assert summary.created == 86
    assert summary.without_data_mapping == 85
    assert summary.blank_applicability == 85
    assert len(summary.cycles) == 17
    assert summary.cycles["Finance & Accounting"] == 11
