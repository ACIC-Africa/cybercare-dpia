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
import importlib.util
import json
import pathlib
import subprocess
import sys
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


def test_a_register_row_missing_the_description_key_does_not_clear_it(db):
    # F2/D-IMP-2: "update name/description/cycle when present, and never
    # touch a field the source does not carry." The register is read with
    # `.get()`, so an ABSENT `description` key used to become None and the
    # UPDATE wrote NULL over whatever was stored. First run carries a
    # description; second run's row has no `description` key at all (not
    # even `None`) — the stored description must survive untouched, and
    # since nothing else differs the row must count as `unchanged`, not
    # `updated` (a column never written cannot make a row count as updated).
    ref = _ref(1)
    first_register = _register(
        [
            {
                "number": ref,
                "name": "Customer Account Management",
                "description": "Manage customer profiles.",
                "business_cycle": "Customer Service",
                "applicable": "1",
            }
        ]
    )
    import_processes(db, first_register)

    second_register = _register(
        [
            {
                "number": ref,
                "name": "Customer Account Management",
                # No "description" key at all.
                "business_cycle": "Customer Service",
                "applicable": "1",
            }
        ]
    )
    summary = import_processes(db, second_register)

    assert (summary.created, summary.updated, summary.unchanged) == (0, 0, 1)
    stored_description = db.execute(
        sqlalchemy.text(
            "SELECT description FROM privacycare_business_process WHERE external_ref = :ref"
        ),
        {"ref": ref},
    ).scalar()
    assert stored_description == "Manage customer profiles.", (
        "an absent description key must not clear the stored description"
    )


def test_a_register_row_with_an_explicit_none_description_does_clear_it(db):
    # Contrast case for F2: a key that IS present, even with an explicit
    # None/blank value, still writes — the source carried it, so this is not
    # the "absent key" case above. Confirms the presence check is `"description"
    # in process`, not `process.get("description") is not None`.
    ref = _ref(1)
    first_register = _register(
        [
            {
                "number": ref,
                "name": "Customer Account Management",
                "description": "Manage customer profiles.",
                "business_cycle": "Customer Service",
                "applicable": "1",
            }
        ]
    )
    import_processes(db, first_register)

    second_register = _register(
        [
            {
                "number": ref,
                "name": "Customer Account Management",
                "description": None,
                "business_cycle": "Customer Service",
                "applicable": "1",
            }
        ]
    )
    summary = import_processes(db, second_register)

    assert (summary.created, summary.updated, summary.unchanged) == (0, 1, 0)
    stored_description = db.execute(
        sqlalchemy.text(
            "SELECT description FROM privacycare_business_process WHERE external_ref = :ref"
        ),
        {"ref": ref},
    ).scalar()
    assert stored_description is None


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

    summary = import_processes(db, register)

    # F3: without this, links == 0 passes vacuously whether the importer
    # created the process (and correctly made no link) or created nothing at
    # all — pin down that the row actually exists.
    assert summary.created == 1
    process_id = db.execute(
        sqlalchemy.text(
            "SELECT id FROM privacycare_business_process WHERE external_ref = :ref"
        ),
        {"ref": ref},
    ).scalar()
    assert process_id is not None, "the process row was never created"

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
    # loading the rows. F1 ruling: `cycles` is the distribution of the
    # WITHOUT-mapping processes only, not of every process in the register —
    # P1 is mapped, so Finance & Accounting counts 4, not 5.
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
    assert summary.cycles == {"Finance & Accounting": 4}


def test_cycles_counts_only_unmapped_processes_even_when_the_mapped_one_is_in_the_bigger_cycle(
    db,
):
    # F1 pin-down: two cycles, and the ONE mapped process sits in the LARGER
    # cycle. The old (wrong) code printed the full per-cycle distribution —
    # Cycle Service: 4, Cycle Finance: 2 — regardless of mapping. Under the
    # ruling, the mapped process is invisible to `cycles`: Cycle Service
    # drops to 3 and Cycle Finance stays at 2, because neither of Finance's
    # two processes has a mapping.
    refs = [_ref(i) for i in range(1, 7)]
    register = _register(
        [
            {"number": refs[0], "name": "S1", "business_cycle": "Cycle Service"},
            {"number": refs[1], "name": "S2", "business_cycle": "Cycle Service"},
            {"number": refs[2], "name": "S3", "business_cycle": "Cycle Service"},
            {"number": refs[3], "name": "S4-mapped", "business_cycle": "Cycle Service"},
            {"number": refs[4], "name": "F1", "business_cycle": "Cycle Finance"},
            {"number": refs[5], "name": "F2", "business_cycle": "Cycle Finance"},
        ],
        with_mapping=["S4-mapped"],
    )

    summary = import_processes(db, register)

    assert summary.without_data_mapping == 5
    assert summary.cycles == {"Cycle Service": 3, "Cycle Finance": 2}


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
    # update. This state is not necessarily "a prior data anomaly" (F4) —
    # concurrent runs or a row made through the create_business_process API
    # route can produce it too — so the message must name the remedy
    # (delete or re-ref one of the duplicates) rather than imply blame.
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

    with pytest.raises(ValueError, match=ref) as exc_info:
        import_processes(db, register)
    message = str(exc_info.value)
    assert "delete" in message and "re-ref" in message, (
        "the error must name the remedy (delete or re-ref a duplicate row), "
        f"got: {message!r}"
    )


REAL_REGISTER_PATH = pathlib.Path(
    "/home/shikoli/Cybota/LightHouse/docs/DataPrivacyManenos/converted/"
    "06_oil_marketer_process_and_data_map.json"
)


def test_importing_the_real_oil_marketer_register(db):
    # The one real-data assertion in this task: the actual customer register
    # imports to the shape a consultant would expect. Read-only against the
    # source file; the fixture's rollback discards everything this writes.
    #
    # Same accommodation as test_taxonomy_loader.py's F1b: this suite must
    # stay green whether run against a fresh DB or the live one Task 2 Step
    # 5 already loaded for real (that commit persists — the fixture only
    # rolls back what THIS test writes). Once the live DB carries these 86
    # external_refs, a second run of this test sees them matched, not
    # created, so we assert the invariant that holds either way rather than
    # a fresh-DB-only created count.
    if not REAL_REGISTER_PATH.exists():
        pytest.skip(f"{REAL_REGISTER_PATH} not present")
    register = json.loads(REAL_REGISTER_PATH.read_text())

    summary = import_processes(db, register)

    assert summary.created + summary.updated + summary.unchanged == 86
    assert summary.created == 86 or summary.unchanged == 86
    assert summary.without_data_mapping == 85
    assert summary.blank_applicability == 85

    # F1 ruling: `cycles` is the distribution of the 85 WITHOUT-mapping
    # processes only. The register's one mapped process ("Customer Account
    # Management") is itself a Customer Service process, so that cycle is
    # the only one whose count changes from the pre-F1 (whole-register)
    # numbers: Customer Service drops from 7 to 6. No cycle vanishes — every
    # one of the 17 business cycles in the file still has at least one
    # unmapped process, so `len(summary.cycles)` stays 17, not fewer. These
    # numbers (17 cycles, Customer Service 6, Finance & Accounting 11) were
    # computed directly from REAL_REGISTER_PATH's contents, not guessed —
    # Finance & Accounting has no mapped process in it, so its count is
    # unchanged at 11.
    assert len(summary.cycles) == 17
    assert summary.cycles["Finance & Accounting"] == 11
    assert summary.cycles["Customer Service"] == 6
    assert sum(summary.cycles.values()) == 85


# --- Task 2: the CLI --------------------------------------------------------
CLI_PATH = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "privacycare" / "import_processes.py"


def _load_cli_module():
    # scripts/ has no __init__.py (it's not a package), so load
    # import_processes.py by path rather than a normal import — same
    # approach as test_taxonomy_loader.py's _load_cli_module(), used here to
    # unit-test the CLI's main() in-process (needed for the --commit test,
    # which must monkeypatch Session.commit before main() runs).
    spec = importlib.util.spec_from_file_location(
        "privacycare_import_processes_cli", CLI_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_target_description_never_includes_the_password():
    # F5/D-IMP-6, unit-level: the real test DB's password ("fides") happens
    # to equal its database name, which makes a substring-based "no password
    # in output" assertion against the real DB unreliable. Call
    # _target_description() directly with a synthetic URL whose password is
    # unambiguous and distinct from every other component, so this proves
    # redaction unconditionally.
    cli = _load_cli_module()
    url = "postgresql://someuser:Sup3rSecretPW9@dbhost.internal:6543/somedb"

    target = cli._target_description(url)

    assert target == "someuser@dbhost.internal:6543/somedb"
    assert "Sup3rSecretPW9" not in target
    assert ":" not in target.split("@")[0], "no credential separator before '@'"


def test_cli_missing_path_exits_nonzero_naming_the_path():
    missing = "/tmp/t08-does-not-exist-register.json"
    result = subprocess.run(
        [sys.executable, str(CLI_PATH), missing],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert missing in result.stderr


def test_cli_malformed_json_exits_nonzero_naming_the_file(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    result = subprocess.run(
        [sys.executable, str(CLI_PATH), str(bad)],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert str(bad) in result.stderr


def test_cli_wrong_shape_exits_nonzero_with_a_clear_message(tmp_path):
    wrong = tmp_path / "wrong.json"
    wrong.write_text(json.dumps({"processes": {}}))
    result = subprocess.run(
        [sys.executable, str(CLI_PATH), str(wrong)],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert str(wrong) in result.stderr
    assert "'processes' list" in result.stderr


def test_cli_core_value_error_exits_nonzero_with_its_message(tmp_path):
    # A register that is well-shaped JSON but fails the core's own
    # validation (blank name) — the CLI must surface import_processes'
    # ValueError message, not a traceback.
    bad_register = _register([{"number": _ref(1), "name": "", "business_cycle": "Finance"}])
    path = tmp_path / "register.json"
    path.write_text(json.dumps(bad_register))
    result = subprocess.run(
        [sys.executable, str(CLI_PATH), str(path)],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "name" in result.stderr
    assert "Traceback" not in result.stderr


def test_cli_dry_run_writes_nothing_and_reports_the_full_summary(tmp_path, db):
    # This is the plan's D-IMP-6 test, moved here: a dry run by default must
    # not write, and the summary text must name every count plus the cycle
    # breakdown.
    ref1, ref2 = _ref(1), _ref(2)
    register = _register(
        [
            {
                "number": ref1,
                "name": "A",
                "business_cycle": "Finance & Accounting",
                "applicable": "1",
            },
            {
                "number": ref2,
                "name": "B",
                "business_cycle": "Finance & Accounting",
                "applicable": "0",
            },
        ]
    )
    path = tmp_path / "register.json"
    path.write_text(json.dumps(register))

    result = subprocess.run(
        [sys.executable, str(CLI_PATH), str(path)],
        capture_output=True,
        text=True,
        check=True,
    )
    out = result.stdout
    # F5/D-IMP-6: the target line must name where the run would write,
    # before anything else — and it must never leak the password. The test
    # DB's default password ("fides") happens to equal its database name, so
    # this asserts the specific credential SHAPE is absent (no "user:pass@"
    # and no raw "postgresql://" URL) rather than asserting the substring
    # "fides" is absent, which would be a false requirement here.
    assert "target: postgres@127.0.0.1:5442/fides" in out
    assert "postgres:fides@" not in out, "the password must never be printed"
    assert "postgresql://" not in out, "the raw connection URL must never be printed"
    assert out.index("target:") < out.index("created:"), (
        "the target must be printed before the summary, so a wrong-database "
        "run is visible even if it fails before the summary prints"
    )
    assert "created: 2" in out
    assert "updated: 0" in out
    assert "unchanged: 0" in out
    # F1: the label is "_in_register" — this is the register FILE's gap, not
    # a live count — and the cycle block carries a heading naming it as the
    # without-mapping distribution.
    assert "without_data_mapping_in_register: 2" in out
    assert "without_data_mapping: 2" not in out, (
        "the old, ambiguous label must not still be printed"
    )
    assert "blank_applicability: 0" in out
    assert "by business cycle" in out
    assert "cycle  Finance & Accounting: 2" in out
    assert "DRY RUN" in out
    assert "COMMITTED" not in out

    count = db.execute(
        sqlalchemy.text(
            "SELECT COUNT(*) FROM privacycare_business_process WHERE external_ref = ANY(:refs)"
        ),
        {"refs": [ref1, ref2]},
    ).scalar()
    assert count == 0, "a dry run must not write anything"


def test_cli_commit_flag_commits(tmp_path, monkeypatch, capsys):
    # In-process (not subprocess) so Session.commit can be monkeypatched
    # before main() runs. The patched commit records the call and then
    # rolls back for real, so this test never actually persists anything —
    # same technique test_taxonomy_loader.py's CLI tests use for the dry-run
    # side; here it is the only way to prove --commit calls commit() at all
    # without letting a test really commit.
    ref = _ref(1)
    register = _register([{"number": ref, "name": "A", "business_cycle": "Finance"}])
    path = tmp_path / "register.json"
    path.write_text(json.dumps(register))

    cli = _load_cli_module()

    calls = []

    def fake_commit(self):
        calls.append(True)
        self.rollback()

    monkeypatch.setattr(Session, "commit", fake_commit)

    rc = cli.main([str(path), "--commit"])

    assert rc == 0
    assert len(calls) == 1
    out = capsys.readouterr().out
    assert "COMMITTED" in out
