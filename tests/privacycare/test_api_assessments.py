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


def _seed_assessment(
    db,
    template_id: str,
    name: str,
    *,
    status: str = "in_progress",
    risk_level: str | None = None,
    created_by: str | None = None,
    data_use: str | None = None,
    data_use_name: str | None = None,
) -> str:
    # status/risk_level/created_by/data_use/data_use_name are all optional
    # kwargs (defaulting to the original single-status behaviour) so the
    # summary tests below can drive every AssessmentSummarySegment and the
    # blocked_groups/owners aggregation without a second seed helper.
    aid = f"asmt_{uuid.uuid4().hex[:8]}"
    db.execute(
        sqlalchemy.text(
            "INSERT INTO privacy_assessment "
            "(id, template_id, name, status, system_fides_key, risk_level, "
            " created_by, data_use, data_use_name) "
            "VALUES (:id, :tid, :name, :status, 'sys_test', :risk_level, "
            " :created_by, :data_use, :data_use_name)"
        ),
        {
            "id": aid,
            "tid": template_id,
            "name": name,
            "status": status,
            "risk_level": risk_level,
            "created_by": created_by,
            "data_use": data_use,
            "data_use_name": data_use_name,
        },
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


def test_summary_shape_matches_the_ui_contract(db):
    # Fix round 1: _summary() previously invented a {total, by_status,
    # by_risk_level} shape instead of reading
    # clients/admin-ui/src/features/privacy-assessments/types.ts. This
    # asserts the real AssessmentSummaryResponse shape: total, by_segment
    # (all 4 segments, always present), blocked_groups, owners.
    tid = _seed_template(db)
    _seed_assessment(db, tid, "Summary A")
    _seed_assessment(db, tid, "Summary B")
    db.flush()
    out = _summary(db)
    assert set(out.keys()) == {"total", "by_segment", "blocked_groups", "owners"}
    assert out["total"] >= 2
    assert set(out["by_segment"].keys()) == {"completed", "pending", "open", "risk"}
    assert isinstance(out["blocked_groups"], list)
    assert isinstance(out["owners"], list)


def test_summary_segments_follow_status_and_risk_level(db):
    # Port of segmentForAssessment() in compute-summary.ts: completed ->
    # "completed", generating -> "pending", in_progress/outdated -> "risk"
    # if risk_level is high else "open".
    tid = _seed_template(db)
    _seed_assessment(db, tid, "Completed", status="completed")
    _seed_assessment(db, tid, "Generating", status="generating")
    _seed_assessment(db, tid, "Open", status="in_progress", risk_level="medium")
    _seed_assessment(db, tid, "At risk", status="in_progress", risk_level="high")
    _seed_assessment(db, tid, "Outdated at risk", status="outdated", risk_level="high")
    db.flush()
    out = _summary(db)
    assert out["by_segment"]["completed"] >= 1
    assert out["by_segment"]["pending"] >= 1
    assert out["by_segment"]["open"] >= 1
    assert out["by_segment"]["risk"] >= 2


def test_summary_blocked_groups_aggregate_by_data_use(db):
    # Port of the groupAgg logic in compute-summary.ts: a group only
    # appears in blocked_groups once it has an outdated or high-risk
    # assessment, named after data_use_name (or "Uncategorized").
    tid = _seed_template(db)
    _seed_assessment(
        db,
        tid,
        "Marketing outdated",
        status="outdated",
        data_use="marketing.advertising",
        data_use_name="Marketing",
    )
    _seed_assessment(
        db,
        tid,
        "Marketing fine",
        status="in_progress",
        data_use="marketing.advertising",
        data_use_name="Marketing",
    )
    db.flush()
    out = _summary(db)
    marketing = next(
        (g for g in out["blocked_groups"] if g["name"] == "Marketing"), None
    )
    assert marketing is not None, "a group with an outdated assessment must be blocked"
    assert marketing["outdated_count"] >= 1
    assert marketing["total_count"] >= 2


def test_summary_owners_aggregate_open_assessments(db):
    # Port of the ownerAgg logic in compute-summary.ts: only open
    # (in_progress/outdated) assessments with a created_by count toward an
    # owner's open_count/outdated_count.
    tid = _seed_template(db)
    _seed_assessment(
        db, tid, "Owned open", status="in_progress", created_by="alice@example.com"
    )
    _seed_assessment(
        db, tid, "Owned outdated", status="outdated", created_by="alice@example.com"
    )
    _seed_assessment(
        db, tid, "Owned but completed", status="completed", created_by="alice@example.com"
    )
    db.flush()
    out = _summary(db)
    alice = next(
        (o for o in out["owners"] if o["owner"] == "alice@example.com"), None
    )
    assert alice is not None
    assert alice["open_count"] >= 2, "completed assessments must not count as open"
    assert alice["outdated_count"] >= 1
