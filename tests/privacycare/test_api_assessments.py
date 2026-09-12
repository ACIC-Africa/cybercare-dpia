# Endpoint behaviour against real rows. Inserts are rolled back.
import json
import uuid

import pytest
import sqlalchemy
from sqlalchemy.orm import Session

from fides.api.privacycare.api.assessments import (
    _assessment_detail,
    _assessment_to_response,
    _evidence_for,
    _grouped_assessments,
    _list_assessments,
    _list_templates,
    _questions_for,
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
    system: str | None = "sys_test",
    task_id: str | None = None,
) -> str:
    # status/risk_level/created_by/data_use/data_use_name/system/task_id are
    # all optional kwargs (defaulting to the original single-status
    # behaviour, system="sys_test") so the summary tests below can drive
    # every AssessmentSummarySegment and the blocked_groups/owners
    # aggregation without a second seed helper, and existing
    # positional/keyword callers keep working unchanged. `task_id` is
    # additive, same pattern as `system` before it (task 2).
    aid = f"asmt_{uuid.uuid4().hex[:8]}"
    db.execute(
        sqlalchemy.text(
            "INSERT INTO privacy_assessment "
            "(id, template_id, name, status, system_fides_key, risk_level, "
            " created_by, data_use, data_use_name, privacy_assessment_task_id) "
            "VALUES (:id, :tid, :name, :status, :system, :risk_level, "
            " :created_by, :data_use, :data_use_name, :task_id)"
        ),
        {
            "id": aid,
            "tid": template_id,
            "name": name,
            "status": status,
            "system": system,
            "risk_level": risk_level,
            "created_by": created_by,
            "data_use": data_use,
            "data_use_name": data_use_name,
            "task_id": task_id,
        },
    )
    return aid


def _seed_task(
    db,
    *,
    use_llm: bool = False,
    llm_model: str | None = None,
) -> str:
    # Columns per the brief: id, created_at, updated_at, action_type, status,
    # celery_id, total_count, completed_count, message, assessment_types,
    # system_fides_keys, created_by, use_llm, llm_model, high_risk_only.
    # action_type/status/celery_id/assessment_types are NOT NULL with no
    # server default for the first three (verified against the live schema);
    # total_count/completed_count/use_llm/high_risk_only all have defaults,
    # supplied explicitly here anyway for a self-contained seed row.
    task_id = f"task_{uuid.uuid4().hex[:8]}"
    db.execute(
        sqlalchemy.text(
            "INSERT INTO privacy_assessment_task "
            "(id, action_type, status, celery_id, total_count, completed_count, "
            " assessment_types, use_llm, llm_model, high_risk_only) "
            "VALUES (:id, 'generate', 'complete', :celery_id, 0, 0, "
            " '{}', :use_llm, :llm_model, false)"
        ),
        {
            "id": task_id,
            "celery_id": f"celery_{uuid.uuid4().hex[:8]}",
            "use_llm": use_llm,
            "llm_model": llm_model,
        },
    )
    return task_id


def test_assessments_group_by_data_use(db):
    tid = _seed_template(db)
    a1 = _seed_assessment(db, tid, "Onboarding A", data_use="essential.service")
    a2 = _seed_assessment(db, tid, "Onboarding B", data_use="essential.service")
    a3 = _seed_assessment(db, tid, "Marketing", data_use="marketing.advertising")
    db.flush()
    groups = {g.data_use: g for g in _grouped_assessments(db)}
    assert "essential.service" in groups and "marketing.advertising" in groups
    ids = {a.id for a in groups["essential.service"].assessments}
    assert {a1, a2} <= ids
    assert a3 not in ids


def test_system_count_counts_distinct_systems_not_assessments(db):
    tid = _seed_template(db)
    _seed_assessment(db, tid, "S1", data_use="shared.use", system="sys_one")
    _seed_assessment(db, tid, "S2", data_use="shared.use", system="sys_one")
    _seed_assessment(db, tid, "S3", data_use="shared.use", system="sys_two")
    db.flush()
    group = next(g for g in _grouped_assessments(db) if g.data_use == "shared.use")
    assert group.system_count == 2, "three assessments across two systems"


def test_null_data_use_forms_its_own_group(db):
    tid = _seed_template(db)
    aid = _seed_assessment(db, tid, "Ungrouped", data_use=None)
    db.flush()
    group = next(g for g in _grouped_assessments(db) if g.data_use is None)
    assert aid in {a.id for a in group.assessments}


def test_status_filter_returns_only_matching_assessments(db):
    # Fix round 1: the UI's status filter (statusFilter in
    # pages/privacy-assessments/index.tsx, passed as {status: statusFilter})
    # was accepted and silently dropped by FastAPI — the route never read a
    # status query param. This asserts the filter actually filters.
    tid = _seed_template(db)
    completed = _seed_assessment(
        db, tid, "Completed", status="completed", data_use="essential.service"
    )
    in_progress = _seed_assessment(
        db, tid, "In progress", status="in_progress", data_use="essential.service"
    )
    db.flush()
    groups = _grouped_assessments(db, status="completed")
    ids = {a.id for g in groups for a in (g.assessments or [])}
    assert completed in ids
    assert in_progress not in ids


def test_status_filter_omitted_returns_everything(db):
    tid = _seed_template(db)
    completed = _seed_assessment(db, tid, "Completed", status="completed")
    in_progress = _seed_assessment(db, tid, "In progress", status="in_progress")
    db.flush()
    groups = _grouped_assessments(db)
    ids = {a.id for g in groups for a in (g.assessments or [])}
    assert completed in ids
    assert in_progress in ids


def test_unknown_status_returns_empty_not_an_error(db):
    tid = _seed_template(db)
    _seed_assessment(db, tid, "Completed", status="completed")
    db.flush()
    groups = _grouped_assessments(db, status="not_a_real_status")
    ids = {a.id for g in groups for a in (g.assessments or [])}
    assert ids == set()


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


def _seed_question(db, template_id: str, key: str, group: str, order: int) -> str:
    qid = f"q_{uuid.uuid4().hex[:8]}"
    db.execute(
        sqlalchemy.text(
            "INSERT INTO assessment_question "
            "(id, template_id, requirement_key, requirement_title, group_order, "
            " question_key, question_text, question_order, required) "
            "VALUES (:id, :tid, :rk, :rt, :go, :qk, 'Is this processing necessary?', 1, true)"
        ),
        {"id": qid, "tid": template_id, "rk": group, "rt": group.title(),
         "go": order, "qk": key},
    )
    return qid


def test_questions_are_grouped_by_requirement(db):
    tid = _seed_template(db)
    aid = _seed_assessment(db, tid, "Grouped DPIA")
    _seed_question(db, tid, "q1", "necessity", 1)
    _seed_question(db, tid, "q2", "necessity", 1)
    _seed_question(db, tid, "q3", "security", 2)
    db.flush()
    groups = _questions_for(db, aid)
    keys = [g["requirement_key"] for g in groups]
    assert keys == ["necessity", "security"], "groups must be ordered by group_order"
    assert len(groups[0]["questions"]) == 2


def test_unknown_assessment_yields_no_groups(db):
    assert _questions_for(db, "nope") == []


def _seed_answer_with_evidence(
    db,
    assessment_id: str,
    question_id: str,
    evidence: dict,
    *,
    answer_source: str = "ai_analysis",
    created_by: str = "ai@cybota.com",
) -> str:
    # assessment_answer <-> answer_version is a circular FK (verified against
    # the live migration): the answer row has to exist before a version can
    # point back at it via answer_id, and current_version_id has to be
    # backfilled afterward. `evidence` is JSONB — CAST is required because a
    # bound text parameter has no implicit cast to jsonb in Postgres.
    answer_id = f"aa_{uuid.uuid4().hex[:8]}"
    version_id = f"av_{uuid.uuid4().hex[:8]}"
    db.execute(
        sqlalchemy.text(
            "INSERT INTO assessment_answer (id, assessment_id, question_id) "
            "VALUES (:id, :assessment_id, :question_id)"
        ),
        {"id": answer_id, "assessment_id": assessment_id, "question_id": question_id},
    )
    db.execute(
        sqlalchemy.text(
            "INSERT INTO answer_version "
            "(id, answer_id, answer_status, answer_source, change_type, "
            " created_by, evidence) "
            "VALUES (:id, :answer_id, 'complete', :answer_source, 'ai_generated', "
            " :created_by, CAST(:evidence AS JSONB))"
        ),
        {
            "id": version_id,
            "answer_id": answer_id,
            "answer_source": answer_source,
            "created_by": created_by,
            "evidence": json.dumps(evidence),
        },
    )
    db.execute(
        sqlalchemy.text(
            "UPDATE assessment_answer SET current_version_id = :version_id "
            "WHERE id = :id"
        ),
        {"version_id": version_id, "id": answer_id},
    )
    return answer_id


def test_evidence_is_empty_when_no_answer_has_recorded_evidence(db):
    # `evidence` is JSONB NOT NULL with server_default '{}' — an answer that
    # never had evidence attached still has a row, just with the untouched
    # default. That must not surface as an evidence item.
    tid = _seed_template(db)
    aid = _seed_assessment(db, tid, "No Evidence DPIA")
    qid = _seed_question(db, tid, "q1", "necessity", 1)
    _seed_answer_with_evidence(db, aid, qid, {})
    db.flush()
    assert _evidence_for(db, aid) == {
        "assessment_id": aid,
        "total_count": 0,
        "items": [],
    }


def test_evidence_returns_real_rows_from_answer_version(db):
    # The earlier draft of this plan stubbed this endpoint to `[]` and told
    # the implementer not to invent an evidence table. That was wrong:
    # evidence lives on answer_version.evidence. This asserts it is actually
    # read and returned, not stubbed.
    tid = _seed_template(db)
    aid = _seed_assessment(db, tid, "Evidence DPIA")
    qid = _seed_question(db, tid, "q1", "necessity", 1)
    _seed_answer_with_evidence(
        db,
        aid,
        qid,
        {
            "id": "ev_1",
            "type": "ai_analysis",
            "value": "System X processes payroll data.",
            "created_at": "2026-09-01T00:00:00+00:00",
            "field_name": "data_categories",
            "source_type": "system",
            "citation_number": 1,
        },
        answer_source="ai_analysis",
        created_by="scribe@cybota.com",
    )
    db.flush()
    out = _evidence_for(db, aid)
    assert out["assessment_id"] == aid
    assert out["total_count"] == 1
    item = out["items"][0]
    assert item["id"] == "ev_1"
    assert item["type"] == "ai_analysis"
    assert item["value"] == "System X processes payroll data."
    assert item["field_name"] == "data_categories"
    assert item["source_type"] == "system"
    assert item["citation_number"] == 1


def test_evidence_skips_payloads_missing_the_fields_evidenceitem_requires(db):
    # A JSON object in `evidence` that doesn't carry `id`/`type` cannot be
    # turned into an EvidenceItem without inventing values, so it is
    # dropped rather than papered over.
    tid = _seed_template(db)
    aid = _seed_assessment(db, tid, "Malformed Evidence DPIA")
    qid = _seed_question(db, tid, "q1", "necessity", 1)
    _seed_answer_with_evidence(db, aid, qid, {"value": "no id or type here"})
    db.flush()
    assert _evidence_for(db, aid)["items"] == []


def test_evidence_skip_is_logged_not_silent(db, caplog):
    # Fix round 1: the skip above used to leave zero trace. A dropped
    # evidence row on a real DPIA is a silent gap in a regulatory record --
    # worse than a loud failure, because nobody notices to fix it. This
    # project's `caplog` fixture is patched by the installed pytest-loguru
    # plugin (tests/fides/api/middleware/test_logging.py exercises the same
    # loguru->caplog wiring via its own `loguru_caplog` fixture; that
    # fixture isn't reachable from tests/privacycare's conftest scope, but
    # plain `caplog` here is already the pytest-loguru-patched one, verified
    # by `uv run --python 3.13 pytest tests/privacycare --fixtures -q`
    # listing only pytest_loguru's caplog for this directory).
    caplog.set_level("WARNING")
    tid = _seed_template(db)
    aid = _seed_assessment(db, tid, "Malformed Evidence DPIA")
    qid = _seed_question(db, tid, "q1", "necessity", 1)
    _seed_answer_with_evidence(db, aid, qid, {"value": "no id or type here"})
    db.flush()
    out = _evidence_for(db, aid)
    assert out["items"] == []
    assert aid in caplog.text
    assert qid in caplog.text
    assert "id" in caplog.text and "type" in caplog.text


def test_detail_carries_question_groups(db):
    tid = _seed_template(db)
    aid = _seed_assessment(db, tid, "Detail DPIA")
    _seed_question(db, tid, "q1", "necessity", 1)
    _seed_question(db, tid, "q2", "necessity", 1)
    db.flush()
    detail = _assessment_detail(db, aid)
    assert detail.name == "Detail DPIA"
    assert len(detail.question_groups) == 1
    group = detail.question_groups[0]
    assert group.requirement_key == "necessity"
    assert group.total_count == 2
    assert group.answered_count == 0, "no answers seeded"


def test_detail_question_id_is_the_real_id_and_id_is_the_label(db):
    tid = _seed_template(db)
    aid = _seed_assessment(db, tid, "Label DPIA")
    qid = _seed_question(db, tid, "Q7", "security", 1)
    db.flush()
    question = _assessment_detail(db, aid).question_groups[0].questions[0]
    assert question.question_id == qid, "question_id must be the database id"
    assert question.id == "Q7", "id is the display label the UI renders"


def test_detail_fields_without_a_source_are_empty_not_missing(db):
    tid = _seed_template(db)
    aid = _seed_assessment(db, tid, "Empty Fields DPIA")
    _seed_question(db, tid, "q1", "necessity", 1)
    db.flush()
    detail = _assessment_detail(db, aid)
    assert detail.questionnaire is None
    question = detail.question_groups[0].questions[0]
    assert question.missing_data == []
    assert question.sme_prompt is None
    assert detail.question_groups[0].risk_level is None


def test_detail_metadata_comes_from_the_generation_task(db):
    tid = _seed_template(db)
    task_id = _seed_task(db, use_llm=True, llm_model="claude-sonnet-5")
    aid = _seed_assessment(db, tid, "Task DPIA", task_id=task_id)
    db.flush()
    meta = _assessment_detail(db, aid).metadata
    assert meta is not None
    assert meta.use_llm is True
    assert meta.model_used == "claude-sonnet-5"
    assert meta.generation_timestamp


def test_detail_without_a_task_has_null_metadata(db):
    tid = _seed_template(db)
    aid = _seed_assessment(db, tid, "No Task DPIA")
    db.flush()
    assert _assessment_detail(db, aid).metadata is None


def test_unknown_detail_raises(db):
    with pytest.raises(LookupError):
        _assessment_detail(db, "no-such-assessment")
