"""The data-mapping capture route (plan 20, Task 4) — the task this whole
plan exists for. Of the customer's 86 business processes, exactly ONE has
any data mapping; this route is how the rest gets one.

Follows test_api_screening.py's own idiom: routes and the mapping module's
own save_mapping are called directly as plain functions against a
rolled-back session (no TestClient). Real vocabulary values are used
throughout — this customer's own loaded data subjects, data categories and
processing grounds — never a fixture-invented term, per this task's own
"never invent the customer's content" constraint.

THE RULING THIS FILE PROVES (overriding the plan's own "mapping a process
twice updates rather than creating a second activity" wording — see
screening/mapping.py's module docstring for the full argument): a
measurement during this plan found a real business process already
carrying TWO real, unrelated activities. Idempotency here is keyed to the
ONE activity THIS ROUTE created for a process, never to the process itself
— test_a_resubmit_never_touches_a_route_unrelated_activity_on_the_same_process
below is what proves a re-submit cannot clobber an activity the route did
not create.
"""
import uuid

import pytest
import sqlalchemy
from fastapi import HTTPException
from sqlalchemy.orm import Session

from fides.api.privacycare import tasks as tasks_module
from fides.api.privacycare.api.screening import save_data_mapping
from fides.api.privacycare.api.screening_schemas import DataMappingRequest
from fides.api.privacycare.context import select_targets
from fides.api.privacycare.screening.gate import record_decision
from fides.api.privacycare.screening.mapping import MAPPING_ROUTE_FEATURE_MARKER
from tests.privacycare.test_api_assessments import _fake_client

DB_URL = "postgresql://postgres:fides@127.0.0.1:5442/fides"

# Same six trigger keys, same display order, as test_api_screening.py's own
# TRIGGER_KEYS — this file only needs a real trigger key to mark a process
# applicable for the "loop closes" test, not Carol's exact wording.
TRIGGER_KEYS = (
    "large_scale",
    "special_category",
    "systematic_monitoring",
    "new_technology",
    "automated_decision",
    "vulnerable_subjects",
)

# Her own vocabulary, measured live: exactly the values used in this file
# are real rows in ctl_data_subjects/ctl_data_categories/
# privacycare_processing_ground. Validation is against the FULL active
# taxonomy (fix round 1, item 1) — is_default = false rows (her own loaded
# additions) AND is_default = true rows (fideslang's own shipped defaults,
# which is what BOTH real live activities on bp_94d5439ced86 actually use)
# both count; only a fides_key absent from the table altogether is rejected.
REAL_SUBJECT = "auditor"  # her own loaded addition (is_default = false)
REAL_CATEGORY = "user.financial.income"
REAL_SPECIAL_CATEGORY = "user.health_and_medical.hiv_status"
REAL_GROUND = "KYC Requirements"
REAL_GROUND_LEGAL_BASIS = "Legitimate interests"
# A real ground with NO fides_legal_basis determined yet (D-KT-4) — Carol
# has not ruled on it, so nothing can be derived from it.
REAL_GROUND_WITHOUT_LEGAL_BASIS = "Research"


@pytest.fixture
def db(monkeypatch):
    engine = sqlalchemy.create_engine(DB_URL)
    with Session(engine) as session:
        # NEVER a no-op: base_class.persist_obj does add/commit/refresh, and
        # a no-op commit starves refresh(). flush() gives write visibility
        # within the transaction without making it durable; rollback() on
        # teardown discards everything this test wrote.
        monkeypatch.setattr(session, "commit", session.flush)
        yield session
        session.rollback()


@pytest.fixture
def triggers(db):
    """Ensures the six trigger rows this file's "loop closes" test needs
    exist — a no-op against Carol's six real seeded rows (Task 5 of plan
    18), a real insert only against a from-empty database. Mirrors
    test_api_screening.py's own `triggers` fixture exactly."""
    for order, key in enumerate(TRIGGER_KEYS, start=1):
        db.execute(
            sqlalchemy.text(
                "INSERT INTO privacycare_screening_trigger "
                "(id, trigger_key, label, description, display_order) "
                "VALUES (:id, :key, :label, :description, :order) "
                "ON CONFLICT (trigger_key) DO NOTHING"
            ),
            {
                "id": str(uuid.uuid4()),
                "key": key,
                "label": key.replace("_", " ").title(),
                "description": f"Test description for {key}.",
                "order": order,
            },
        )
    return TRIGGER_KEYS


def _seed_business_process(db, name: str, business_cycle: str) -> str:
    """Seeds one of the customer's own business processes (her register,
    not a generic fixture name). Same helper, same reasoning, as
    test_api_screening.py's own _seed_business_process."""
    process_id = f"bp_{uuid.uuid4().hex[:12]}"
    db.execute(
        sqlalchemy.text(
            "INSERT INTO privacycare_business_process (id, name, business_cycle) "
            "VALUES (:id, :name, :cycle)"
        ),
        {"id": process_id, "name": name, "cycle": business_cycle},
    )
    return process_id


def _seed_unrelated_activity(db, business_process_id: str, name: str, data_use: str) -> str:
    """Seeds a real-shaped activity linked to a process WITHOUT this
    route's marker — this is what a pre-existing, route-unowned activity
    looks like (mirrors the live bp_94d5439ced86 scenario: two real
    activities, "Fuel card marketing campaigns" and "Fuel card account
    administration", that a naive process-keyed idempotency would clobber).
    Needs a system (system_id is NOT NULL on privacydeclaration), seeded
    inline rather than via test_context's _seed_system so this file has no
    cross-module fixture dependency."""
    system_id = f"sys_{uuid.uuid4().hex[:8]}"
    db.execute(
        sqlalchemy.text(
            "INSERT INTO ctl_systems (id, fides_key, name) "
            "VALUES (:id, :fides_key, :name)"
        ),
        {"id": system_id, "fides_key": system_id, "name": name},
    )
    decl_id = f"pri_{uuid.uuid4()}"
    db.execute(
        sqlalchemy.text(
            "INSERT INTO privacydeclaration "
            "(id, name, data_use, data_categories, data_subjects, system_id, "
            " legal_basis_for_processing, retention_period) "
            "VALUES (:id, :name, :data_use, :categories, :subjects, :system_id, "
            " :legal_basis, :retention)"
        ),
        {
            "id": decl_id,
            "name": name,
            "data_use": data_use,
            "categories": ["user.contact.email"],
            "subjects": ["customer"],
            "system_id": system_id,
            "legal_basis": "Consent",
            "retention": "2 years",
        },
    )
    db.execute(
        sqlalchemy.text(
            "INSERT INTO privacycare_process_declaration "
            "(id, business_process_id, privacy_declaration_id) "
            "VALUES (:id, :process_id, :declaration_id)"
        ),
        {
            "id": f"pd_{uuid.uuid4().hex[:12]}",
            "process_id": business_process_id,
            "declaration_id": decl_id,
        },
    )
    return decl_id


def _snapshot(db, declaration_id: str) -> dict:
    row = db.execute(
        sqlalchemy.text(
            "SELECT name, data_use, data_categories, data_subjects, "
            "       legal_basis_for_processing, retention_period, third_parties, "
            "       processes_special_category_data, system_id, features, updated_at "
            "FROM privacydeclaration WHERE id = :id"
        ),
        {"id": declaration_id},
    ).mappings().first()
    return dict(row)


def _activity_count_for_process(db, business_process_id: str) -> int:
    return db.execute(
        sqlalchemy.text(
            "SELECT count(*) FROM privacycare_process_declaration "
            "WHERE business_process_id = :id"
        ),
        {"id": business_process_id},
    ).scalar()


@pytest.fixture
def business_process_id(db) -> str:
    return _seed_business_process(db, "Fuel Card Issuance", "Card Operations")


def _save(db, business_process_id, **overrides):
    body = dict(name="Fuel card KYC verification", data_categories=[REAL_CATEGORY])
    body.update(overrides)
    return save_data_mapping(
        business_process_id, DataMappingRequest(**body), db=db,
        client=_fake_client("carol@example.com"),
    )


# --- A complete mapping ----------------------------------------------------


def test_a_complete_mapping_creates_an_activity_and_the_link(db, business_process_id):
    response = save_data_mapping(
        business_process_id,
        DataMappingRequest(
            name="Fuel card KYC verification",
            data_categories=[REAL_CATEGORY],
            data_subjects=[REAL_SUBJECT],
            ground=REAL_GROUND,
            purpose="Verify a fuel card applicant's identity before enrolment.",
            retention_period="7 years",
            third_parties="Credit reference bureau",
        ),
        db=db,
        client=_fake_client("carol@example.com"),
    )

    assert response.created is True
    assert response.business_process_id == business_process_id
    assert response.name == "Fuel card KYC verification"
    assert response.data_categories == [REAL_CATEGORY]
    assert response.data_subjects == [REAL_SUBJECT]
    assert response.fides_legal_basis == REAL_GROUND_LEGAL_BASIS
    assert response.purpose == "Verify a fuel card applicant's identity before enrolment."
    assert response.retention_period == "7 years"
    assert response.third_parties == "Credit reference bureau"
    assert response.processes_special_category_data is False
    assert response.system_id

    link = db.execute(
        sqlalchemy.text(
            "SELECT privacy_declaration_id FROM privacycare_process_declaration "
            "WHERE business_process_id = :id"
        ),
        {"id": business_process_id},
    ).scalar()
    assert link == response.privacy_declaration_id

    row = _snapshot(db, response.privacy_declaration_id)
    assert row["name"] == "Fuel card KYC verification"
    assert row["legal_basis_for_processing"] == REAL_GROUND_LEGAL_BASIS
    assert MAPPING_ROUTE_FEATURE_MARKER in row["features"]


# --- The legal basis is derived, never accepted ----------------------------


def test_the_legal_basis_is_derived_from_the_ground_and_cannot_be_set_by_the_client(
    db, business_process_id
):
    # DataMappingRequest carries no field a caller could use to set the
    # legal basis directly — passing one is silently dropped by pydantic's
    # default extra="ignore", exactly like ScreeningDecisionRequest's own
    # dpia_required guard (test_api_screening.py's
    # test_a_caller_cannot_pass_dpia_required).
    request = DataMappingRequest(
        name="Fuel card KYC verification",
        data_categories=[REAL_CATEGORY],
        ground=REAL_GROUND,
        legal_basis_for_processing="Consent",
        fides_legal_basis="Consent",
    )
    assert not hasattr(request, "legal_basis_for_processing")
    assert not hasattr(request, "fides_legal_basis")

    response = save_data_mapping(
        business_process_id, request, db=db,
        client=_fake_client("carol@example.com"),
    )

    # The ground is "Legitimate interests" — if the client's smuggled
    # "Consent" had been honoured, this would read "Consent" instead.
    assert response.fides_legal_basis == REAL_GROUND_LEGAL_BASIS


def test_a_ground_with_no_determined_legal_basis_is_rejected(db, business_process_id):
    with pytest.raises(HTTPException) as caught:
        _save(db, business_process_id, ground=REAL_GROUND_WITHOUT_LEGAL_BASIS)
    assert caught.value.status_code == 400
    assert REAL_GROUND_WITHOUT_LEGAL_BASIS in caught.value.detail


# --- Rejected by name -------------------------------------------------------


def test_an_unknown_data_subject_is_rejected_by_name(db, business_process_id):
    with pytest.raises(HTTPException) as caught:
        _save(db, business_process_id, data_subjects=["not_a_real_subject"])
    assert caught.value.status_code == 400
    assert "not_a_real_subject" in caught.value.detail


def test_an_unknown_data_category_is_rejected_by_name(db, business_process_id):
    with pytest.raises(HTTPException) as caught:
        _save(db, business_process_id, data_categories=["not.a.real.category"])
    assert caught.value.status_code == 400
    assert "not.a.real.category" in caught.value.detail


def test_a_mapping_using_only_default_fideslang_values_succeeds(db, business_process_id):
    # Fix round 1, item 1: "customer" (ctl_data_subjects, is_default=true)
    # and "user.contact.email" (ctl_data_categories, is_default=true) are
    # BOTH real Fides-shipped defaults, not one of her loaded-for-her rows —
    # and are EXACTLY what both real live activities on bp_94d5439ced86
    # actually use. The first cut of this route rejected this by name,
    # which rejected the only data mapping the customer actually has.
    # Validation is now against the full active taxonomy, defaults
    # included; this must succeed.
    response = save_data_mapping(
        business_process_id,
        DataMappingRequest(
            name="Fuel card account administration",
            data_categories=["user.contact.email"],
            data_subjects=["customer"],
        ),
        db=db,
        client=_fake_client("carol@example.com"),
    )
    assert response.created is True
    assert response.data_subjects == ["customer"]
    assert response.data_categories == ["user.contact.email"]


def test_an_unknown_ground_is_rejected_by_name(db, business_process_id):
    with pytest.raises(HTTPException) as caught:
        _save(db, business_process_id, ground="Not One Of Her Grounds")
    assert caught.value.status_code == 400
    assert "Not One Of Her Grounds" in caught.value.detail


def test_mapping_against_an_unknown_business_process_is_404(db):
    with pytest.raises(HTTPException) as caught:
        _save(db, str(uuid.uuid4()))
    assert caught.value.status_code == 404


# --- Special-category data is derived, never ticked ------------------------


def test_special_category_data_is_flagged_automatically_when_a_special_category_is_chosen(
    db, business_process_id
):
    response = _save(db, business_process_id, data_categories=[REAL_SPECIAL_CATEGORY])
    assert response.processes_special_category_data is True


def test_ordinary_category_data_is_not_flagged_special(db, business_process_id):
    response = _save(db, business_process_id, data_categories=[REAL_CATEGORY])
    assert response.processes_special_category_data is False


# --- A partial mapping is savable ------------------------------------------


def test_a_partial_mapping_saves_with_only_a_name_and_a_category(db, business_process_id):
    response = save_data_mapping(
        business_process_id,
        DataMappingRequest(name="Fuel card KYC verification", data_categories=[REAL_CATEGORY]),
        db=db,
        client=_fake_client("carol@example.com"),
    )

    assert response.created is True
    assert response.name == "Fuel card KYC verification"
    assert response.data_categories == [REAL_CATEGORY]
    assert response.data_subjects == []
    assert response.ground is None
    assert response.fides_legal_basis is None
    assert response.purpose is None
    assert response.retention_period is None
    assert response.third_parties is None


def test_a_missing_name_is_rejected(db, business_process_id):
    with pytest.raises(HTTPException) as caught:
        _save(db, business_process_id, name="   ")
    assert caught.value.status_code == 400


def test_an_empty_data_categories_list_is_rejected(db, business_process_id):
    # DataMappingRequest itself enforces min_length=1 at the schema layer —
    # this proves the enforcement actually rejects an empty list rather than
    # silently accepting one that slipped past validation some other way.
    with pytest.raises(Exception):
        DataMappingRequest(name="Fuel card KYC verification", data_categories=[])


# --- Idempotency: keyed to the activity, never to the process --------------


def test_resubmitting_a_mapping_updates_the_same_activity_not_a_second_one(
    db, business_process_id
):
    first = save_data_mapping(
        business_process_id,
        DataMappingRequest(name="Fuel card KYC verification", data_categories=[REAL_CATEGORY]),
        db=db,
        client=_fake_client("carol@example.com"),
    )
    assert first.created is True

    second = save_data_mapping(
        business_process_id,
        DataMappingRequest(
            name="Fuel card KYC verification",
            data_categories=[REAL_CATEGORY],
            data_subjects=[REAL_SUBJECT],
            ground=REAL_GROUND,
            retention_period="7 years",
        ),
        db=db,
        client=_fake_client("carol@example.com"),
    )

    assert second.created is False
    assert second.privacy_declaration_id == first.privacy_declaration_id
    assert second.data_subjects == [REAL_SUBJECT]
    assert second.fides_legal_basis == REAL_GROUND_LEGAL_BASIS
    assert second.retention_period == "7 years"
    assert _activity_count_for_process(db, business_process_id) == 1


def test_fields_left_unanswered_on_a_resubmit_are_not_erased(db, business_process_id):
    save_data_mapping(
        business_process_id,
        DataMappingRequest(
            name="Fuel card KYC verification",
            data_categories=[REAL_CATEGORY],
            ground=REAL_GROUND,
            third_parties="Credit reference bureau",
        ),
        db=db,
        client=_fake_client("carol@example.com"),
    )

    # Resubmit naming only name/data_categories (the two always-required
    # fields) — ground/third_parties are NOT repeated, and must survive.
    second = save_data_mapping(
        business_process_id,
        DataMappingRequest(
            name="Fuel card KYC verification — updated",
            data_categories=[REAL_CATEGORY, REAL_SPECIAL_CATEGORY],
        ),
        db=db,
        client=_fake_client("carol@example.com"),
    )

    assert second.name == "Fuel card KYC verification — updated"
    assert second.fides_legal_basis == REAL_GROUND_LEGAL_BASIS
    assert second.third_parties == "Credit reference bureau"
    assert second.processes_special_category_data is True


def test_a_resubmit_never_touches_a_route_unrelated_activity_on_the_same_process(
    db, business_process_id
):
    # Mirrors the live bp_94d5439ced86 scenario, self-seeded per this plan's
    # own ruling (seed your own links; do not rely on shared live data).
    marketing_id = _seed_unrelated_activity(
        db, business_process_id, "Fuel card marketing campaigns", "marketing.advertising"
    )
    admin_id = _seed_unrelated_activity(
        db, business_process_id, "Fuel card account administration",
        "essential.service.payment_processing",
    )
    before_marketing = _snapshot(db, marketing_id)
    before_admin = _snapshot(db, admin_id)

    first = save_data_mapping(
        business_process_id,
        DataMappingRequest(name="Fuel card KYC verification", data_categories=[REAL_CATEGORY]),
        db=db,
        client=_fake_client("carol@example.com"),
    )
    assert first.created is True
    assert first.privacy_declaration_id not in (marketing_id, admin_id)

    # Resubmit — this must update ONLY the route's own activity.
    second = save_data_mapping(
        business_process_id,
        DataMappingRequest(
            name="Fuel card KYC verification",
            data_categories=[REAL_CATEGORY],
            ground=REAL_GROUND,
        ),
        db=db,
        client=_fake_client("carol@example.com"),
    )
    assert second.created is False
    assert second.privacy_declaration_id == first.privacy_declaration_id

    after_marketing = _snapshot(db, marketing_id)
    after_admin = _snapshot(db, admin_id)
    assert after_marketing == before_marketing, (
        "the route-unrelated marketing activity must never be touched"
    )
    assert after_admin == before_admin, (
        "the route-unrelated account-administration activity must never be touched"
    )
    assert _activity_count_for_process(db, business_process_id) == 3


# --- The loop closes ---------------------------------------------------


def test_the_created_activity_is_immediately_assessable_proving_the_loop_closes(
    db, triggers, business_process_id
):
    # Step 1: mark the process applicable.
    record_decision(
        db,
        business_process_id=business_process_id,
        triggered_keys=["large_scale"],
        justification=None,
        decided_by="carol@example.com",
    )

    # Step 2: capture the mapping.
    mapping = save_data_mapping(
        business_process_id,
        DataMappingRequest(
            name="Fuel card KYC verification",
            data_categories=[REAL_CATEGORY],
            data_subjects=[REAL_SUBJECT],
            ground=REAL_GROUND,
        ),
        db=db,
        client=_fake_client("carol@example.com"),
    )

    # Step 3: the activity the mapping just created shows up as a
    # generation target — the same read path run_generation itself uses.
    targets = select_targets(db, None, high_risk_only=False)
    matching = [t for t in targets if t.declaration_id == mapping.privacy_declaration_id]
    assert len(matching) == 1
    assert matching[0].data_categories == [REAL_CATEGORY]

    # Step 4: the generation gate — resolving THIS activity through the
    # link this route just wrote, to the process's verdict this route just
    # read — says it is NOT screened out, i.e. generation would proceed.
    assert tasks_module._is_activity_screened_out(db, mapping.privacy_declaration_id) is False


def test_an_unmapped_but_screened_out_process_would_still_be_skipped(
    db, triggers, business_process_id
):
    # The mirror image of the test above, proving the gate this route feeds
    # is the SAME gate — mark the process NOT applicable and show the same
    # activity now resolves as screened out.
    record_decision(
        db,
        business_process_id=business_process_id,
        triggered_keys=[],
        justification="No special category or high-volume processing involved.",
        decided_by="carol@example.com",
    )
    mapping = save_data_mapping(
        business_process_id,
        DataMappingRequest(name="Fuel card KYC verification", data_categories=[REAL_CATEGORY]),
        db=db,
        client=_fake_client("carol@example.com"),
    )

    assert tasks_module._is_activity_screened_out(db, mapping.privacy_declaration_id) is True


# --- Ground provenance is written, not just the derived value --------------


def _declaration_ground_row(db, declaration_id: str) -> dict | None:
    row = db.execute(
        sqlalchemy.text(
            "SELECT processing_ground_id, recorded_by "
            "FROM privacycare_declaration_ground WHERE privacy_declaration_id = :id"
        ),
        {"id": declaration_id},
    ).mappings().first()
    return dict(row) if row is not None else None


def test_the_ground_provenance_is_recorded_alongside_the_derived_legal_basis(
    db, business_process_id
):
    response = save_data_mapping(
        business_process_id,
        DataMappingRequest(
            name="Fuel card KYC verification",
            data_categories=[REAL_CATEGORY],
            ground=REAL_GROUND,
        ),
        db=db,
        client=_fake_client("carol@example.com"),
    )

    ground_id = db.execute(
        sqlalchemy.text("SELECT id FROM privacycare_processing_ground WHERE ground = :g"),
        {"g": REAL_GROUND},
    ).scalar()
    row = _declaration_ground_row(db, response.privacy_declaration_id)
    assert row is not None, "no privacycare_declaration_ground row was written"
    assert row["processing_ground_id"] == ground_id
    assert row["recorded_by"] == "carol@example.com"


def test_no_provenance_is_written_when_no_ground_is_given(db, business_process_id):
    response = save_data_mapping(
        business_process_id,
        DataMappingRequest(name="Fuel card KYC verification", data_categories=[REAL_CATEGORY]),
        db=db,
        client=_fake_client("carol@example.com"),
    )
    assert _declaration_ground_row(db, response.privacy_declaration_id) is None


def test_resubmitting_with_a_different_ground_updates_the_provenance_not_duplicates_it(
    db, business_process_id
):
    # Two real grounds sharing the same legal-basis class, so a plain
    # equality check on fides_legal_basis alone could not tell them apart —
    # only the recorded processing_ground_id can.
    first = save_data_mapping(
        business_process_id,
        DataMappingRequest(
            name="Fuel card KYC verification", data_categories=[REAL_CATEGORY], ground=REAL_GROUND,
        ),
        db=db,
        client=_fake_client("carol@example.com"),
    )
    first_ground_id = db.execute(
        sqlalchemy.text("SELECT id FROM privacycare_processing_ground WHERE ground = :g"),
        {"g": REAL_GROUND},
    ).scalar()
    assert _declaration_ground_row(db, first.privacy_declaration_id)["processing_ground_id"] == first_ground_id

    other_ground = "Customer Relationship Administration"  # real row, also "Legitimate interests"
    second = save_data_mapping(
        business_process_id,
        DataMappingRequest(
            name="Fuel card KYC verification", data_categories=[REAL_CATEGORY], ground=other_ground,
        ),
        db=db,
        client=_fake_client("carol@example.com"),
    )
    assert second.privacy_declaration_id == first.privacy_declaration_id

    other_ground_id = db.execute(
        sqlalchemy.text("SELECT id FROM privacycare_processing_ground WHERE ground = :g"),
        {"g": other_ground},
    ).scalar()
    rows = db.execute(
        sqlalchemy.text(
            "SELECT count(*) FROM privacycare_declaration_ground "
            "WHERE privacy_declaration_id = :id"
        ),
        {"id": first.privacy_declaration_id},
    ).scalar()
    assert rows == 1, "resubmitting with a different ground must update, not duplicate"
    updated_row = _declaration_ground_row(db, first.privacy_declaration_id)
    assert updated_row["processing_ground_id"] == other_ground_id


# --- System provisioning: lazy, per-process, marked and removable ----------


def _system_row(db, system_id: str) -> dict:
    row = db.execute(
        sqlalchemy.text("SELECT fides_key, tags FROM ctl_systems WHERE id = :id"),
        {"id": system_id},
    ).mappings().first()
    return dict(row)


def test_a_provisioned_system_is_tagged_identifiable_and_removable(db, business_process_id):
    response = save_data_mapping(
        business_process_id,
        DataMappingRequest(name="Fuel card KYC verification", data_categories=[REAL_CATEGORY]),
        db=db,
        client=_fake_client("carol@example.com"),
    )

    system = _system_row(db, response.system_id)
    assert system["fides_key"] == f"privacycare_process_{business_process_id}"
    assert MAPPING_ROUTE_FEATURE_MARKER in (system["tags"] or [])


def test_a_reused_real_system_is_never_tagged_as_route_provisioned(db, business_process_id):
    # Seed a REAL, pre-existing system for this process (mirrors the two
    # live activities sharing ctl_ef9cadb3-...) — this route must reuse it,
    # not provision a second one, and must never retroactively tag someone
    # else's real system as if this route had created it.
    real_system_id = f"sys_{uuid.uuid4().hex[:8]}"
    db.execute(
        sqlalchemy.text("INSERT INTO ctl_systems (id, fides_key, name) VALUES (:id, :key, :name)"),
        {"id": real_system_id, "key": real_system_id, "name": "Fuel Card Platform"},
    )
    _seed_unrelated_activity(
        db, business_process_id, "Fuel card marketing campaigns", "marketing.advertising"
    )
    # Point that unrelated activity's system at the REAL system, replacing
    # the one _seed_unrelated_activity provisioned for itself, so this
    # process now genuinely has exactly one real, pre-existing system.
    db.execute(
        sqlalchemy.text(
            "UPDATE privacydeclaration SET system_id = :sid "
            "WHERE id IN (SELECT privacy_declaration_id FROM privacycare_process_declaration "
            "WHERE business_process_id = :pid)"
        ),
        {"sid": real_system_id, "pid": business_process_id},
    )

    response = save_data_mapping(
        business_process_id,
        DataMappingRequest(name="Fuel card KYC verification", data_categories=[REAL_CATEGORY]),
        db=db,
        client=_fake_client("carol@example.com"),
    )

    assert response.system_id == real_system_id
    system = _system_row(db, real_system_id)
    assert MAPPING_ROUTE_FEATURE_MARKER not in (system["tags"] or [])


def test_provisioning_is_lazy_never_eager_for_every_process(db):
    # Two freshly seeded, never-mapped processes: provisioning must not
    # have happened for either until save_data_mapping is actually called
    # on one of them.
    untouched = _seed_business_process(db, "CSR Planning & Execution", "CSR")
    mapped = _seed_business_process(db, "Fraud Risk Assessments", "Audit & Risk")

    before = db.execute(
        sqlalchemy.text(
            "SELECT count(*) FROM ctl_systems WHERE fides_key = :key"
        ),
        {"key": f"privacycare_process_{untouched}"},
    ).scalar()
    assert before == 0

    save_data_mapping(
        mapped,
        DataMappingRequest(name="Fraud review", data_categories=[REAL_CATEGORY]),
        db=db,
        client=_fake_client("carol@example.com"),
    )

    still_none_for_untouched = db.execute(
        sqlalchemy.text(
            "SELECT count(*) FROM ctl_systems WHERE fides_key = :key"
        ),
        {"key": f"privacycare_process_{untouched}"},
    ).scalar()
    assert still_none_for_untouched == 0, (
        "provisioning must be lazy — mapping one process must never "
        "provision a system for a different, unmapped process"
    )
