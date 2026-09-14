# derive_special_category reads the tag, not a name list — a tagged
# ancestor makes a leaf category special too, and a declarant's "no" that
# disagrees with the tag is a finding (mismatch), never the reverse.
import uuid

import pytest
import sqlalchemy
from sqlalchemy.orm import Session

from fides.api.privacycare.special_category import derive_special_category
from fides.api.privacycare.taxonomy.loader import load_kenyan_taxonomy
from tests.privacycare.test_context import _seed_declaration, _seed_system

TAG = "dpa2019:special_category"

DB_URL = "postgresql://postgres:fides@127.0.0.1:5442/fides"


@pytest.fixture
def db():
    engine = sqlalchemy.create_engine(DB_URL)
    with Session(engine) as session:
        yield session
        session.rollback()


def test_hiv_status_makes_the_declaration_special(db):
    load_kenyan_taxonomy(db)
    system = _seed_system(db, f"sys_{uuid.uuid4().hex[:8]}")
    decl = _seed_declaration(
        db,
        system,
        "marketing",
        categories=["user.health_and_medical.hiv_status"],
        processes_special_category_data=False,
    )

    view = derive_special_category(db, decl)

    assert view.derived is True
    assert view.triggering_keys == ["user.health_and_medical.hiv_status"]
    assert view.mismatch is True, "declarant ticked no, the data says yes: a finding"


def test_ancestor_tag_is_inherited(db):
    # D-KT-2 is what makes this work: user.biometric is tagged, so a
    # declaration written against user.biometric.fingerprint is special too.
    load_kenyan_taxonomy(db)
    system = _seed_system(db, f"sys_{uuid.uuid4().hex[:8]}")
    decl = _seed_declaration(
        db,
        system,
        "marketing",
        categories=["user.biometric.fingerprint"],
        processes_special_category_data=True,
    )

    view = derive_special_category(db, decl)

    assert view.derived is True and view.mismatch is False


def test_contact_details_are_not_special(db):
    load_kenyan_taxonomy(db)
    system = _seed_system(db, f"sys_{uuid.uuid4().hex[:8]}")
    decl = _seed_declaration(
        db,
        system,
        "marketing",
        categories=["user.contact.email"],
        processes_special_category_data=False,
    )

    view = derive_special_category(db, decl)

    assert view.derived is False and view.triggering_keys == [] and view.mismatch is False


def test_a_true_tick_with_no_tagged_category_is_not_a_finding(db):
    # The declarant may know something the taxonomy does not — mismatch is
    # one-directional, declared=True/derived=False is never a finding.
    load_kenyan_taxonomy(db)
    system = _seed_system(db, f"sys_{uuid.uuid4().hex[:8]}")
    decl = _seed_declaration(
        db,
        system,
        "marketing",
        categories=["user.contact.email"],
        processes_special_category_data=True,
    )

    view = derive_special_category(db, decl)

    assert view.derived is False
    assert view.mismatch is False


def test_mutation_removing_the_tag_removes_the_answer(db):
    # Spec acceptance 4. Proves the derivation reads the tag, not a name
    # list.
    load_kenyan_taxonomy(db)
    system = _seed_system(db, f"sys_{uuid.uuid4().hex[:8]}")
    decl = _seed_declaration(
        db,
        system,
        "marketing",
        categories=["user.health_and_medical.hiv_status"],
        processes_special_category_data=False,
    )
    db.execute(
        sqlalchemy.text(
            "UPDATE ctl_data_categories SET tags = array_remove(tags, :tag) "
            "WHERE fides_key = 'user.health_and_medical.hiv_status'"
        ),
        {"tag": TAG},
    )

    assert derive_special_category(db, decl).derived is False


def test_unknown_declaration_raises(db):
    with pytest.raises(LookupError):
        derive_special_category(db, f"decl_{uuid.uuid4().hex[:8]}")
