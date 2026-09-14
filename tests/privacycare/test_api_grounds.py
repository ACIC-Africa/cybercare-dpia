# D-KT-5's route surface: which Kenyan ground justifies a declaration's
# Article 6 value, and the guarantee (D-KT-4) that the declaration's own
# stored legal_basis_for_processing agrees with the ground's class before
# either can be recorded together.
#
# Same pattern as test_api_processes.py: call the route functions directly
# (_fake_client, the real _seed_system/_seed_declaration helpers, a
# function-scoped db fixture that rolls back on teardown) rather than going
# through FastAPI's dependency injection.
from uuid import uuid4

import pytest
import sqlalchemy
from fastapi import HTTPException
from sqlalchemy.orm import Session

from fides.api.privacycare.api.grounds import (
    SetGroundRequest,
    get_declaration_ground,
    list_processing_grounds,
    set_declaration_ground,
)
from fides.api.privacycare.taxonomy.loader import load_kenyan_taxonomy
from tests.privacycare.test_api_assessments import _fake_client
from tests.privacycare.test_context import _seed_declaration, _seed_system

DB_URL = "postgresql://postgres:fides@127.0.0.1:5442/fides"


@pytest.fixture
def db():
    engine = sqlalchemy.create_engine(DB_URL)
    with Session(engine) as session:
        yield session
        session.rollback()


@pytest.fixture(autouse=True)
def _no_commit(db, monkeypatch):
    # set_declaration_ground commits on its success path (the request's own
    # session boundary, same as create_business_process in processes.py).
    # Without this guard, seeded systems/declarations/ground rows were
    # committed to the live DB once already (a test called the route's
    # success path without neutralising commit first) — autouse, module-wide,
    # so no test in this file can forget it again.
    monkeypatch.setattr(db, "commit", lambda: None)


def _ground_id(db, ground: str) -> str:
    return db.execute(
        sqlalchemy.text(
            "SELECT id FROM privacycare_processing_ground WHERE ground = :ground"
        ),
        {"ground": ground},
    ).scalar()


def test_only_mapped_grounds_are_offered(db):
    load_kenyan_taxonomy(db)

    body = list_processing_grounds(db=db, client=_fake_client("carol@example.com"))

    assert len(body.grounds) == 11
    assert body.unmapped_count == 12
    assert all(g.fides_legal_basis for g in body.grounds)


def test_grounds_are_ordered_by_name(db):
    load_kenyan_taxonomy(db)

    body = list_processing_grounds(db=db, client=_fake_client("carol@example.com"))

    names = [g.ground for g in body.grounds]
    assert names == sorted(names)


def test_recording_a_ground_requires_the_enum_to_agree(db):
    load_kenyan_taxonomy(db)
    system = _seed_system(db, f"sys_{uuid4().hex[:8]}")
    decl = _seed_declaration(db, system, "marketing", legal_basis_for_processing="Consent")
    kyc = _ground_id(db, "KYC Requirements")

    with pytest.raises(HTTPException) as exc_info:
        set_declaration_ground(
            decl,
            SetGroundRequest(processing_ground_id=kyc),
            db=db,
            client=_fake_client("carol@example.com"),
        )

    assert exc_info.value.status_code == 422


def test_recording_a_ground_round_trips(db):
    load_kenyan_taxonomy(db)
    system = _seed_system(db, f"sys_{uuid4().hex[:8]}")
    decl = _seed_declaration(
        db, system, "marketing", legal_basis_for_processing="Legitimate interests"
    )
    kyc = _ground_id(db, "KYC Requirements")

    out = set_declaration_ground(
        decl,
        SetGroundRequest(processing_ground_id=kyc),
        db=db,
        client=_fake_client("carol@example.com"),
    )

    assert out.fides_legal_basis == "Legitimate interests"
    assert out.privacy_declaration_id == decl
    assert out.processing_ground_id == kyc

    again = get_declaration_ground(decl, db=db, client=_fake_client("carol@example.com"))

    assert again.processing_ground_id == kyc
    assert again.fides_legal_basis == "Legitimate interests"


def test_recording_a_ground_upserts_rather_than_duplicates(db):
    # privacycare_declaration_ground is unique on privacy_declaration_id — a
    # declarant changing their mind about which ground applies must replace
    # the row, not collide with it.
    load_kenyan_taxonomy(db)
    system = _seed_system(db, f"sys_{uuid4().hex[:8]}")
    decl = _seed_declaration(
        db, system, "marketing", legal_basis_for_processing="Legitimate interests"
    )
    kyc = _ground_id(db, "KYC Requirements")

    set_declaration_ground(
        decl,
        SetGroundRequest(processing_ground_id=kyc),
        db=db,
        client=_fake_client("alice@example.com"),
    )
    out = set_declaration_ground(
        decl,
        SetGroundRequest(processing_ground_id=kyc),
        db=db,
        client=_fake_client("bob@example.com"),
    )

    assert out.processing_ground_id == kyc
    count = db.execute(
        sqlalchemy.text(
            "SELECT count(*) FROM privacycare_declaration_ground "
            "WHERE privacy_declaration_id = :id"
        ),
        {"id": decl},
    ).scalar()
    assert count == 1


def test_a_null_class_ground_cannot_be_recorded(db):
    load_kenyan_taxonomy(db)
    system = _seed_system(db, f"sys_{uuid4().hex[:8]}")
    decl = _seed_declaration(db, system, "marketing", legal_basis_for_processing="Public interest")
    pub = _ground_id(db, "Public Interest")

    with pytest.raises(HTTPException) as exc_info:
        set_declaration_ground(
            decl,
            SetGroundRequest(processing_ground_id=pub),
            db=db,
            client=_fake_client("carol@example.com"),
        )

    assert exc_info.value.status_code == 409, (
        "suggested class is not a class; Carol has not ruled"
    )


def test_a_null_declaration_legal_basis_is_422(db):
    # A declaration with no legal_basis_for_processing at all has nothing for
    # a ground's class to agree with, so it must be rejected the same way a
    # mismatch is — not treated as vacuously fine because there is nothing
    # to compare.
    load_kenyan_taxonomy(db)
    system = _seed_system(db, f"sys_{uuid4().hex[:8]}")
    decl_id = f"decl_{uuid4().hex[:8]}"
    db.execute(
        sqlalchemy.text(
            "INSERT INTO privacydeclaration "
            "(id, name, data_use, data_categories, system_id, features, "
            " processes_special_category_data, data_shared_with_third_parties, "
            " flexible_legal_basis_for_processing, legal_basis_for_processing, "
            " retention_period) "
            "VALUES (:id, NULL, 'marketing', ARRAY['user.contact.email'], "
            " :system_id, '{}', false, false, true, NULL, '7 years')"
        ),
        {"id": decl_id, "system_id": system},
    )
    kyc = _ground_id(db, "KYC Requirements")

    with pytest.raises(HTTPException) as exc_info:
        set_declaration_ground(
            decl_id,
            SetGroundRequest(processing_ground_id=kyc),
            db=db,
            client=_fake_client("carol@example.com"),
        )

    assert exc_info.value.status_code == 422
    assert "legal_basis_for_processing" in exc_info.value.detail


def test_setting_a_ground_against_an_unknown_declaration_is_a_404(db):
    load_kenyan_taxonomy(db)
    kyc = _ground_id(db, "KYC Requirements")

    with pytest.raises(HTTPException) as exc_info:
        set_declaration_ground(
            "decl_does_not_exist",
            SetGroundRequest(processing_ground_id=kyc),
            db=db,
            client=_fake_client("carol@example.com"),
        )

    assert exc_info.value.status_code == 404


def test_setting_an_unknown_ground_is_a_404(db):
    system = _seed_system(db, f"sys_{uuid4().hex[:8]}")
    decl = _seed_declaration(db, system, "marketing", legal_basis_for_processing="Consent")

    with pytest.raises(HTTPException) as exc_info:
        set_declaration_ground(
            decl,
            SetGroundRequest(processing_ground_id="ground_does_not_exist"),
            db=db,
            client=_fake_client("carol@example.com"),
        )

    assert exc_info.value.status_code == 404


def test_reading_an_unrecorded_declaration_ground_is_a_404(db):
    system = _seed_system(db, f"sys_{uuid4().hex[:8]}")
    decl = _seed_declaration(db, system, "marketing", legal_basis_for_processing="Consent")

    with pytest.raises(HTTPException) as exc_info:
        get_declaration_ground(decl, db=db, client=_fake_client("carol@example.com"))

    assert exc_info.value.status_code == 404


def test_a_ground_for_a_deleted_declaration_reads_404(db):
    # I2: privacycare_declaration_ground carries no FK to privacydeclaration
    # (models.py says so deliberately), and Fides DELETEs a declaration whose
    # logical id `data_use:name` no longer matches on a system save
    # (db/system.py) — so a ground row CAN outlive the declaration it names.
    # The read path joins privacydeclaration precisely so that stranded row
    # reads as "no ground recorded" rather than as a live answer about a
    # declaration that no longer exists.
    load_kenyan_taxonomy(db)
    system = _seed_system(db, f"sys_{uuid4().hex[:8]}")
    decl = _seed_declaration(
        db, system, "marketing", legal_basis_for_processing="Legitimate interests"
    )
    kyc = _ground_id(db, "KYC Requirements")
    set_declaration_ground(
        decl,
        SetGroundRequest(processing_ground_id=kyc),
        db=db,
        client=_fake_client("carol@example.com"),
    )
    # Read back once while the declaration still exists, so the 404 below is
    # provably the join biting and not a row that was never written.
    assert (
        get_declaration_ground(
            decl, db=db, client=_fake_client("carol@example.com")
        ).processing_ground_id
        == kyc
    )

    db.execute(
        sqlalchemy.text("DELETE FROM privacydeclaration WHERE id = :id"), {"id": decl}
    )
    orphan = db.execute(
        sqlalchemy.text(
            "SELECT count(*) FROM privacycare_declaration_ground "
            "WHERE privacy_declaration_id = :id"
        ),
        {"id": decl},
    ).scalar()
    assert orphan == 1, "the ground row outlives the declaration — that is the hazard"

    with pytest.raises(HTTPException) as exc_info:
        get_declaration_ground(decl, db=db, client=_fake_client("carol@example.com"))

    assert exc_info.value.status_code == 404
