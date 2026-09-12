# Target selection and context assembly. Inserts are rolled back (same
# pattern as test_api_assessments.py / test_answers.py: no ORM models for
# ctl_systems/privacydeclaration/ctl_data_uses in this package, so seed rows
# with raw SQL via a function-scoped db fixture that rolls back on teardown).
import uuid

import pytest
import sqlalchemy
from sqlalchemy.orm import Session

from fides.api.privacycare.context import (
    UNSUPPORTED_SOURCE_ROOTS,
    GenerationTarget,
    build_context,
    resolve_source,
    select_targets,
    unresolvable_roots,
)

DB_URL = "postgresql://postgres:fides@127.0.0.1:5442/fides"


@pytest.fixture
def db():
    engine = sqlalchemy.create_engine(DB_URL)
    with Session(engine) as session:
        yield session
        session.rollback()


def _seed_system(
    db, fides_key: str, *, name=None, requires_dpa=False, uses_profiling=False
) -> str:
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
            " true, false, :uses_profiling, false, :requires_dpa, false, "
            " false, false, false)"
        ),
        {
            "id": system_id,
            "fides_key": fides_key,
            "name": name,
            "description": f"{fides_key} description",
            "requires_dpa": requires_dpa,
            "uses_profiling": uses_profiling,
        },
    )
    return system_id


def _seed_declaration(
    db,
    system_id: str,
    data_use: str,
    *,
    name=None,
    categories=None,
    processes_special_category_data=False,
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
            " :processes_special_category_data, false, true, 'Consent', "
            " '7 years')"
        ),
        {
            "id": decl_id,
            "name": name,
            "data_use": data_use,
            "categories": categories or ["user.contact.email"],
            "system_id": system_id,
            "processes_special_category_data": processes_special_category_data,
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


def test_select_targets_high_risk_only_ignores_other_gdpr_triggers(db):
    # Guards OQ-PRIVACY-10 with teeth: uses_profiling and
    # processes_special_category_data are real GDPR Article 35 triggers, but
    # deciding they also constitute a Kenyan DPA 2019 trigger is a legal
    # call reserved for the customer's privacy SME, not engineering. A
    # system that trips both of those, yet has
    # requires_data_protection_assessments=False, must still be excluded —
    # if the predicate is ever widened to OR them in, this must start
    # failing.
    key = f"sys-{uuid.uuid4().hex[:6]}"
    sid = _seed_system(db, key, requires_dpa=False, uses_profiling=True)
    _seed_declaration(
        db,
        sid,
        "marketing.advertising",
        processes_special_category_data=True,
    )
    db.flush()

    assert select_targets(db, [key], high_risk_only=True) == []


def test_select_targets_returns_nothing_when_no_system_matches(db):
    assert select_targets(db, ["no-such-system"], high_risk_only=False) == []


def test_select_targets_treats_an_empty_list_as_no_systems_not_all_systems(db):
    # None and [] are opposite requests. None = "every system"; [] = "none".
    # Collapsing the second into the first runs a caller's explicit
    # narrowing over the whole estate.
    key = f"sys-{uuid.uuid4().hex[:6]}"
    _seed_declaration(db, _seed_system(db, key), "marketing.advertising")
    db.flush()

    assert select_targets(db, [], high_risk_only=False) == []
    assert select_targets(db, None, high_risk_only=False) != []


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


def test_build_context_joins_data_categories_in_declaration_order_with_fallback(db):
    # ctl_data_categories ships pre-seeded with the real Fides default
    # taxonomy (like ctl_data_uses), so "user.contact.email" resolves to its
    # real name/description. The second key is deliberately made-up so the
    # no-taxonomy-row fallback (name -> the key itself, description -> None)
    # is exercised in the same call, in the declaration's own order.
    unregistered_key = f"custom.category.{uuid.uuid4().hex[:6]}"
    key = f"sys-{uuid.uuid4().hex[:6]}"
    sid = _seed_system(db, key, name="CRM")
    _seed_declaration(
        db, sid, "marketing.advertising",
        categories=["user.contact.email", unregistered_key],
    )
    db.flush()

    target = next(t for t in select_targets(db, [key], high_risk_only=False))
    context = build_context(db, target)

    assert context["data_category"]["name"] == [
        "User Contact Email",
        unregistered_key,
    ]
    assert context["data_category"]["description"] == [
        "User's contact email address.",
        None,
    ]
    assert resolve_source(context, "data_category.name") == (
        f"User Contact Email, {unregistered_key}"
    )


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


def test_resolve_source_returns_none_for_a_list_of_only_nones():
    # A list can be non-empty and still carry no fact — every element
    # absent. That must collapse to None too, not to an empty string
    # rendered from joining nothing.
    context = {"privacy_declaration": {"data_categories": [None, None]}}
    assert resolve_source(context, "privacy_declaration.data_categories") is None


@pytest.mark.parametrize("blank", ["", "   "])
def test_resolve_source_returns_none_for_a_blank_string(blank):
    # An empty or whitespace-only string is present but says nothing — it
    # must read as absent, the same as None or [], not as "" in a DPIA
    # answer.
    context = {"system": {"name": blank}}
    assert resolve_source(context, "system.name") is None


def test_unresolvable_roots_flags_unsupported_roots_regardless_of_context():
    # privacy_notice and connection have no data in ANY context — they are
    # phase-1-unsupported Fides subsystems, not merely absent-here facts.
    context = {"system": {"name": "CRM"}}
    paths = ["system.name", "privacy_notice.name", "connection.name"]

    assert unresolvable_roots(context, paths) == {"privacy_notice", "connection"}


def test_unresolvable_roots_flags_a_supported_root_with_nothing_to_say():
    # privacy_declaration IS supported — build_context always populates it —
    # but this declaration's own data_categories is empty, so the source
    # still resolves to None for this specific assessment.
    context = {"privacy_declaration": {"data_categories": []}}

    assert unresolvable_roots(
        context, ["privacy_declaration.data_categories"]
    ) == {"privacy_declaration"}


def test_unresolvable_roots_is_empty_when_every_source_resolves():
    context = {"system": {"name": "CRM"}}
    assert unresolvable_roots(context, ["system.name"]) == set()


def test_unsupported_source_roots_names_exactly_the_five_phase_1_gaps():
    # Locks the constant's membership: adding or removing an entry here is a
    # deliberate, reviewed decision, not an incidental edit.
    assert UNSUPPORTED_SOURCE_ROOTS == {
        "privacy_notice",
        "privacy_experience",
        "policy",
        "connection",
        "fides",
    }
