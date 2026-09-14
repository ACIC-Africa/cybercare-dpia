import importlib.util
import pathlib
import subprocess
import sys
import uuid

import pytest
import sqlalchemy
from fideslang.models import DataCategory, DataSubject
from sqlalchemy.orm import Session

from fides.api.privacycare.taxonomy import kenyan
from fides.api.privacycare.taxonomy.loader import (
    count_declarations_referencing_created_keys,
    load_kenyan_taxonomy,
    revert_kenyan_taxonomy,
)
from tests.privacycare.test_context import _seed_declaration, _seed_system

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


def test_cli_dry_run_by_default_writes_nothing(db):
    # Ruling F1b: this suite must stay green whether run against a fresh DB
    # or one Task 3 Step 4 already loaded for real, so we cannot assert
    # is_default=false count == 0 outright (true only pre-load). Instead we
    # snapshot the two counts the loader could touch, run the dry run, and
    # assert neither moved — the session has no writes of its own, so a
    # fresh query after the subprocess exits sees whatever is truly
    # committed.
    before = (
        db.execute(sqlalchemy.text(
            "SELECT count(*) FROM ctl_data_subjects WHERE is_default = false"
        )).scalar(),
        db.execute(sqlalchemy.text(
            "SELECT count(*) FROM privacycare_taxonomy_mapping"
        )).scalar(),
    )
    out = subprocess.run(
        [sys.executable, "scripts/privacycare/load_taxonomy.py"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert "DRY RUN" in out and "mapping rows: 111" in out
    after = (
        db.execute(sqlalchemy.text(
            "SELECT count(*) FROM ctl_data_subjects WHERE is_default = false"
        )).scalar(),
        db.execute(sqlalchemy.text(
            "SELECT count(*) FROM privacycare_taxonomy_mapping"
        )).scalar(),
    )
    assert before == after


def test_cli_revert_without_commit_is_dry_run(db):
    # F10: --commit is the single persistence switch for both directions;
    # --revert alone must roll back like the plain load dry run does.
    before = (
        db.execute(sqlalchemy.text(
            "SELECT count(*) FROM ctl_data_subjects WHERE is_default = false"
        )).scalar(),
        db.execute(sqlalchemy.text(
            "SELECT count(*) FROM privacycare_taxonomy_mapping"
        )).scalar(),
    )
    out = subprocess.run(
        [sys.executable, "scripts/privacycare/load_taxonomy.py", "--revert"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert "DRY RUN" in out
    after = (
        db.execute(sqlalchemy.text(
            "SELECT count(*) FROM ctl_data_subjects WHERE is_default = false"
        )).scalar(),
        db.execute(sqlalchemy.text(
            "SELECT count(*) FROM privacycare_taxonomy_mapping"
        )).scalar(),
    )
    assert before == after


def test_created_rows_carry_no_version_added(db):
    # F9: has_versioning_if_default (fideslang/validation.py) rejects a
    # non-default row that carries version information. The INSERTs in
    # loader.py used to write version_added='3.1.3' on every created
    # (is_default=false) row — this proves that defect stays fixed.
    load_kenyan_taxonomy(db)
    subjects = db.execute(sqlalchemy.text(
        "SELECT count(*) FROM ctl_data_subjects "
        "WHERE is_default = false AND version_added IS NOT NULL"
    )).scalar()
    categories = db.execute(sqlalchemy.text(
        "SELECT count(*) FROM ctl_data_categories "
        "WHERE is_default = false AND version_added IS NOT NULL"
    )).scalar()
    assert (subjects, categories) == (0, 0)


def test_created_rows_carry_default_organization(db):
    # F11: fideslang's response model requires organization_fides_key to be
    # a non-null string; the INSERTs used to leave it NULL on every created
    # row. 'default_organization' is the only row in ctl_organizations and
    # what every Fides default row carries — pin the row shape here.
    load_kenyan_taxonomy(db)
    subjects = db.execute(sqlalchemy.text(
        "SELECT count(*) FROM ctl_data_subjects "
        "WHERE is_default = false AND organization_fides_key IS DISTINCT FROM 'default_organization'"
    )).scalar()
    categories = db.execute(sqlalchemy.text(
        "SELECT count(*) FROM ctl_data_categories "
        "WHERE is_default = false AND organization_fides_key IS DISTINCT FROM 'default_organization'"
    )).scalar()
    assert (subjects, categories) == (0, 0)


def test_created_rows_pass_fideslang_response_validation(db):
    # F9: GET /api/v1/data_subject and /api/v1/data_category serialize
    # every row through fideslang's own DataSubject/DataCategory models —
    # the same models _validate_creates() uses, but built here straight
    # from the DB columns fideslang knows about, not from kenyan.py, so
    # this proves the row we actually wrote (not a lookalike) validates.
    load_kenyan_taxonomy(db)

    subject_row = dict(db.execute(sqlalchemy.text(
        "SELECT fides_key, name, description, rights, is_default, active, "
        "tags, version_added, organization_fides_key FROM ctl_data_subjects "
        "WHERE fides_key = 'applicant'"
    )).mappings().one())
    DataSubject(**subject_row)

    category_row = dict(db.execute(sqlalchemy.text(
        "SELECT fides_key, name, description, parent_key, is_default, active, "
        "tags, version_added, organization_fides_key FROM ctl_data_categories "
        "WHERE 'privacycare:kenyan' = ANY(tags) LIMIT 1"
    )).mappings().one())
    DataCategory(**category_row)


def _load_cli_module():
    # scripts/ has no __init__.py (it's not a package), so load
    # load_taxonomy.py by path rather than a normal import — this is the
    # same script the subprocess-based CLI tests above invoke, just loaded
    # in-process to unit-test its pure _database_url() helper without a DB.
    path = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "privacycare" / "load_taxonomy.py"
    spec = importlib.util.spec_from_file_location("privacycare_load_taxonomy_cli", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_database_url_honours_privacycare_database_url_override(monkeypatch):
    # Review finding (Important #1): _database_url() must check
    # PRIVACYCARE_DATABASE_URL first, same precedence as
    # src/fides/api/privacycare/migrations/env.py's _database_url(), so
    # this CLI and migrate.sh/env.py never silently target different
    # databases. One test, both branches of that precedence.
    cli = _load_cli_module()

    monkeypatch.setenv("PRIVACYCARE_DATABASE_URL", "postgresql://scratch:scratch@example.invalid:1/scratch")
    monkeypatch.setenv("FIDES__DATABASE__SERVER", "should-be-ignored")
    assert cli._database_url() == "postgresql://scratch:scratch@example.invalid:1/scratch"

    monkeypatch.delenv("PRIVACYCARE_DATABASE_URL", raising=False)
    monkeypatch.setenv("FIDES__DATABASE__USER", "u")
    monkeypatch.setenv("FIDES__DATABASE__PASSWORD", "p")
    monkeypatch.setenv("FIDES__DATABASE__SERVER", "h")
    monkeypatch.setenv("FIDES__DATABASE__PORT", "1")
    monkeypatch.setenv("FIDES__DATABASE__DB", "d")
    assert cli._database_url() == "postgresql://u:p@h:1/d"


# --- I3: the revert guard --------------------------------------------------
# privacydeclaration.data_categories / .data_subjects are text arrays with no
# foreign key, so DELETEing a taxonomy row a declaration names always
# succeeds — and leaves that declaration naming a key nobody can resolve, at
# which point Fides' own validate_privacy_declarations (db/system.py) rejects
# the next save of the system and the admin UI cannot edit it any more. The
# guard is the only thing between an operator running `--revert --commit` on
# a populated tenant and that outcome.
CREATED_CATEGORY_KEY = "user.health_and_medical.hiv_status"
CREATED_SUBJECT_KEY = "authorized_signatory"


def _seed_declaration_naming_created_keys(db, **kwargs):
    system = _seed_system(db, f"sys-{uuid.uuid4().hex[:6]}")
    return _seed_declaration(db, system, "marketing.advertising", **kwargs)


def test_revert_refuses_while_a_declaration_names_a_created_category(db):
    load_kenyan_taxonomy(db)
    _seed_declaration_naming_created_keys(db, categories=[CREATED_CATEGORY_KEY])

    assert count_declarations_referencing_created_keys(db) == 1

    with pytest.raises(ValueError) as exc_info:
        revert_kenyan_taxonomy(db)

    assert "1 privacy declaration(s)" in str(exc_info.value)
    # Nothing was deleted: the guard runs before the first DELETE.
    assert db.execute(sqlalchemy.text(
        "SELECT count(*) FROM ctl_data_categories WHERE fides_key = :key"
    ), {"key": CREATED_CATEGORY_KEY}).scalar() == 1


def test_revert_refuses_while_a_declaration_names_a_created_subject(db):
    # The subject half of the guard: data_subjects is a second text array on
    # the same row, and a revert strips 33 created subject keys as well.
    load_kenyan_taxonomy(db)
    system = _seed_system(db, f"sys-{uuid.uuid4().hex[:6]}")
    decl = _seed_declaration(db, system, "marketing.advertising")
    db.execute(sqlalchemy.text(
        "UPDATE privacydeclaration SET data_subjects = ARRAY[:key] WHERE id = :id"
    ), {"key": CREATED_SUBJECT_KEY, "id": decl})

    assert count_declarations_referencing_created_keys(db) == 1

    with pytest.raises(ValueError):
        revert_kenyan_taxonomy(db)

    assert db.execute(sqlalchemy.text(
        "SELECT count(*) FROM ctl_data_subjects WHERE fides_key = :key"
    ), {"key": CREATED_SUBJECT_KEY}).scalar() == 1


def test_revert_with_force_proceeds_anyway(db):
    # --force is the operator saying "I accept that those declarations will
    # name keys that no longer exist". It must actually revert, not just
    # skip the count.
    load_kenyan_taxonomy(db)
    _seed_declaration_naming_created_keys(db, categories=[CREATED_CATEGORY_KEY])

    revert_kenyan_taxonomy(db, force=True)

    counts = db.execute(sqlalchemy.text(
        "SELECT (SELECT count(*) FROM ctl_data_subjects), "
        "(SELECT count(*) FROM ctl_data_categories), "
        "(SELECT count(*) FROM ctl_data_categories WHERE :tag = ANY(tags))"
    ), {"tag": kenyan.SPECIAL_TAG}).one()
    assert counts == (15, 85, 0)


def test_a_declaration_naming_only_default_keys_does_not_block_revert(db):
    # The guard counts CREATED keys only. A declaration naming Fides' own
    # default categories (the overwhelmingly common case, and the state of
    # the live database today) must not make revert unusable.
    load_kenyan_taxonomy(db)
    _seed_declaration_naming_created_keys(db, categories=["user.contact.email"])

    assert count_declarations_referencing_created_keys(db) == 0

    revert_kenyan_taxonomy(db)


# --- I4: the read-back drift check ----------------------------------------
def test_loading_over_a_drifted_row_raises(db):
    # The INSERTs are ON CONFLICT DO NOTHING, so a content change in
    # kenyan.py silently never reaches an already-loaded database. Simulate
    # the diverged state the way it would really arise (the DB holding
    # different content from the module) and prove the loader refuses to
    # report success over it.
    load_kenyan_taxonomy(db)
    db.execute(sqlalchemy.text(
        "UPDATE ctl_data_categories SET name = 'Something Else' WHERE fides_key = :key"
    ), {"key": CREATED_CATEGORY_KEY})

    with pytest.raises(ValueError) as exc_info:
        load_kenyan_taxonomy(db)

    assert CREATED_CATEGORY_KEY in str(exc_info.value)
    assert "--revert" in str(exc_info.value), "the message must name the remedy"


def test_loading_over_a_drifted_subject_raises(db):
    load_kenyan_taxonomy(db)
    db.execute(sqlalchemy.text(
        "UPDATE ctl_data_subjects SET description = 'not what kenyan.py says' "
        "WHERE fides_key = :key"
    ), {"key": CREATED_SUBJECT_KEY})

    with pytest.raises(ValueError) as exc_info:
        load_kenyan_taxonomy(db)

    assert CREATED_SUBJECT_KEY in str(exc_info.value)


def test_a_deleted_created_row_is_restored_by_the_next_load(db):
    # The other half of the read-back check: a MISSING key is not the
    # unfixable case a content edit is, because ON CONFLICT DO NOTHING still
    # inserts a row that is not there. The loader re-creates it and the
    # read-back then agrees, so no drift is reported — the check fires on
    # content that cannot be updated in place, not on absence that can.
    load_kenyan_taxonomy(db)
    db.execute(sqlalchemy.text(
        "DELETE FROM ctl_data_categories WHERE fides_key = :key"
    ), {"key": CREATED_CATEGORY_KEY})

    load_kenyan_taxonomy(db)

    restored = db.execute(sqlalchemy.text(
        "SELECT name FROM ctl_data_categories WHERE fides_key = :key"
    ), {"key": CREATED_CATEGORY_KEY}).scalar()
    assert restored == "HIV Status"
