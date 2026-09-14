import pytest
import sqlalchemy
from sqlalchemy.orm import Session

from fideslang.models import DataCategory, DataSubject

from fides.api.privacycare.taxonomy import kenyan
from fides.api.privacycare.taxonomy.loader import load_kenyan_taxonomy, revert_kenyan_taxonomy

DB_URL = "postgresql://postgres:fides@127.0.0.1:5442/fides"


@pytest.fixture
def db(monkeypatch):
    engine = sqlalchemy.create_engine(DB_URL)
    with Session(engine) as session:
        # Fides' Base.create_or_update commits internally; the loader in this
        # plan does not use it, but nothing in a test may commit either.
        monkeypatch.setattr(session, "commit", lambda: None)
        yield session
        session.rollback()


def test_every_row_validates_with_fideslang():
    # D-KT-2: fideslang rejects a mismatched parent and a parallel root. Prove
    # every row we intend to create is one fideslang would accept BEFORE any
    # database is touched.
    for c in kenyan.CATEGORIES:
        if c.action == "create":
            DataCategory(fides_key=c.fides_key, parent_key=c.parent_key, name=c.term,
                         description=c.reason, is_default=False)
    for s in kenyan.SUBJECTS:
        if s.action == "create":
            DataSubject(fides_key=s.fides_key, name=s.term, description=s.reason,
                        is_default=False, rights=kenyan.SIX_RIGHTS)


def test_every_customer_term_gets_a_mapping_row(db):
    # D-KT-7: the count assertion is on mapping rows, whatever the action.
    summary = load_kenyan_taxonomy(db)
    assert summary.mapping_rows == 43 + 68
    rows = db.execute(sqlalchemy.text(
        "SELECT taxonomy, count(*) FROM privacycare_taxonomy_mapping GROUP BY taxonomy"
    )).all()
    assert dict(rows) == {"data_subject": 43, "data_category": 68}


def test_subjects_reuse_9_create_33_drop_1(db):
    summary = load_kenyan_taxonomy(db)
    assert (summary.subjects_reused, summary.subjects_created, summary.subjects_skipped) == (9, 33, 1)
    n = db.execute(sqlalchemy.text(
        "SELECT count(*) FROM ctl_data_subjects WHERE is_default = false"
    )).scalar()
    assert n == 33
    name = db.execute(sqlalchemy.text(
        "SELECT name FROM ctl_data_subjects WHERE fides_key = 'customer'"
    )).scalar()
    assert name == "Customer/Client", "reused default rows carry the customer's term"


def test_every_loaded_subject_has_the_six_rights(db):
    # D-KT-6: uniform INCLUDE-six with provenance brief §3. JSON, not SQL NULL.
    load_kenyan_taxonomy(db)
    rows = db.execute(sqlalchemy.text(
        "SELECT fides_key, rights FROM ctl_data_subjects "
        "WHERE fides_key IN (SELECT fides_key FROM privacycare_taxonomy_mapping "
        "WHERE taxonomy='data_subject' AND action IN ('reuse','create'))"
    )).all()
    assert len(rows) == 42
    for key, rights in rows:
        assert rights == kenyan.SIX_RIGHTS, key


def test_special_categories_are_tagged_and_leaves_are_under_existing_parents(db):
    # D-KT-3 carrier + D-KT-2: 8 distinct tagged defaults (Race and Ethnic or
    # Social Origin share race_ethnicity) + 18 created special leaves = 26.
    summary = load_kenyan_taxonomy(db)
    assert summary.categories_tagged == 8
    tagged = db.execute(sqlalchemy.text(
        "SELECT fides_key, parent_key FROM ctl_data_categories WHERE :tag = ANY(tags) ORDER BY 1"
    ), {"tag": kenyan.SPECIAL_TAG}).all()
    assert len(tagged) == 26
    parents = {p for _, p in tagged if p}
    existing = set(db.execute(sqlalchemy.text(
        "SELECT fides_key FROM ctl_data_categories WHERE is_default = true"
    )).scalars())
    assert parents <= existing, parents - existing
    assert ("user.health_and_medical.hiv_status", "user.health_and_medical") in tagged


def test_deferred_terms_are_recorded_with_an_owner_not_loaded(db):
    summary = load_kenyan_taxonomy(db)
    assert ("data_category", "Symbol", "Carol") in summary.deferred
    assert db.execute(sqlalchemy.text(
        "SELECT count(*) FROM ctl_data_categories WHERE name = 'Symbol'"
    )).scalar() == 0


def test_grounds_load_23_with_11_mapped(db):
    summary = load_kenyan_taxonomy(db)
    assert (summary.grounds_loaded, summary.grounds_unmapped) == (23, 12)
    kyc = db.execute(sqlalchemy.text(
        "SELECT fides_legal_basis FROM privacycare_processing_ground WHERE ground = 'KYC Requirements'"
    )).scalar()
    assert kyc == "Legitimate interests", "recorded as the customer classed it, not corrected"


def test_loading_twice_changes_nothing(db):
    load_kenyan_taxonomy(db)
    before = db.execute(sqlalchemy.text(
        "SELECT (SELECT count(*) FROM ctl_data_subjects), (SELECT count(*) FROM ctl_data_categories), "
        "(SELECT count(*) FROM privacycare_taxonomy_mapping), (SELECT count(*) FROM privacycare_processing_ground)"
    )).one()
    load_kenyan_taxonomy(db)
    after = db.execute(sqlalchemy.text(
        "SELECT (SELECT count(*) FROM ctl_data_subjects), (SELECT count(*) FROM ctl_data_categories), "
        "(SELECT count(*) FROM privacycare_taxonomy_mapping), (SELECT count(*) FROM privacycare_processing_ground)"
    )).one()
    # 33 subjects created; 29 categories created (18 special + 11 non-special);
    # derive, don't copy: the number must come from the data module.
    created = sum(c.action == "create" for c in kenyan.CATEGORIES)
    assert created == 29
    assert before == after == (15 + 33, 85 + created, 111, 23)


def test_revert_restores_the_fides_baseline(db):
    load_kenyan_taxonomy(db)
    revert_kenyan_taxonomy(db)
    counts = db.execute(sqlalchemy.text(
        "SELECT (SELECT count(*) FROM ctl_data_subjects), (SELECT count(*) FROM ctl_data_categories), "
        "(SELECT count(*) FROM ctl_data_categories WHERE :tag = ANY(tags)), "
        "(SELECT name FROM ctl_data_subjects WHERE fides_key='customer')"
    ), {"tag": kenyan.SPECIAL_TAG}).one()
    assert counts == (15, 85, 0, "Customer")
