# The ROPA is the whole point: for one business process, everything it
# processes, on what basis, in which system. Reads across PrivacyCare's
# process layer and Fides' declarations.
import pytest
import sqlalchemy
from sqlalchemy.orm import Session

from fides.api.privacycare.models import BusinessProcess, ProcessDeclaration
from fides.api.privacycare.ropa import (
    NATURAL_PERSON_ROLE_NOT_RECORDED,
    ropa_for_process,
)
from fides.api.privacycare.taxonomy.loader import load_kenyan_taxonomy


@pytest.fixture
def db():
    engine = sqlalchemy.create_engine(
        "postgresql://postgres:fides@127.0.0.1:5442/fides"
    )
    with Session(engine) as session:
        yield session
        session.rollback()


def test_a_process_with_no_declarations_is_a_valid_empty_ropa(db):
    proc = BusinessProcess(name="Newly Identified Process")
    db.add(proc)
    db.flush()
    entry = ropa_for_process(db, proc.id)
    assert entry.process.name == "Newly Identified Process"
    assert entry.declarations == []
    assert entry.missing_declarations == []


def test_a_link_to_a_vanished_declaration_is_reported_not_crashed(db):
    proc = BusinessProcess(name="Process With Stale Link")
    db.add(proc)
    db.flush()
    db.add(
        ProcessDeclaration(
            business_process_id=proc.id,
            privacy_declaration_id="decl_that_does_not_exist",
        )
    )
    db.flush()
    entry = ropa_for_process(db, proc.id)
    assert entry.declarations == []
    assert entry.missing_declarations == ["decl_that_does_not_exist"], (
        "a link whose declaration is gone must surface, not vanish silently"
    )


def test_unknown_process_raises(db):
    with pytest.raises(LookupError):
        ropa_for_process(db, "no-such-process")


def test_a_real_declaration_round_trips_with_system_and_categories(db):
    db.execute(
        sqlalchemy.text(
            """
            INSERT INTO ctl_systems (id, fides_key, name)
            VALUES (:id, :fides_key, :name)
            """
        ),
        {
            "id": "sys_ropa_test",
            "fides_key": "sys_ropa_test_key",
            "name": "Loan Origination System",
        },
    )
    db.execute(
        sqlalchemy.text(
            """
            INSERT INTO privacydeclaration (
                id, data_use, data_categories, data_subjects,
                legal_basis_for_processing, retention_period, system_id
            )
            VALUES (
                :id, :data_use, :data_categories, :data_subjects,
                :legal_basis_for_processing, :retention_period, :system_id
            )
            """
        ),
        {
            "id": "decl_ropa_test",
            "data_use": "essential.service.operations",
            "data_categories": [
                "user.contact.email",
                "user.financial.account_number",
            ],
            "data_subjects": ["customer", "employee"],
            "legal_basis_for_processing": "Contract",
            "retention_period": "7 years",
            "system_id": "sys_ropa_test",
        },
    )
    proc = BusinessProcess(name="Loan Origination")
    db.add(proc)
    db.flush()
    db.add(
        ProcessDeclaration(
            business_process_id=proc.id,
            privacy_declaration_id="decl_ropa_test",
        )
    )
    db.flush()

    entry = ropa_for_process(db, proc.id)

    assert entry.missing_declarations == []
    assert len(entry.declarations) == 1
    decl = entry.declarations[0]
    assert decl.id == "decl_ropa_test"
    assert decl.data_use == "essential.service.operations"
    assert decl.data_categories == [
        "user.contact.email",
        "user.financial.account_number",
    ]
    assert decl.data_subjects == ["customer", "employee"]
    assert decl.legal_basis == "Contract"
    assert decl.retention_period == "7 years"
    assert decl.system_id == "sys_ropa_test"
    assert decl.system_name == "Loan Origination System"


def test_a_mix_of_real_and_missing_links_does_not_interfere(db):
    db.execute(
        sqlalchemy.text(
            """
            INSERT INTO ctl_systems (id, fides_key, name)
            VALUES (:id, :fides_key, :name)
            """
        ),
        {
            "id": "sys_ropa_mixed",
            "fides_key": "sys_ropa_mixed_key",
            "name": "Mixed Test System",
        },
    )
    db.execute(
        sqlalchemy.text(
            """
            INSERT INTO privacydeclaration (
                id, data_use, data_categories, data_subjects,
                legal_basis_for_processing, retention_period, system_id
            )
            VALUES (
                :id, :data_use, :data_categories, :data_subjects,
                :legal_basis_for_processing, :retention_period, :system_id
            )
            """
        ),
        {
            "id": "decl_ropa_mixed",
            "data_use": "essential.service.operations",
            "data_categories": ["user.contact.email", "user.device.cookie_id"],
            "data_subjects": ["customer"],
            "legal_basis_for_processing": "Legitimate interests",
            "retention_period": "3 years",
            "system_id": "sys_ropa_mixed",
        },
    )
    proc = BusinessProcess(name="Process With One Real And One Missing Link")
    db.add(proc)
    db.flush()
    db.add(
        ProcessDeclaration(
            business_process_id=proc.id,
            privacy_declaration_id="decl_ropa_mixed",
        )
    )
    db.add(
        ProcessDeclaration(
            business_process_id=proc.id,
            privacy_declaration_id="decl_ropa_mixed_missing",
        )
    )
    db.flush()

    entry = ropa_for_process(db, proc.id)

    assert len(entry.declarations) == 1
    assert entry.declarations[0].id == "decl_ropa_mixed"
    assert entry.missing_declarations == ["decl_ropa_mixed_missing"]


def test_ropa_entry_carries_special_category_and_natural_person_role(db):
    # Task 4: the ROPA read must show the SAME derived answer as
    # context.py's build_context, plus a per-data-subject role — recorded
    # for a subject the Kenyan taxonomy loaded (employee), and the literal
    # not-yet-recorded placeholder for one it never saw.
    load_kenyan_taxonomy(db)
    db.execute(
        sqlalchemy.text(
            """
            INSERT INTO ctl_systems (id, fides_key, name)
            VALUES (:id, :fides_key, :name)
            """
        ),
        {
            "id": "sys_ropa_special",
            "fides_key": "sys_ropa_special_key",
            "name": "Clinic Records",
        },
    )
    db.execute(
        sqlalchemy.text(
            """
            INSERT INTO privacydeclaration (
                id, data_use, data_categories, data_subjects,
                legal_basis_for_processing, retention_period, system_id,
                processes_special_category_data
            )
            VALUES (
                :id, :data_use, :data_categories, :data_subjects,
                :legal_basis_for_processing, :retention_period, :system_id,
                :processes_special_category_data
            )
            """
        ),
        {
            "id": "decl_ropa_special",
            "data_use": "essential.service.operations",
            "data_categories": ["user.health_and_medical.hiv_status"],
            "data_subjects": ["employee", "sys_ropa_special_unmapped_subject"],
            "legal_basis_for_processing": "Consent",
            "retention_period": "7 years",
            "system_id": "sys_ropa_special",
            "processes_special_category_data": False,
        },
    )
    proc = BusinessProcess(name="Clinic Records Process")
    db.add(proc)
    db.flush()
    db.add(
        ProcessDeclaration(
            business_process_id=proc.id,
            privacy_declaration_id="decl_ropa_special",
        )
    )
    db.flush()

    entry = ropa_for_process(db, proc.id)

    assert len(entry.declarations) == 1
    decl = entry.declarations[0]
    assert decl.special_category_derived is True
    assert decl.special_category_triggering_keys == [
        "user.health_and_medical.hiv_status"
    ]
    assert decl.special_category_mismatch is True
    assert decl.natural_person_roles == ["employee", NATURAL_PERSON_ROLE_NOT_RECORDED]


def test_declaration_order_is_stable_across_calls(db):
    # I5: the declaration query has no ORDER BY, so two renderings of the
    # same Article 30 record could order entries differently and diff
    # spuriously in a regulatory artifact. Deliberately insert declarations
    # with ids that would sort differently than insertion order under
    # anything but an explicit ORDER BY, to catch a regression back to
    # whatever order Postgres happens to return.
    db.execute(
        sqlalchemy.text(
            """
            INSERT INTO ctl_systems (id, fides_key, name)
            VALUES (:id, :fides_key, :name)
            """
        ),
        {
            "id": "sys_ropa_order",
            "fides_key": "sys_ropa_order_key",
            "name": "Order Stability Test System",
        },
    )
    declaration_ids = ["decl_ropa_order_c", "decl_ropa_order_a", "decl_ropa_order_b"]
    for decl_id in declaration_ids:
        db.execute(
            sqlalchemy.text(
                """
                INSERT INTO privacydeclaration (
                    id, data_use, data_categories, data_subjects,
                    legal_basis_for_processing, retention_period, system_id
                )
                VALUES (
                    :id, :data_use, :data_categories, :data_subjects,
                    :legal_basis_for_processing, :retention_period, :system_id
                )
                """
            ),
            {
                "id": decl_id,
                "data_use": "essential.service.operations",
                "data_categories": ["user.contact.email"],
                "data_subjects": ["customer"],
                "legal_basis_for_processing": "Contract",
                "retention_period": "1 year",
                "system_id": "sys_ropa_order",
            },
        )
    proc = BusinessProcess(name="Process With Multiple Linked Declarations")
    db.add(proc)
    db.flush()
    for decl_id in declaration_ids:
        db.add(
            ProcessDeclaration(
                business_process_id=proc.id,
                privacy_declaration_id=decl_id,
            )
        )
    db.flush()

    first_call = [d.id for d in ropa_for_process(db, proc.id).declarations]
    second_call = [d.id for d in ropa_for_process(db, proc.id).declarations]

    assert first_call == second_call, (
        "two renderings of the same ROPA entry ordered its declarations "
        "differently"
    )
    assert first_call == sorted(declaration_ids), (
        "declarations must come back ordered by id, not insertion or "
        "database-arbitrary order"
    )
