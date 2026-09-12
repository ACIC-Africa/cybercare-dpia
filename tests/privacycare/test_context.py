# Target selection and context assembly. Inserts are rolled back (same
# pattern as test_api_assessments.py / test_answers.py: no ORM models for
# ctl_systems/privacydeclaration/ctl_data_uses in this package, so seed rows
# with raw SQL via a function-scoped db fixture that rolls back on teardown).
import uuid

import pytest
import sqlalchemy
from sqlalchemy.orm import Session

from fides.api.privacycare.context import (
    GenerationTarget,
    build_context,
    resolve_source,
    select_targets,
)

DB_URL = "postgresql://postgres:fides@127.0.0.1:5442/fides"


@pytest.fixture
def db():
    engine = sqlalchemy.create_engine(DB_URL)
    with Session(engine) as session:
        yield session
        session.rollback()


def _seed_system(db, fides_key: str, *, name=None, requires_dpa=False) -> str:
    system_id = f"sys_{uuid.uuid4().hex[:8]}"
    db.execute(
        sqlalchemy.text(
            "INSERT INTO ctl_systems "
            "(id, fides_key, name, description, dataset_references, "
            " processes_personal_data, exempt_from_privacy_regulations, "
            " uses_profiling, does_international_transfers, "
            " requires_data_protection_assessments, uses_cookies, "
            " cookie_refresh, uses_non_cookie_access, hidden) "
            "VALUES (:id, :fides_key, :name, :description, '{}', "
            " true, false, false, false, :requires_dpa, false, "
            " false, false, false)"
        ),
        {
            "id": system_id,
            "fides_key": fides_key,
            "name": name,
            "description": f"{fides_key} description",
            "requires_dpa": requires_dpa,
        },
    )
    return system_id


def _seed_declaration(
    db, system_id: str, data_use: str, *, name=None, categories=None
) -> str:
    decl_id = f"decl_{uuid.uuid4().hex[:8]}"
    db.execute(
        sqlalchemy.text(
            "INSERT INTO privacydeclaration "
            "(id, name, data_use, data_categories, system_id, features, "
            " processes_special_category_data, data_shared_with_third_parties, "
            " flexible_legal_basis_for_processing, legal_basis_for_processing, "
            " retention_period) "
            "VALUES (:id, :name, :data_use, :categories, :system_id, '{}', "
            " false, false, true, 'Consent', '7 years')"
        ),
        {
            "id": decl_id,
            "name": name,
            "data_use": data_use,
            "categories": categories or ["user.contact.email"],
            "system_id": system_id,
        },
    )
    return decl_id


def _seed_data_use(db, fides_key: str, name: str, description: str) -> None:
    db.execute(
        sqlalchemy.text(
            "INSERT INTO ctl_data_uses "
            "(id, fides_key, name, description, is_default, active) "
            "VALUES (:id, :fides_key, :name, :description, false, true) "
            "ON CONFLICT DO NOTHING"
        ),
        {
            "id": f"du_{uuid.uuid4().hex[:8]}",
            "fides_key": fides_key,
            "name": name,
            "description": description,
        },
    )


def test_select_targets_returns_one_target_per_declaration(db):
    sid = _seed_system(db, f"sys-{uuid.uuid4().hex[:6]}", name="Billing")
    _seed_declaration(db, sid, "essential.service.payment_processing")
    _seed_declaration(db, sid, "marketing.advertising")
    db.flush()

    targets = select_targets(db, None, high_risk_only=False)

    mine = [t for t in targets if t.system_name == "Billing"]
    assert len(mine) == 2, (
        "one assessment per declaration, not per system — privacy_assessment "
        "carries declaration_id/data_use/data_categories"
    )
    assert {t.data_use for t in mine} == {
        "essential.service.payment_processing",
        "marketing.advertising",
    }


def test_select_targets_filters_by_system_fides_keys(db):
    wanted = f"sys-{uuid.uuid4().hex[:6]}"
    other = f"sys-{uuid.uuid4().hex[:6]}"
    _seed_declaration(db, _seed_system(db, wanted), "marketing.advertising")
    _seed_declaration(db, _seed_system(db, other), "marketing.advertising")
    db.flush()

    targets = select_targets(db, [wanted], high_risk_only=False)

    assert [t.system_fides_key for t in targets] == [wanted]


def test_select_targets_high_risk_only_uses_the_requires_dpa_flag(db):
    high = f"sys-{uuid.uuid4().hex[:6]}"
    low = f"sys-{uuid.uuid4().hex[:6]}"
    _seed_declaration(
        db, _seed_system(db, high, requires_dpa=True), "marketing.advertising"
    )
    _seed_declaration(
        db, _seed_system(db, low, requires_dpa=False), "marketing.advertising"
    )
    db.flush()

    keys = {
        t.system_fides_key
        for t in select_targets(db, [high, low], high_risk_only=True)
    }

    assert keys == {high}, (
        "OQ-PRIVACY-10: the only trigger implemented is "
        "ctl_systems.requires_data_protection_assessments"
    )


def test_select_targets_returns_nothing_when_no_system_matches(db):
    assert select_targets(db, ["no-such-system"], high_risk_only=False) == []


def test_build_context_carries_the_declaration_and_its_data_use(db):
    # ctl_data_uses ships pre-seeded with the real Fides default taxonomy
    # (56 rows including "marketing.advertising" itself), so a fresh
    # fides_key is used here rather than colliding with a canonical row via
    # ON CONFLICT DO NOTHING, which would silently keep the existing name
    # and description instead of the ones this test asserts on.
    data_use_key = f"custom.marketing.{uuid.uuid4().hex[:6]}"
    key = f"sys-{uuid.uuid4().hex[:6]}"
    sid = _seed_system(db, key, name="CRM")
    _seed_data_use(db, data_use_key, "Advertising", "Promoting goods.")
    _seed_declaration(
        db, sid, data_use_key, name="Email campaigns",
        categories=["user.contact.email", "user.behavior"],
    )
    db.flush()

    target = next(t for t in select_targets(db, [key], high_risk_only=False))
    context = build_context(db, target)

    assert context["system"]["name"] == "CRM"
    assert context["privacy_declaration"]["name"] == "Email campaigns"
    assert context["privacy_declaration"]["data_categories"] == [
        "user.contact.email",
        "user.behavior",
    ]
    assert context["data_use"]["name"] == "Advertising"
    assert context["data_use"]["description"] == "Promoting goods."


def test_build_context_tolerates_an_unregistered_data_use(db):
    # A declaration may name a data_use with no row in ctl_data_uses. That
    # must not crash generation for the whole system — the sources that
    # resolve still do.
    key = f"sys-{uuid.uuid4().hex[:6]}"
    sid = _seed_system(db, key, name="Legacy")
    _seed_declaration(db, sid, f"custom.unregistered.{uuid.uuid4().hex[:6]}")
    db.flush()

    target = next(t for t in select_targets(db, [key], high_risk_only=False))
    context = build_context(db, target)

    assert context["data_use"]["name"] is None
    assert context["data_use"]["description"] is None


@pytest.mark.parametrize(
    "path,expected",
    [
        ("system.name", "CRM"),
        ("system.description", "a description"),
        ("privacy_declaration.data_use", "marketing.advertising"),
        ("privacy_declaration.data_categories", "user.contact.email, user.behavior"),
        ("data_use.name", "Advertising"),
        ("privacy_declaration.missing_field", None),
        ("nonexistent_root.name", None),
        ("malformed", None),
    ],
)
def test_resolve_source_reads_a_dotted_path(path, expected):
    # fides_sources on assessment_question is a list of exactly these dotted
    # paths — sampled from the live table: ['system.name',
    # 'system.description', 'privacy_declaration.name'].
    context = {
        "system": {"name": "CRM", "description": "a description"},
        "privacy_declaration": {
            "data_use": "marketing.advertising",
            "data_categories": ["user.contact.email", "user.behavior"],
        },
        "data_use": {"name": "Advertising"},
    }
    assert resolve_source(context, path) == expected


def test_resolve_source_returns_none_for_an_empty_list():
    # An empty list must read as "not known", not as the string "[]" — an
    # empty list rendered into a DPIA answer would assert a fact.
    context = {"privacy_declaration": {"data_categories": []}}
    assert resolve_source(context, "privacy_declaration.data_categories") is None
