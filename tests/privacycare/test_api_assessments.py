# Endpoint behaviour against real rows. Inserts are rolled back.
import uuid

import pytest
import sqlalchemy
from sqlalchemy.orm import Session

from fides.api.privacycare.api.assessments import (
    _assessment_to_response,
    _list_assessments,
    _list_templates,
    _summary,
)

DB_URL = "postgresql://postgres:fides@127.0.0.1:5442/fides"


@pytest.fixture
def db():
    engine = sqlalchemy.create_engine(DB_URL)
    with Session(engine) as session:
        yield session
        session.rollback()


def _seed_template(db) -> str:
    # assessment_type and region are NOT NULL on the live table (not
    # mentioned in the task brief's column list) — supply both or the
    # insert violates a not-null constraint.
    tid = f"tpl_{uuid.uuid4().hex[:8]}"
    db.execute(
        sqlalchemy.text(
            "INSERT INTO assessment_template "
            "(id, version, name, assessment_type, region, is_active) "
            "VALUES (:id, '1.0', 'Kenya DPA 2019 DPIA', 'dpia', 'KE', true)"
        ),
        {"id": tid},
    )
    return tid


def _seed_template_named(db, name: str) -> str:
    # Same NOT NULL constraints as _seed_template, but lets the caller pick
    # a name — used to exercise the template_key() id fallback with a name
    # that collapses to an empty slug.
    tid = f"tpl_{uuid.uuid4().hex[:8]}"
    db.execute(
        sqlalchemy.text(
            "INSERT INTO assessment_template "
            "(id, version, name, assessment_type, region, is_active) "
            "VALUES (:id, '1.0', :name, 'dpia', 'KE', true)"
        ),
        {"id": tid, "name": name},
    )
    return tid


def _seed_assessment(db, template_id: str, name: str) -> str:
    aid = f"asmt_{uuid.uuid4().hex[:8]}"
    db.execute(
        sqlalchemy.text(
            "INSERT INTO privacy_assessment "
            "(id, template_id, name, status, system_fides_key) "
            "VALUES (:id, :tid, :name, 'in_progress', 'sys_test')"
        ),
        {"id": aid, "tid": template_id, "name": name},
    )
    return aid


def test_list_returns_seeded_assessments(db):
    tid = _seed_template(db)
    aid = _seed_assessment(db, tid, "Fuel Dealer Onboarding DPIA")
    db.flush()
    rows = _list_assessments(db)
    ids = [r.id for r in rows]
    assert aid in ids


def test_response_carries_the_template_name(db):
    tid = _seed_template(db)
    aid = _seed_assessment(db, tid, "Payroll DPIA")
    db.flush()
    row = next(r for r in _list_assessments(db) if r.id == aid)
    response = _assessment_to_response(row)
    assert response.name == "Payroll DPIA"
    assert response.template_name == "Kenya DPA 2019 DPIA", (
        "template_name must be joined in, not left null"
    )
    assert response.status == "in_progress"


def test_timestamps_serialise_as_strings(db):
    tid = _seed_template(db)
    aid = _seed_assessment(db, tid, "Timestamp DPIA")
    db.flush()
    row = next(r for r in _list_assessments(db) if r.id == aid)
    response = _assessment_to_response(row)
    assert response.created_at is None or isinstance(response.created_at, str), (
        "the UI contract types created_at as string | null"
    )


def test_templates_derive_the_key_the_ui_requires(db):
    tid = _seed_template(db)
    db.flush()
    tpl = next(t for t in _list_templates(db) if t.id == tid)
    assert tpl.key == "kenya_dpa_2019_dpia", (
        "key is not a column; it must be derived from the name"
    )
    assert tpl.version == "1.0"


def test_templates_fall_back_to_id_when_name_has_no_letters_or_digits(db):
    # A name that is entirely punctuation collapses to an empty slug in
    # template_key(). The endpoint must call template_key(name, id) — with
    # the id fallback — so this still comes back with a non-empty key
    # instead of colliding with every other punctuation-only template.
    tid = _seed_template_named(db, "!!!")
    db.flush()
    tpl = next(t for t in _list_templates(db) if t.id == tid)
    assert tpl.key, "id fallback must produce a non-empty key"
    assert tpl.key != ""


def test_summary_counts_by_status_and_risk(db):
    tid = _seed_template(db)
    _seed_assessment(db, tid, "Summary A")
    _seed_assessment(db, tid, "Summary B")
    db.flush()
    out = _summary(db)
    assert out["total"] >= 2
    assert out["by_status"].get("in_progress", 0) >= 2
    assert "by_risk_level" in out
