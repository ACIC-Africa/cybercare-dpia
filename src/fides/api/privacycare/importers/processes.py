# Imports a parsed business-process register (the JSON shape produced by
# scripts that convert a customer's process-and-data-mapping workbook, e.g.
# docs/DataPrivacyManenos/converted/06_oil_marketer_process_and_data_map.json
# in the sibling LightHouse repo) into privacycare_business_process.
#
# `register` is the parsed JSON, passed in rather than read from disk — the
# core takes data, Task 2's CLI takes a path. Like taxonomy/loader.py's
# load_kenyan_taxonomy, this module NEVER calls db.commit() or db.rollback():
# it writes through the session it is given and the caller's session
# boundary decides whether those writes are kept. There is no `commit`
# kwarg and no separate "pretend" branch — a dry run is the caller rolling
# back the same session after calling this function, so it always exercises
# the real write path.
#
# Upsert, not append (D-IMP-2): the consultant re-runs this every time the
# customer revises the workbook, and a second run that doubles the register
# is worse than useless. There is no unique index on external_ref, and none
# may be added here (no migration in this task), so the match is a plain
# `SELECT ... WHERE external_ref = :ref AND deleted_at IS NULL FOR UPDATE`
# rather than an `ON CONFLICT`. If more than one live row already shares a
# ref, we refuse to guess which one to update. This is NOT only "a prior
# data anomaly": `SELECT ... FOR UPDATE` takes no lock when it matches
# nothing (there is no row yet to lock), so two runs of this importer
# started concurrently against a fresh database can both miss the match,
# both insert, and both commit — no anomaly required, just bad timing. A
# duplicate ref can also come from api/processes.py's create_business_process
# route (POST), which calls the same _create_process() this module reuses
# with whatever external_ref the caller supplies, no uniqueness check of its
# own. Either way the remedy is the same and is named in the raised error:
# an operator deletes or re-refs one of the duplicate rows so the next run
# has exactly one to match.
#
# This module never creates a privacycare_process_declaration row (D-IMP-4):
# the register describes processes, not which privacy declarations they
# process data under, and a ROPA that claims a link the customer never
# described is the one thing an import must not do.
#
# owner_name, owner_email, criticality_note and last_attested_at are never
# written here, on insert or update: the register carries none of them, and
# writing a field the source does not carry is exactly the kind of invented
# data D-IMP-4 rules out one field down. Reuse api/processes.py's
# _create_process for the insert rather than a second insert path; the
# update is this module's own sqlalchemy.text() statement, since
# _create_process only inserts.
from dataclasses import dataclass

import sqlalchemy
from sqlalchemy.orm import Session

from fides.api.privacycare.api.processes import BusinessProcessCreate, _create_process


@dataclass(frozen=True)
class ImportSummary:
    created: int
    updated: int
    unchanged: int
    without_data_mapping: int
    blank_applicability: int
    cycles: dict[str, int]


_SELECT_MATCHING_SQL = sqlalchemy.text(
    "SELECT id, name, description, business_cycle, is_critical "
    "FROM privacycare_business_process "
    "WHERE external_ref = :ref AND deleted_at IS NULL "
    "FOR UPDATE"
)

# D-IMP-2 also means "never touch a field the source does not carry" (F2):
# the register is read with `.get()`, so a `description` or `business_cycle`
# key that is simply ABSENT from a row must not become a NULL written over
# whatever is already stored. `name` and `is_critical` are always present —
# a blank name is rejected by _validate(), and is_critical is derived from
# `applicable` whether or not that key exists — so only these two columns
# ever need to be left out of the SET list. Four fixed statements (rather
# than building SQL text at runtime) cover the four presence combinations.
_UPDATE_ALL_SQL = sqlalchemy.text(
    "UPDATE privacycare_business_process "
    "SET name = :name, description = :description, "
    "    business_cycle = :business_cycle, is_critical = :is_critical, "
    "    updated_at = now() "
    "WHERE id = :id"
)

_UPDATE_NO_DESCRIPTION_SQL = sqlalchemy.text(
    "UPDATE privacycare_business_process "
    "SET name = :name, business_cycle = :business_cycle, "
    "    is_critical = :is_critical, updated_at = now() "
    "WHERE id = :id"
)

_UPDATE_NO_BUSINESS_CYCLE_SQL = sqlalchemy.text(
    "UPDATE privacycare_business_process "
    "SET name = :name, description = :description, is_critical = :is_critical, "
    "    updated_at = now() "
    "WHERE id = :id"
)

_UPDATE_NO_DESCRIPTION_NO_BUSINESS_CYCLE_SQL = sqlalchemy.text(
    "UPDATE privacycare_business_process "
    "SET name = :name, is_critical = :is_critical, updated_at = now() "
    "WHERE id = :id"
)


def _is_blank(value: str | None) -> bool:
    return value is None or not value.strip()


def _external_ref(process: dict) -> str:
    # A process with no number cannot be matched on the next run, so it is
    # malformed the same way a blank name is (see the brief's blank-name
    # rule).
    number = process.get("number")
    ref = str(number).strip() if number is not None else ""
    if not ref:
        raise ValueError(f"process {process!r} has a blank or missing 'number'")
    return ref


def _validate(processes: list[dict]) -> None:
    """Validate the whole register before any write happens, so a malformed
    file leaves the session untouched rather than half-importing (checked by
    test_a_malformed_register_writes_nothing)."""
    for process in processes:
        _external_ref(process)
        if _is_blank(process.get("name")):
            raise ValueError(f"process {process!r} has a blank or missing 'name'")


def import_processes(db: Session, register: dict) -> ImportSummary:
    """Upsert every process in `register` into privacycare_business_process.

    Never commits or rolls back — the caller's session boundary decides
    whether the writes are kept, exactly like load_kenyan_taxonomy. Matches
    existing rows on `external_ref = str(process["number"]).strip()`;
    unmatched processes are inserted via api/processes.py's _create_process.
    A matched row whose stored name/is_critical, plus description and
    business_cycle when the register row carries those keys, already equal
    the register's values is left alone and counted `unchanged`; any
    difference among the fields the register actually carries is written
    and counted `updated`. A `description` or `business_cycle` key that is
    ABSENT from a register row is never written — the existing column is
    left exactly as it was, and its value plays no part in the
    updated/unchanged comparison (F2; see the SQL constants above). A key
    that IS present with an explicit empty or None value still writes: the
    source carried it, even if what it carried was blank.

    `is_critical` is `process["applicable"] == "1"` on both insert and
    update. A blank applicability (missing, None, or whitespace-only) is
    imported as not-critical — the column is NOT NULL — and counted in
    `blank_applicability` so the unanswered prioritisation stays visible
    (D-IMP-3).

    `without_data_mapping` counts processes whose name is not in
    `register["processes_with_data_mapping_detail"]` (a list of process
    names, not numbers) — this is the register's gap, not a live count, and
    a re-run months later still reports the workbook's original number.
    `cycles` is the distribution of THOSE SAME without-mapping processes
    only, one count per `business_cycle` value as given (a blank cycle keys
    as `""`, never a made-up label) — a process that already has a mapping
    contributes to neither `without_data_mapping` nor `cycles` (F1: this
    used to count every process regardless of mapping status, which misdescribed
    the register whenever any process had a data-mapping detail already
    recorded).
    """
    processes = register.get("processes", [])
    with_mapping = set(register.get("processes_with_data_mapping_detail", []))

    _validate(processes)

    created = 0
    updated = 0
    unchanged = 0
    without_data_mapping = 0
    blank_applicability = 0
    cycles: dict[str, int] = {}

    for process in processes:
        ref = _external_ref(process)
        name = process["name"]
        description_present = "description" in process
        description = process.get("description")
        business_cycle_present = "business_cycle" in process
        business_cycle = process.get("business_cycle")
        applicable = process.get("applicable")

        if _is_blank(applicable):
            blank_applicability += 1
        is_critical = applicable == "1"

        # F1: cycles is the distribution of the WITHOUT-mapping subset only,
        # so it must be gated by the same test as without_data_mapping
        # itself, not incremented for every process regardless of mapping.
        if name not in with_mapping:
            without_data_mapping += 1
            cycle_key = business_cycle or ""
            cycles[cycle_key] = cycles.get(cycle_key, 0) + 1

        rows = db.execute(_SELECT_MATCHING_SQL, {"ref": ref}).mappings().all()
        if len(rows) > 1:
            raise ValueError(
                f"external_ref {ref!r} matches {len(rows)} live "
                "privacycare_business_process rows; cannot upsert without "
                "guessing which one to update — an operator must delete or "
                "re-ref one of the duplicate rows before the next run"
            )

        if not rows:
            _create_process(
                db,
                BusinessProcessCreate(
                    name=name,
                    description=description,
                    business_cycle=business_cycle,
                    is_critical=is_critical,
                    external_ref=ref,
                ),
            )
            created += 1
            continue

        row = rows[0]

        # F2: only compare (and only ever write) the columns the register
        # row actually carries a key for, besides name/is_critical which are
        # always present. An absent description/business_cycle key must not
        # count a row as `updated`, because it is never written.
        new_values: dict[str, object] = {"name": name, "is_critical": is_critical}
        old_values: dict[str, object] = {"name": row["name"], "is_critical": row["is_critical"]}
        if description_present:
            new_values["description"] = description
            old_values["description"] = row["description"]
        if business_cycle_present:
            new_values["business_cycle"] = business_cycle
            old_values["business_cycle"] = row["business_cycle"]

        if new_values == old_values:
            unchanged += 1
            continue

        params: dict[str, object] = {"id": row["id"], "name": name, "is_critical": is_critical}
        if description_present and business_cycle_present:
            update_sql = _UPDATE_ALL_SQL
            params["description"] = description
            params["business_cycle"] = business_cycle
        elif description_present:
            update_sql = _UPDATE_NO_BUSINESS_CYCLE_SQL
            params["description"] = description
        elif business_cycle_present:
            update_sql = _UPDATE_NO_DESCRIPTION_SQL
            params["business_cycle"] = business_cycle
        else:
            update_sql = _UPDATE_NO_DESCRIPTION_NO_BUSINESS_CYCLE_SQL

        db.execute(update_sql, params)
        updated += 1

    return ImportSummary(
        created=created,
        updated=updated,
        unchanged=unchanged,
        without_data_mapping=without_data_mapping,
        blank_applicability=blank_applicability,
        cycles=cycles,
    )
