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
import threading
import time
import uuid

import pydantic
import pytest
import sqlalchemy
from fastapi import HTTPException
from sqlalchemy.orm import Session

from fides.api.models.sql_models import (
    System as SystemModel,  # type: ignore[attr-defined]
)
from fides.api.oauth.utils import verify_oauth_client
from fides.api.privacycare import tasks as tasks_module
from fides.api.privacycare.api.router import (
    PRIVACYCARE_SCREENING_PREFIX,
    privacycare_screening_router,
)
from fides.api.privacycare.api.screening import get_data_mapping, save_data_mapping
from fides.api.privacycare.api.screening_schemas import DataMappingRequest
from fides.api.privacycare.context import select_targets
from fides.api.privacycare.screening.gate import record_decision
from fides.api.privacycare.screening.mapping import (
    MAPPING_ROUTE_FEATURE_MARKER,
    MAPPING_ROUTE_ORGANIZATION_FIDES_KEY,
    MAPPING_ROUTE_SYSTEM_TYPE,
    save_mapping,
)
from fides.api.schemas.system import BasicSystemResponse
from fides.common.scope_registry import PRIVACYCARE_SCREENING_CREATE
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
# A real ctl_data_uses fides_key (fix wave, item C-2). All 56 rows in
# ctl_data_uses are fideslang defaults — the Kenyan taxonomy load never
# added any of her own (measured) — so there is no "her own loaded
# addition" analogue here the way REAL_SUBJECT/REAL_CATEGORY have one.
REAL_DATA_USE = "essential.legal_obligation"
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


def _seed_orphan_link(db, business_process_id: str, declaration_id: str) -> None:
    """Seeds a privacycare_process_declaration row pointing at a
    privacy_declaration_id that does NOT exist — mirrors the real
    'decl_deleted_last_year' orphan row already living on bp_94d5439ced86
    in the customer's live data (a declaration deleted after the link was
    made). Same helper shape as test_api_screening.py's own
    _link_declaration, kept local per this file's own no-cross-import
    convention."""
    db.execute(
        sqlalchemy.text(
            "INSERT INTO privacycare_process_declaration "
            "(id, business_process_id, privacy_declaration_id) "
            "VALUES (:id, :process_id, :declaration_id)"
        ),
        {
            "id": f"pd_{uuid.uuid4().hex[:12]}",
            "process_id": business_process_id,
            "declaration_id": declaration_id,
        },
    )


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


@pytest.fixture
def committed_business_process_id():
    """A REAL, committed business process on its own connection — needed
    ONLY by the two-thread concurrency test (fix round 2, item I-2), which
    must see the SAME row from two independent database connections at
    once. The rolled-back `db` fixture above is a single transaction other
    connections cannot see into, so it cannot be used here. Teardown
    deletes every row the test could have created against this process
    (both possible activities, their ground-provenance rows, their
    provisioned system, and the link rows) plus the process itself, for
    real, so nothing durable survives this test.
    """
    engine = sqlalchemy.create_engine(DB_URL)
    with Session(engine) as session:
        process_id = _seed_business_process(session, "Fuel Card Issuance", "Card Operations")
        session.commit()
        try:
            yield process_id
        finally:
            declaration_ids = session.execute(
                sqlalchemy.text(
                    "SELECT privacy_declaration_id FROM privacycare_process_declaration "
                    "WHERE business_process_id = :id"
                ),
                {"id": process_id},
            ).scalars().all()
            if declaration_ids:
                session.execute(
                    sqlalchemy.text(
                        "DELETE FROM privacycare_declaration_ground "
                        "WHERE privacy_declaration_id = ANY(:ids)"
                    ),
                    {"ids": declaration_ids},
                )
            system_ids = session.execute(
                sqlalchemy.text(
                    "SELECT DISTINCT system_id FROM privacydeclaration WHERE id = ANY(:ids)"
                ),
                {"ids": declaration_ids},
            ).scalars().all() if declaration_ids else []
            session.execute(
                sqlalchemy.text(
                    "DELETE FROM privacycare_process_declaration WHERE business_process_id = :id"
                ),
                {"id": process_id},
            )
            if declaration_ids:
                session.execute(
                    sqlalchemy.text("DELETE FROM privacydeclaration WHERE id = ANY(:ids)"),
                    {"ids": declaration_ids},
                )
            if system_ids:
                session.execute(
                    sqlalchemy.text(
                        "DELETE FROM ctl_systems WHERE id = ANY(:ids) "
                        "AND :marker = ANY(tags)"
                    ),
                    {"ids": system_ids, "marker": MAPPING_ROUTE_FEATURE_MARKER},
                )
            session.execute(
                sqlalchemy.text("DELETE FROM privacycare_business_process WHERE id = :id"),
                {"id": process_id},
            )
            session.commit()


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
            # A real ctl_data_uses fides_key (fix wave, item C-2) — purpose
            # is a taxonomy key now, never free text. See
            # REAL_DATA_USE's own comment for why this one.
            purpose=REAL_DATA_USE,
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
    assert response.purpose == REAL_DATA_USE
    assert response.retention_period == "7 years"
    assert response.third_parties == "Credit reference bureau"
    assert response.processes_special_category_data is False

    # Fix round 2, item M-4: a bare truthiness check ("assert
    # response.system_id") passes for ANY non-empty string, proving only
    # that some value was set — not that it identifies the actual system
    # this process was mapped onto. Assert identity against the real
    # provisioned row instead.
    provisioned_system_id = db.execute(
        sqlalchemy.text("SELECT id FROM ctl_systems WHERE fides_key = :key"),
        {"key": f"privacycare_process_{business_process_id}"},
    ).scalar()
    assert response.system_id == provisioned_system_id

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


def test_an_unknown_purpose_is_rejected_by_name(db, business_process_id):
    # Fix wave, item C-2: purpose is a taxonomy key (ctl_data_uses), not
    # free text — the defect this test guards against is exactly what
    # produced the two invalid live rows the final whole-branch review
    # found: prose written straight into data_use, a FidesKey column.
    with pytest.raises(HTTPException) as caught:
        _save(
            db, business_process_id,
            purpose="Verify applicant identity and issue a fuel card",
        )
    assert caught.value.status_code == 400
    assert "Verify applicant identity and issue a fuel card" in caught.value.detail


def test_a_valid_purpose_taxonomy_key_is_accepted_and_persisted(db, business_process_id):
    response = _save(db, business_process_id, purpose=REAL_DATA_USE)
    assert response.purpose == REAL_DATA_USE
    row = _snapshot(db, response.privacy_declaration_id)
    assert row["data_use"] == REAL_DATA_USE


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


def test_a_partial_resubmit_wholesale_replaces_categories_and_can_drop_special_status(
    db, business_process_id
):
    # Fix round 2, item M-2. data_categories is one of the two always-
    # required, always-fully-REPLACED fields (mapping.py's own docstring,
    # "A PARTIAL MAPPING IS SAVABLE") — this is the brief's own behaviour,
    # not a bug, and it is NOT being changed here. But it is destructive
    # (a resubmit naming fewer categories drops the ones left unnamed, and
    # processes_special_category_data is recomputed from whatever survives,
    # so it can silently flip from true back to false) and, until this
    # test, nothing pinned that down — a future "be helpful, merge the
    # categories instead of replacing them" change would pass the whole
    # suite. This test exists so that change fails loudly, on purpose.
    first = save_data_mapping(
        business_process_id,
        DataMappingRequest(
            name="Fuel card KYC verification",
            data_categories=[REAL_SPECIAL_CATEGORY, REAL_CATEGORY],
        ),
        db=db,
        client=_fake_client("carol@example.com"),
    )
    assert set(first.data_categories) == {REAL_SPECIAL_CATEGORY, REAL_CATEGORY}
    assert first.processes_special_category_data is True

    second = save_data_mapping(
        business_process_id,
        DataMappingRequest(name="Fuel card KYC verification", data_categories=[REAL_CATEGORY]),
        db=db,
        client=_fake_client("carol@example.com"),
    )
    assert second.privacy_declaration_id == first.privacy_declaration_id
    assert second.data_categories == [REAL_CATEGORY]
    assert REAL_SPECIAL_CATEGORY not in second.data_categories
    assert second.processes_special_category_data is False, (
        "documented, intended behaviour: a partial resubmit replaces "
        "data_categories wholesale, and the special-category flag is "
        "recomputed from whatever survives — this must keep failing loudly "
        "if that behaviour is ever accidentally changed to a merge"
    )


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


def test_an_empty_data_categories_list_is_rejected_by_the_schema(db, business_process_id):
    # DataMappingRequest itself enforces min_length=1 at the schema layer —
    # this proves the enforcement actually rejects an empty list rather than
    # silently accepting one that slipped past validation some other way.
    # pydantic.ValidationError specifically, not a bare Exception, so this
    # cannot pass for an unrelated reason (e.g. a typo'd field name).
    with pytest.raises(pydantic.ValidationError):
        DataMappingRequest(name="Fuel card KYC verification", data_categories=[])


def test_an_empty_data_categories_list_is_rejected_by_save_mapping_itself(
    db, business_process_id
):
    # Fix round 2, item M-4. The schema-level test above never calls
    # save_mapping at all — a bare pytest.raises(Exception) around only the
    # DataMappingRequest constructor would also pass on a typo'd field
    # name, proving nothing about mapping.py's OWN "at least one data
    # category is required" guard. Call save_mapping directly, bypassing
    # the schema layer that would normally catch this first, so that guard
    # is exercised on its own terms.
    with pytest.raises(ValueError, match="at least one data category"):
        save_mapping(
            db,
            business_process_id=business_process_id,
            name="Fuel card KYC verification",
            data_categories=[],
            recorded_by="carol@example.com",
        )


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


def test_an_orphan_link_does_not_crash_or_get_treated_as_the_route_activity(
    db, business_process_id
):
    # Fix round 2, item M-3. Mirrors the real orphan row on bp_94d5439ced86
    # (privacy_declaration_id = 'decl_deleted_last_year', no matching
    # privacydeclaration row) — nothing previously seeded one, so the INNER
    # JOINs in _EXISTING_ROUTE_ACTIVITY_SQL / _ANY_SYSTEM_FOR_PROCESS_SQL
    # excluding it were never actually exercised by any test. Pinned here:
    # changing either JOIN to a LEFT JOIN must fail this test (system_id
    # would come back NULL and the subsequent privacydeclaration INSERT
    # would violate its NOT NULL constraint).
    _seed_orphan_link(db, business_process_id, "decl_deleted_last_year")

    response = save_data_mapping(
        business_process_id,
        DataMappingRequest(name="Fuel card KYC verification", data_categories=[REAL_CATEGORY]),
        db=db,
        client=_fake_client("carol@example.com"),
    )

    assert response.created is True
    assert response.system_id is not None
    # The orphan link itself is untouched — still exactly one row, still
    # pointing at the same non-existent id.
    orphan_count = db.execute(
        sqlalchemy.text(
            "SELECT count(*) FROM privacycare_process_declaration "
            "WHERE business_process_id = :id AND privacy_declaration_id = :decl"
        ),
        {"id": business_process_id, "decl": "decl_deleted_last_year"},
    ).scalar()
    assert orphan_count == 1
    # Two links now exist for this process: the orphan, and the new one.
    assert _activity_count_for_process(db, business_process_id) == 2


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

    # Step 2.5 (fix round 2, item I-1): the test's own name promises the
    # LINK the mapping wrote is what makes the rest of this test meaningful
    # — without this assertion, the test passed even with mapping.py's own
    # _INSERT_LINK_SQL deleted entirely (select_targets never touches the
    # link table, and _is_activity_screened_out defaults an unlinked
    # activity to "not screened out" by design). Assert the SAME query
    # tasks.py's own _is_activity_screened_out reads from actually returns
    # this process for this declaration, so the test earns its name rather
    # than passing for an unrelated reason.
    linked_processes = db.execute(
        tasks_module._BUSINESS_PROCESSES_FOR_DECLARATION_SQL,
        {"declaration_id": mapping.privacy_declaration_id},
    ).scalars().all()
    assert linked_processes == [business_process_id]

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


def test_a_saved_grounds_round_trips_through_get_mapping(db, business_process_id):
    # Fix wave, item I1: before this fix, get_mapping always passed
    # ground=None into its response regardless of what was persisted — the
    # UI's own Lawful basis picker then always reopened empty, even though
    # the derived fides_legal_basis it was supposed to explain WAS being
    # returned correctly. Save a mapping naming a real ground, then read it
    # back through the same GET route the screen calls, and prove the
    # ground's own text — not just its derived legal basis — comes back.
    saved = save_data_mapping(
        business_process_id,
        DataMappingRequest(
            name="Fuel card KYC verification",
            data_categories=[REAL_CATEGORY],
            ground=REAL_GROUND,
        ),
        db=db,
        client=_fake_client("carol@example.com"),
    )
    assert saved.ground == REAL_GROUND
    assert saved.fides_legal_basis == REAL_GROUND_LEGAL_BASIS

    read_back = get_data_mapping(business_process_id, db=db)

    assert read_back.mapping is not None
    assert read_back.mapping.ground == REAL_GROUND, (
        "get_mapping must resolve the ground through "
        "privacycare_declaration_ground -> privacycare_processing_ground, "
        "not echo None the way a POST with no ground argument would"
    )
    assert read_back.mapping.fides_legal_basis == REAL_GROUND_LEGAL_BASIS
    assert read_back.mapping.privacy_declaration_id == saved.privacy_declaration_id


def test_a_resubmit_that_changes_the_ground_is_reflected_on_the_next_read(
    db, business_process_id
):
    # The provenance row is an upsert keyed on privacy_declaration_id (see
    # test_resubmitting_with_a_different_ground_updates_the_provenance_not_
    # duplicates_it above) — the read side must follow that same update,
    # not keep echoing whichever ground was recorded first.
    save_data_mapping(
        business_process_id,
        DataMappingRequest(
            name="Fuel card KYC verification", data_categories=[REAL_CATEGORY], ground=REAL_GROUND,
        ),
        db=db,
        client=_fake_client("carol@example.com"),
    )
    other_ground = "Customer Relationship Administration"  # real row, also "Legitimate interests"
    save_data_mapping(
        business_process_id,
        DataMappingRequest(
            name="Fuel card KYC verification", data_categories=[REAL_CATEGORY], ground=other_ground,
        ),
        db=db,
        client=_fake_client("carol@example.com"),
    )

    read_back = get_data_mapping(business_process_id, db=db)
    assert read_back.mapping.ground == other_ground


def test_a_mapping_with_no_ground_reads_back_with_ground_none(db, business_process_id):
    # The mirror image of the round-trip test above: a mapping that never
    # named a ground must read back as None, not as a leftover or invented
    # value — proving get_mapping's resolution is a real LEFT-JOIN-shaped
    # lookup (no row -> None) rather than something that only happens to
    # work when a ground exists.
    saved = save_data_mapping(
        business_process_id,
        DataMappingRequest(name="Fuel card KYC verification", data_categories=[REAL_CATEGORY]),
        db=db,
        client=_fake_client("carol@example.com"),
    )
    assert saved.ground is None

    read_back = get_data_mapping(business_process_id, db=db)
    assert read_back.mapping.ground is None
    assert read_back.mapping.fides_legal_basis is None


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


def test_a_provisioned_system_passes_fideslang_response_validation(db, business_process_id):
    # Fix wave, item C-1. Mirrors test_taxonomy_loader.py's own
    # test_created_rows_pass_fideslang_response_validation exactly — the
    # SAME defect shape (a raw-SQL INSERT leaving a required fideslang
    # field NULL), the SAME convention (validate the row this code path
    # actually wrote, not a lookalike). GET /api/v1/system declares
    # BasicSystemResponse as its response_model, so that is the model this
    # asserts against, not a hand-picked subset of its fields.
    #
    # Before this fix, organization_fides_key and system_type were both
    # left NULL, which is not merely "missing" — both are required,
    # non-Optional str on fideslang's own System model, which
    # BasicSystemResponse inherits — so FastAPI's response validation
    # would raise trying to serialize this row inside the LIST of every
    # system, taking every OTHER (valid) system down with it. This test
    # proves the row alone validates; the live-database repair (this
    # task's report) proves the two already-written bad rows were fixed
    # the same way.
    response = _save(db, business_process_id)

    orm_system = (
        db.query(SystemModel).filter(SystemModel.id == response.system_id).one()
    )
    validated = BasicSystemResponse.model_validate(orm_system)
    assert validated.organization_fides_key == MAPPING_ROUTE_ORGANIZATION_FIDES_KEY
    assert validated.system_type == MAPPING_ROUTE_SYSTEM_TYPE


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
    # Fix round 2, item M-4: the original version of this test asserted
    # `before == 0` for a process seeded three lines earlier with a fresh
    # UUID — trivially true regardless of whether provisioning is lazy or
    # eager, so it proved nothing. Strengthened two ways: (1) a TOTAL
    # ctl_systems row-count delta, which WOULD catch an eager/bulk
    # provisioning bug (looping over every business process, of which many
    # more than one exist live in this database) in a way a single
    # fides_key lookup cannot; (2) the untouched process's own fides_key
    # still resolves to nothing afterward.
    untouched = _seed_business_process(db, "CSR Planning & Execution", "CSR")
    mapped = _seed_business_process(db, "Fraud Risk Assessments", "Audit & Risk")

    total_before = db.execute(sqlalchemy.text("SELECT count(*) FROM ctl_systems")).scalar()

    save_data_mapping(
        mapped,
        DataMappingRequest(name="Fraud review", data_categories=[REAL_CATEGORY]),
        db=db,
        client=_fake_client("carol@example.com"),
    )

    total_after = db.execute(sqlalchemy.text("SELECT count(*) FROM ctl_systems")).scalar()
    assert total_after - total_before == 1, (
        "exactly one system must be provisioned by mapping ONE process — "
        "an eager/bulk provisioning bug would add far more than one"
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


# --- Security: the mapping route requires Fides' own system-write
# authorisation ALONGSIDE the PrivacyCare scope (fix round 2, item I-3) ---


def test_the_mapping_route_declares_system_write_authorisation_alongside_the_privacycare_scope():
    # This route writes ctl_systems and privacydeclaration — Ethyca tables
    # in the customer's own Fides estate — and, before this fix, was
    # authorised behind PRIVACYCARE_SCREENING_CREATE alone: an M2M client
    # minted with only that one scope could create systems and processing
    # activities, and record lawful-basis provenance, none of which it
    # could do through any Ethyca endpoint. grounds.py's own PUT
    # .../ground route (writing the SAME privacycare_declaration_ground
    # table) requires SYSTEM_UPDATE via verify_oauth_client_for_
    # declaration_system; this route must require the equivalent,
    # verify_oauth_client_for_business_process_mapping, via SYSTEM_UPDATE.
    #
    # Declaration-level, not a live-auth integration test — same house
    # style as test_every_screening_route_requires_its_declared_scope in
    # test_api_screening.py, which proves every route DECLARES its scope
    # by walking route.dependencies, not by driving a real TestClient
    # against real tokens (this router has no such test anywhere).
    from fides.api.privacycare.api.screening import (
        verify_oauth_client_for_business_process_mapping,
    )
    from fides.common.scope_registry import SYSTEM_UPDATE

    routes = [
        r for r in privacycare_screening_router.routes
        if getattr(r, "path", "") == f"{PRIVACYCARE_SCREENING_PREFIX}/{{business_process_id}}/mapping"
        and "POST" in getattr(r, "methods", set())
    ]
    assert routes, "the mapping route was not found on privacycare_screening_router"

    for route in routes:
        deps = getattr(route, "dependencies", [])
        system_write_deps = [
            d for d in deps
            if getattr(d, "dependency", None) is verify_oauth_client_for_business_process_mapping
        ]
        assert system_write_deps, (
            f"{route.path} has no verify_oauth_client_for_business_process_mapping "
            "dependency — an M2M client with only PRIVACYCARE_SCREENING_CREATE "
            "could create Fides systems and processing activities"
        )
        assert any(
            SYSTEM_UPDATE in getattr(d, "scopes", []) for d in system_write_deps
        ), f"{route.path} does not require SYSTEM_UPDATE via system-write authorisation"

        # PRIVACYCARE_SCREENING_CREATE must STILL be required too — this is
        # an addition, not a replacement (Owner/Contributor already hold
        # both, so nothing regresses for role-based callers).
        privacycare_deps = [
            d for d in deps if getattr(d, "dependency", None) is verify_oauth_client
        ]
        assert any(
            PRIVACYCARE_SCREENING_CREATE in getattr(d, "scopes", []) for d in privacycare_deps
        ), f"{route.path} lost its PRIVACYCARE_SCREENING_CREATE requirement"


def test_existing_system_id_for_process_resolves_the_same_system_the_write_path_uses(
    db, business_process_id
):
    # The authorisation dependency and the write path must never be able to
    # disagree about which system a mapping call is about to touch — proven
    # here by seeding a real pre-existing system for the process and
    # checking mapping.existing_system_id_for_process (what the auth
    # dependency calls) returns exactly that system's id, matching what
    # save_data_mapping itself goes on to reuse.
    real_system_id = f"sys_{uuid.uuid4().hex[:8]}"
    db.execute(
        sqlalchemy.text("INSERT INTO ctl_systems (id, fides_key, name) VALUES (:id, :key, :name)"),
        {"id": real_system_id, "key": real_system_id, "name": "Fuel Card Platform"},
    )
    decl_id = _seed_unrelated_activity(
        db, business_process_id, "Fuel card marketing campaigns", "marketing.advertising"
    )
    db.execute(
        sqlalchemy.text("UPDATE privacydeclaration SET system_id = :sid WHERE id = :id"),
        {"sid": real_system_id, "id": decl_id},
    )

    from fides.api.privacycare.screening.mapping import existing_system_id_for_process

    assert existing_system_id_for_process(db, business_process_id) == real_system_id

    response = save_data_mapping(
        business_process_id,
        DataMappingRequest(name="Fuel card KYC verification", data_categories=[REAL_CATEGORY]),
        db=db,
        client=_fake_client("carol@example.com"),
    )
    assert response.system_id == real_system_id


# --- Concurrency: idempotency holds under two simultaneous callers
# (fix round 2, item I-2) ---------------------------------------------------


def test_concurrent_first_mappings_of_the_same_process_do_not_create_two_activities(
    committed_business_process_id,
):
    """Reproduces I-2's scenario with two real sessions on two real
    connections, forced into the exact interleaving the finding describes,
    rather than asserting anything about the SQL text alone — same shape as
    test_risk_register.py's own test_concurrent_add_risk_does_not_lose_the_
    higher_band.

    Thread A calls save_mapping for a business process that has never been
    mapped and, deliberately, does NOT commit yet — its transaction, and
    with the fix the FOR UPDATE lock save_mapping now takes on the
    business process row, stays open. Thread B is only started once A has
    finished its own save_mapping call (SELECT ... FOR UPDATE + INSERT,
    all uncommitted), so B's own save_mapping necessarily blocks trying to
    acquire the same lock. Only after B has had time to actually reach and
    block on that call does the test let A commit, unblocking B.

    With the fix, B's block happens BEFORE it reads _EXISTING_ROUTE_
    ACTIVITY_SQL, so once unblocked it re-reads post-A's-commit state and
    correctly finds A's already-marked activity, taking the UPDATE branch
    rather than a second INSERT. Without the fix, B's existence check runs
    before it ever blocks (its only lock would be the INSERT's own
    implicit row lock, taken too late), so B also takes the create branch
    and the process ends up with two marker-carrying activities — this
    test asserts exactly one exists afterward, and that only one is linked.
    """
    business_process_id = committed_business_process_id

    a_ready = threading.Event()
    release_a = threading.Event()
    errors: list[BaseException] = []

    def txn_a():
        try:
            engine_a = sqlalchemy.create_engine(DB_URL)
            with Session(engine_a) as session_a:
                save_mapping(
                    session_a,
                    business_process_id=business_process_id,
                    name="Fuel card KYC verification",
                    data_categories=[REAL_CATEGORY],
                    recorded_by="carol@example.com",
                )
                # Transaction intentionally left open: the FOR UPDATE lock
                # save_mapping took on the business process row is still
                # held.
                a_ready.set()
                held = release_a.wait(timeout=10)
                if not held:
                    raise AssertionError("txn_a: release_a was never set")
                session_a.commit()
        except BaseException as exc:  # noqa: BLE001 - surfaced via errors list
            errors.append(exc)

    def txn_b():
        try:
            a_ready.wait(timeout=10)
            engine_b = sqlalchemy.create_engine(DB_URL)
            with Session(engine_b) as session_b:
                # This call blocks inside save_mapping's own FOR UPDATE
                # SELECT until txn_a commits and releases the lock.
                save_mapping(
                    session_b,
                    business_process_id=business_process_id,
                    name="Fuel card KYC verification (retry)",
                    data_categories=[REAL_CATEGORY],
                    recorded_by="carol@example.com",
                )
                session_b.commit()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    thread_a = threading.Thread(target=txn_a)
    thread_b = threading.Thread(target=txn_b)

    thread_a.start()
    assert a_ready.wait(timeout=10), "txn_a never reached its held-open point"
    thread_b.start()
    # Give txn_b time to actually issue save_mapping and block on txn_a's
    # lock before txn_a is allowed to commit. Generous relative to local
    # Postgres round-trip latency; the interleaving this proves does not
    # depend on the exact duration, only on B attempting its own FOR
    # UPDATE before A releases its lock.
    time.sleep(0.5)
    release_a.set()

    thread_a.join(timeout=10)
    thread_b.join(timeout=10)

    assert not thread_a.is_alive(), "txn_a did not finish"
    assert not thread_b.is_alive(), "txn_b did not finish"
    assert not errors, f"background transaction(s) raised: {errors}"

    with Session(sqlalchemy.create_engine(DB_URL)) as verify:
        links = verify.execute(
            sqlalchemy.text(
                "SELECT privacy_declaration_id FROM privacycare_process_declaration "
                "WHERE business_process_id = :id"
            ),
            {"id": business_process_id},
        ).scalars().all()
        assert len(links) == 1, (
            f"expected exactly ONE linked activity after two concurrent first "
            f"mappings of the same process, found {len(links)}: {links}"
        )
        activity_count = verify.execute(
            sqlalchemy.text(
                "SELECT count(*) FROM privacydeclaration WHERE id = ANY(:ids) "
                "AND :marker = ANY(features)"
            ),
            {"ids": links, "marker": MAPPING_ROUTE_FEATURE_MARKER},
        ).scalar()
        assert activity_count == 1
        # B's UPDATE branch should have won the write race (it ran last,
        # after A's commit) — the surviving activity's name is B's.
        name = verify.execute(
            sqlalchemy.text("SELECT name FROM privacydeclaration WHERE id = :id"),
            {"id": links[0]},
        ).scalar()
        assert name == "Fuel card KYC verification (retry)"
