# Endpoint behaviour against real rows. Inserts are rolled back.
import contextlib
import json
import types
import uuid

import pytest
import sqlalchemy
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.orm import Session

from fides.api.privacycare.api import assessments as assessments_module
from fides.api.privacycare.api.answers import QuestionNotInTemplateError, write_answer
from fides.api.privacycare.api.assessments import (
    _assessment_detail,
    _assessment_to_response,
    _bulk_update_answers,
    _created_by_from_client,
    _delete_assessment,
    _evidence_for,
    _grouped_assessments,
    _list_assessments,
    _list_templates,
    _questions_for,
    _summary,
    _update_answer,
    _update_assessment,
    bulk_update_answers,
    delete_assessment,
    update_answer,
    update_assessment,
)
from fides.api.privacycare.api.schemas import (
    AnswerUpdate,
    AssessmentStatus,
    BulkUpdateAnswersRequest,
    RiskLevel,
    UpdateAnswerRequest,
    UpdatePrivacyAssessmentRequest,
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
        db,
        tid,
        "Owned but completed",
        status="completed",
        created_by="alice@example.com",
    )
    db.flush()
    out = _summary(db)
    alice = next((o for o in out["owners"] if o["owner"] == "alice@example.com"), None)
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
        {
            "id": qid,
            "tid": template_id,
            "rk": group,
            "rt": group.title(),
            "go": order,
            "qk": key,
        },
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
    answer_status: str = "complete",
    updated_at: str | None = None,
) -> str:
    # assessment_answer <-> answer_version is a circular FK (verified against
    # the live migration): the answer row has to exist before a version can
    # point back at it via answer_id, and current_version_id has to be
    # backfilled afterward. `evidence` is JSONB — CAST is required because a
    # bound text parameter has no implicit cast to jsonb in Postgres.
    # `answer_status` defaults to "complete" (the original hardcoded value)
    # so every existing caller is unaffected; task-3 fix round 1 needs a
    # "needs_input" row to pin answered_count's completion semantics.
    # `updated_at`, if given, overrides the column's now()-server-default
    # after insert — fix round 2 needs deterministic, distinguishable
    # timestamps across two seeded versions to pin
    # last_updated_at/last_updated_by's "most recently updated" derivation;
    # relying on real wall-clock ordering between two inserts in the same
    # test would be flaky.
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
            "VALUES (:id, :answer_id, :answer_status, :answer_source, 'ai_generated', "
            " :created_by, CAST(:evidence AS JSONB))"
        ),
        {
            "id": version_id,
            "answer_id": answer_id,
            "answer_status": answer_status,
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
    if updated_at is not None:
        db.execute(
            sqlalchemy.text(
                "UPDATE answer_version SET updated_at = :updated_at WHERE id = :id"
            ),
            {"updated_at": updated_at, "id": version_id},
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


def test_detail_question_evidence_skips_malformed_payload(db, caplog):
    # Fix round 2: _question_response used to inject answer_version.evidence
    # into AssessmentQuestionResponse.evidence raw and unvalidated (typed
    # List[dict]) — a payload missing `id`/`type` sailed straight through to
    # the detail response, where AssessmentDetail.tsx feeds it to the
    # evidence drawer and EvidenceCardGroup.tsx calls
    # `item.field_name!.replace(...)` on it: a missing key there is a
    # browser crash. Both evidence paths now share
    # _evidence_item_from_payload, so this must skip identically to (and log
    # the same way as) the /evidence endpoint's existing
    # test_evidence_skips_payloads_missing_the_fields_evidenceitem_requires.
    caplog.set_level("WARNING")
    tid = _seed_template(db)
    aid = _seed_assessment(db, tid, "Malformed Question Evidence DPIA")
    qid = _seed_question(db, tid, "q1", "necessity", 1)
    _seed_answer_with_evidence(db, aid, qid, {"value": "no id or type here"})
    db.flush()
    question = _assessment_detail(db, aid).question_groups[0].questions[0]
    assert question.evidence == [], (
        "a malformed evidence payload must not reach the detail response — "
        "EvidenceCardGroup.tsx would crash on a missing required field"
    )
    assert aid in caplog.text and qid in caplog.text


def test_detail_answered_count_increments_for_some_but_not_all(db):
    # Fix round 1: the only prior test asserted answered_count == 0 with no
    # answers seeded — nothing proved it ever increments. Seed a complete
    # answer for one of three questions in a group and leave the other two
    # unanswered: total_count must cover all three, answered_count only the
    # one actually answered.
    tid = _seed_template(db)
    aid = _seed_assessment(db, tid, "Partial Progress DPIA")
    q1 = _seed_question(db, tid, "q1", "necessity", 1)
    _seed_question(db, tid, "q2", "necessity", 1)
    _seed_question(db, tid, "q3", "necessity", 1)
    _seed_answer_with_evidence(db, aid, q1, {"id": "ev_1", "type": "ai_analysis"})
    db.flush()
    group = _assessment_detail(db, aid).question_groups[0]
    assert group.total_count == 3
    assert group.answered_count == 1, "only q1 has a complete answer"


def test_detail_answered_count_excludes_needs_input(db):
    # Fix round 1 semantic question, resolved by reading the UI rather than
    # assumed: QuestionGroupPanel.tsx renders answered_count/total_count as
    # completion progress (`isGroupCompleted = answeredCount === totalCount`
    # switches a "Completed"/"Pending" tag) and AssessmentDetail.tsx treats
    # AnswerStatus.NEEDS_INPUT as explicitly outstanding (it filters exactly
    # that status into `needsInputIds`, the set still awaiting a person).
    # A current answer whose status is "needs_input" must therefore NOT
    # count toward answered_count — counting it would show a group as
    # Completed while a question inside it still needs input, an
    # overstatement of completeness in a regulatory (DPIA) record.
    tid = _seed_template(db)
    aid = _seed_assessment(db, tid, "Needs Input DPIA")
    q1 = _seed_question(db, tid, "q1", "necessity", 1)
    q2 = _seed_question(db, tid, "q2", "necessity", 1)
    _seed_answer_with_evidence(
        db, aid, q1, {"id": "ev_1", "type": "ai_analysis"}, answer_status="complete"
    )
    _seed_answer_with_evidence(
        db, aid, q2, {"id": "ev_2", "type": "ai_analysis"}, answer_status="needs_input"
    )
    db.flush()
    group = _assessment_detail(db, aid).question_groups[0]
    assert group.total_count == 2
    assert group.answered_count == 1, (
        "a needs_input answer must not count as answered, "
        "or the group would show Completed while still needing input"
    )


def test_detail_answered_count_excludes_partial(db):
    # Fix round 2: fix round 1 correctly excluded needs_input but left
    # "partial" counted as answered — the same evidence was not applied to
    # it. Read AnswerStatusTags.tsx directly: COMPLETE gets its own branch
    # (a plain source-label tag). PARTIAL falls through to the SAME branch
    # NEEDS_INPUT would use if it weren't COMPLETE, and is additionally
    # wrapped in a Tooltip reading "This answer can be automatically derived
    # if you populate: ..." (or the no-missing-data variant, "...if the
    # relevant field is populated") — the UI's own copy says a partial
    # answer is not yet complete. So a group of two "partial" questions must
    # NOT read 2/2 under QuestionGroupPanel.tsx's literal "Completed" tag
    # while both cards inside still show that tooltip: that is the exact
    # overstatement-of-completeness failure this test pins down.
    tid = _seed_template(db)
    aid = _seed_assessment(db, tid, "Partial Answers DPIA")
    q1 = _seed_question(db, tid, "q1", "necessity", 1)
    q2 = _seed_question(db, tid, "q2", "necessity", 1)
    _seed_answer_with_evidence(
        db, aid, q1, {"id": "ev_1", "type": "ai_analysis"}, answer_status="partial"
    )
    _seed_answer_with_evidence(
        db, aid, q2, {"id": "ev_2", "type": "ai_analysis"}, answer_status="partial"
    )
    db.flush()
    group = _assessment_detail(db, aid).question_groups[0]
    assert group.total_count == 2
    assert group.answered_count == 0, (
        "two partial answers must not read 2/2 / 'Completed' — "
        "AnswerStatusTags.tsx renders partial as its own non-complete tag "
        "with a tooltip explaining the answer is not yet final"
    )


def test_detail_group_last_updated_derives_from_the_latest_answer_version(db):
    # Fix round 2: last_updated_at/last_updated_by were hardcoded to None
    # with a "no source in the OSS schema" comment. That claim was wrong —
    # answer_version.updated_at and .created_by exist, and _EVIDENCE_SQL
    # already selects the equivalent columns. Derive both for QuestionGroup
    # from whichever question in the group has the most recently updated
    # current answer version, not just the first one seeded.
    tid = _seed_template(db)
    aid = _seed_assessment(db, tid, "Last Updated DPIA")
    q1 = _seed_question(db, tid, "q1", "necessity", 1)
    q2 = _seed_question(db, tid, "q2", "necessity", 1)
    _seed_answer_with_evidence(
        db,
        aid,
        q1,
        {"id": "ev_1", "type": "ai_analysis"},
        created_by="alice@example.com",
        updated_at="2026-01-01T00:00:00+00:00",
    )
    _seed_answer_with_evidence(
        db,
        aid,
        q2,
        {"id": "ev_2", "type": "ai_analysis"},
        created_by="bob@example.com",
        updated_at="2026-06-01T00:00:00+00:00",
    )
    db.flush()
    group = _assessment_detail(db, aid).question_groups[0]
    assert group.last_updated_by == "bob@example.com", (
        "must derive from the MOST RECENTLY updated answer version, not "
        "simply the first one seeded"
    )
    assert group.last_updated_at is not None
    assert group.last_updated_at.startswith("2026-06-01")


def test_detail_group_last_updated_is_null_with_no_answers(db):
    tid = _seed_template(db)
    aid = _seed_assessment(db, tid, "No Answers DPIA")
    _seed_question(db, tid, "q1", "necessity", 1)
    db.flush()
    group = _assessment_detail(db, aid).question_groups[0]
    assert group.last_updated_at is None
    assert group.last_updated_by is None


def test_detail_metadata_is_null_not_a_500_when_task_created_at_is_null(db):
    # Fix round 2 (500 risk): AssessmentMetadata.generation_timestamp is
    # required and non-nullable in the TS contract
    # (features/privacy-assessments/types.ts), but
    # privacy_assessment_task.created_at is DB-nullable (verified against
    # the live migration) even though every normal write path
    # server_defaults it to now(). A task row with an explicit null
    # created_at must not raise on serialisation — it must come back as no
    # metadata, the same way a dangling task id already does.
    tid = _seed_template(db)
    task_id = f"task_{uuid.uuid4().hex[:8]}"
    db.execute(
        sqlalchemy.text(
            "INSERT INTO privacy_assessment_task "
            "(id, action_type, status, celery_id, total_count, completed_count, "
            " assessment_types, use_llm, high_risk_only, created_at) "
            "VALUES (:id, 'generate', 'complete', :celery_id, 0, 0, "
            " '{}', false, false, NULL)"
        ),
        {"id": task_id, "celery_id": f"celery_{uuid.uuid4().hex[:8]}"},
    )
    aid = _seed_assessment(db, tid, "Null Task Timestamp DPIA", task_id=task_id)
    db.flush()
    assert _assessment_detail(db, aid).metadata is None


# --- PUT .../{assessment_id}/questions/{question_id} (task 2) ---
#
# _update_answer is the non-committing core (write + recompute + assemble
# the response) — tested directly here the same way _assessment_detail/
# _evidence_for are, against a transaction the `db` fixture rolls back.
# update_answer is the actual HTTP route: a thin shell around
# _update_answer that maps its two raised exceptions to 404 and commits on
# success. Its 404 paths are exercised directly below too — safe against
# the same rollback, since write_answer raises before either path ever
# writes anything. Its commit path is exercised via monkeypatch (see
# test_update_answer_route_commits_on_success) rather than a real commit,
# so no test here ever persists rows past this file's `db` fixture.
#
# `client` is never resolved through FastAPI's dependency injection in any
# of these tests (same as `db` above, which bypasses Depends(get_db)
# entirely) — a plain object exposing `.user_id` is all update_answer reads
# off it.
def _fake_client(user_id, *, id="client_fake_default"):
    # `id` matters now too: _created_by_from_client falls back to it when
    # user_id is None (fix round 1, MAJOR finding 2) — a real ClientDetail
    # always has one, so a stand-in must too.
    return types.SimpleNamespace(user_id=user_id, id=id)


def _seed_second_template(db) -> str:
    # _seed_template hardcodes assessment_type="dpia", version="1.0" —
    # calling it twice in one test collides with
    # uq_assessment_template_type_version_revision (UNIQUE on
    # assessment_type, version, fides_revision; same note as
    # tests/privacycare/test_answers.py's own _seed_second_template, which
    # this mirrors). A genuinely distinct second template needs a different
    # assessment_type.
    tid = f"tpl_{uuid.uuid4().hex[:8]}"
    db.execute(
        sqlalchemy.text(
            "INSERT INTO assessment_template "
            "(id, version, name, assessment_type, region, is_active) "
            "VALUES (:id, '1.0', 'A Different Template', 'gdpr', 'EU', true)"
        ),
        {"id": tid},
    )
    return tid


def test_update_answer_returns_the_question_with_the_new_answer_text(db):
    tid = _seed_template(db)
    aid = _seed_assessment(db, tid, "Update Answer DPIA")
    qid = _seed_question(db, tid, "q1", "necessity", 1)
    db.flush()

    response = _update_answer(
        db, aid, qid, "We only collect what's necessary.", "alice@example.com"
    )

    assert response.question.question_id == qid
    assert response.question.answer_text == "We only collect what's necessary."
    assert response.question.answer_status == "complete"


def test_update_answer_completeness_reflects_the_write(db):
    tid = _seed_template(db)
    aid = _seed_assessment(db, tid, "Update Completeness DPIA")
    q1 = _seed_question(db, tid, "q1", "necessity", 1)
    _seed_question(db, tid, "q2", "necessity", 1)
    db.flush()

    response = _update_answer(db, aid, q1, "Answered.", "alice@example.com")

    assert response.completeness == pytest.approx(0.5), (
        "1 of 2 questions now has a complete answer"
    )
    persisted = db.execute(
        sqlalchemy.text("SELECT completeness FROM privacy_assessment WHERE id = :id"),
        {"id": aid},
    ).scalar()
    assert persisted == pytest.approx(0.5), (
        "completeness must be persisted, not just returned in the envelope"
    )


def test_update_answer_status_is_the_assessments_not_the_answers(db):
    # The response's `status` must be privacy_assessment.status
    # ("outdated" here), never answer_version.answer_status ("complete" —
    # write_answer's fixed default for a human-typed save). The two ride
    # along in the same envelope and are easy to conflate.
    tid = _seed_template(db)
    aid = _seed_assessment(db, tid, "Status DPIA", status="outdated")
    qid = _seed_question(db, tid, "q1", "necessity", 1)
    db.flush()

    response = _update_answer(db, aid, qid, "Answered.", "alice@example.com")

    assert response.status == "outdated"
    assert response.question.answer_status == "complete"


def test_update_answer_created_by_comes_from_the_caller_not_the_body(db, monkeypatch):
    # Fix round 1, MAJOR finding 1: the original version of this test called
    # _update_answer directly, which proves the CORE records whatever
    # created_by it's handed — it proves nothing about where the ROUTE gets
    # that value from. A regression that wired the route's created_by to
    # the request body, or to a hardcoded string, would have shipped green
    # under that version. This now goes through the actual PUT route
    # (update_answer) with an authenticated client, and asserts the
    # PERSISTED author is that client's user_id — a value that appears
    # NOWHERE in the request body, so it could only have come from the
    # client. Commit is monkeypatched (same reason as
    # test_update_answer_route_commits_on_success below): a real commit
    # would persist this test's rows past the `db` fixture's rollback.
    tid = _seed_template(db)
    aid = _seed_assessment(db, tid, "Created By DPIA")
    qid = _seed_question(db, tid, "q1", "necessity", 1)
    db.flush()

    monkeypatch.setattr(db, "commit", lambda: None)

    update_answer(
        aid,
        qid,
        UpdateAnswerRequest(answer_text="Answered."),
        db=db,
        client=_fake_client("carol@example.com", id="client_should_not_be_used"),
    )

    created_by = db.execute(
        sqlalchemy.text(
            "SELECT av.created_by FROM assessment_answer a "
            "JOIN answer_version av ON av.id = a.current_version_id "
            "WHERE a.assessment_id = :aid AND a.question_id = :qid"
        ),
        {"aid": aid, "qid": qid},
    ).scalar()
    assert created_by == "carol@example.com", (
        "created_by must be the AUTHENTICATED CLIENT's user, not the "
        "request body (which has no created_by field at all) and not a "
        "hardcoded/forged value"
    )


def test_created_by_from_client_uses_the_linked_user_when_present():
    client = _fake_client("alice@example.com", id="client_irrelevant")
    assert _created_by_from_client(client) == "alice@example.com"


def test_created_by_from_client_falls_back_to_a_marked_client_id_when_unlinked():
    # Fix round 1, MAJOR finding 2: a SYSTEM_READ client with no linked
    # FidesUser (client.user_id is None) is a LEGITIMATE, ordinary caller —
    # the root client, and any standalone machine-to-machine API client
    # created via POST /api/v1/oauth/client, both reach this route with
    # user_id=None (see _created_by_from_client's own docstring for the
    # file:line evidence). Neither is rejected; both must still produce a
    # non-null, attributable author.
    client = _fake_client(None, id="api_client_abc123")
    result = _created_by_from_client(client)
    assert result == "client:api_client_abc123"
    assert result is not None


def test_update_answer_route_never_persists_a_null_author(db, monkeypatch):
    # End-to-end version of the unit test above: a client with no linked
    # user must still produce a non-null, marked-as-client author in the
    # actual persisted answer_version row — not NULL, which
    # answer_version.created_by's column permits at the DB level (an
    # Ethyca migration made it nullable) but this feature's audit-trail
    # contract forbids.
    tid = _seed_template(db)
    aid = _seed_assessment(db, tid, "Unlinked Client DPIA")
    qid = _seed_question(db, tid, "q1", "necessity", 1)
    db.flush()

    monkeypatch.setattr(db, "commit", lambda: None)

    update_answer(
        aid,
        qid,
        UpdateAnswerRequest(answer_text="Answered by a bare API client."),
        db=db,
        client=_fake_client(None, id="api_client_no_user"),
    )

    created_by = db.execute(
        sqlalchemy.text(
            "SELECT av.created_by FROM assessment_answer a "
            "JOIN answer_version av ON av.id = a.current_version_id "
            "WHERE a.assessment_id = :aid AND a.question_id = :qid"
        ),
        {"aid": aid, "qid": qid},
    ).scalar()
    assert created_by == "client:api_client_no_user"
    assert created_by is not None


def test_update_answer_route_maps_unknown_assessment_to_404(db):
    with pytest.raises(HTTPException) as exc_info:
        update_answer(
            "no-such-assessment",
            "no-such-question",
            UpdateAnswerRequest(answer_text="x"),
            db=db,
            client=_fake_client("alice@example.com"),
        )
    assert exc_info.value.status_code == 404


def test_update_answer_route_maps_foreign_question_to_404(db):
    tid = _seed_template(db)
    other_tid = _seed_second_template(db)
    aid = _seed_assessment(db, tid, "Own Template DPIA")
    foreign_qid = _seed_question(db, other_tid, "q1", "necessity", 1)
    db.flush()

    with pytest.raises(HTTPException) as exc_info:
        update_answer(
            aid,
            foreign_qid,
            UpdateAnswerRequest(answer_text="Should not be written."),
            db=db,
            client=_fake_client("alice@example.com"),
        )
    assert exc_info.value.status_code == 404

    assert (
        db.execute(
            sqlalchemy.text(
                "SELECT COUNT(*) FROM assessment_answer "
                "WHERE assessment_id = :aid AND question_id = :qid"
            ),
            {"aid": aid, "qid": foreign_qid},
        ).scalar()
        == 0
    ), "a 404'd write must not leave a partial row behind"


def test_update_answer_route_commits_on_success(db, monkeypatch):
    # write_answer/recompute_completeness/_update_answer never commit (the
    # "caller owns the transaction" convention documented in
    # api/answers.py) — update_answer is the one place that must, or every
    # write in this feature would silently vanish once the request's
    # session closes. Monkeypatched rather than a real db.commit(): a real
    # commit here would persist this test's seeded rows past the `db`
    # fixture's rollback and into the shared database.
    tid = _seed_template(db)
    aid = _seed_assessment(db, tid, "Commit DPIA")
    qid = _seed_question(db, tid, "q1", "necessity", 1)
    db.flush()

    committed = []
    monkeypatch.setattr(db, "commit", lambda: committed.append(True))

    update_answer(
        aid,
        qid,
        UpdateAnswerRequest(answer_text="Answered."),
        db=db,
        client=_fake_client("alice@example.com"),
    )

    assert committed, "update_answer must commit once _update_answer succeeds"


# --- PUT .../{assessment_id}/questions (task 3: bulk save) ---
#
# _bulk_update_answers is the non-committing core (write the whole batch +
# recompute once + assemble the response) — tested directly here the same
# way _update_answer is above. bulk_update_answers is the actual HTTP
# route: a thin shell that maps LookupError/QuestionNotInTemplateError to
# 404 and commits on success.


def test_bulk_update_writes_all_answers_and_reports_the_count(db):
    tid = _seed_template(db)
    aid = _seed_assessment(db, tid, "Bulk Two Writes DPIA")
    q1 = _seed_question(db, tid, "q1", "necessity", 1)
    q2 = _seed_question(db, tid, "q2", "necessity", 1)
    db.flush()

    response = _bulk_update_answers(
        db,
        aid,
        [
            AnswerUpdate(question_id=q1, answer_text="Answer one."),
            AnswerUpdate(question_id=q2, answer_text="Answer two."),
        ],
        "alice@example.com",
    )

    assert response.updated_count == 2
    texts = {q.question_id: q.answer_text for q in response.questions}
    assert texts[q1] == "Answer one."
    assert texts[q2] == "Answer two."


def test_bulk_update_recomputes_completeness_once_not_per_answer(db, monkeypatch):
    # Fix-round-shaped proof: correctness of the final completeness number
    # alone can't distinguish "recomputed once" from "recomputed per
    # answer" (both land on the same value for this batch). Spy on the
    # actual call count instead.
    tid = _seed_template(db)
    aid = _seed_assessment(db, tid, "Bulk Recompute Once DPIA")
    q1 = _seed_question(db, tid, "q1", "necessity", 1)
    q2 = _seed_question(db, tid, "q2", "necessity", 1)
    db.flush()

    calls = []
    original = assessments_module.recompute_completeness

    def _counting_recompute(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(
        assessments_module, "recompute_completeness", _counting_recompute
    )

    response = _bulk_update_answers(
        db,
        aid,
        [
            AnswerUpdate(question_id=q1, answer_text="Answer one."),
            AnswerUpdate(question_id=q2, answer_text="Answer two."),
        ],
        "alice@example.com",
    )

    assert len(calls) == 1, (
        "completeness must be recomputed ONCE for the whole batch, not once per answer"
    )
    assert response.completeness == pytest.approx(1.0), "both of 2 questions answered"


def test_bulk_update_bad_question_id_rolls_back_the_whole_batch(db):
    # The atomicity decision this plan makes explicit: a batch with one bad
    # question_id must roll back EVERY write it attempted, including the
    # good entries that preceded the bad one — a partially-applied batch
    # that reports success is the worst outcome for a document a regulator
    # will read.
    tid = _seed_template(db)
    other_tid = _seed_second_template(db)
    aid = _seed_assessment(db, tid, "Bulk Rollback DPIA")
    q1 = _seed_question(db, tid, "q1", "necessity", 1)
    q2 = _seed_question(db, tid, "q2", "necessity", 1)
    foreign_qid = _seed_question(db, other_tid, "q3", "necessity", 1)
    db.flush()

    with pytest.raises(QuestionNotInTemplateError):
        _bulk_update_answers(
            db,
            aid,
            [
                AnswerUpdate(question_id=q1, answer_text="Good answer 1."),
                AnswerUpdate(question_id=q2, answer_text="Good answer 2."),
                AnswerUpdate(question_id=foreign_qid, answer_text="Bad."),
            ],
            "alice@example.com",
        )

    handle_count = db.execute(
        sqlalchemy.text(
            "SELECT COUNT(*) FROM assessment_answer WHERE assessment_id = :aid"
        ),
        {"aid": aid},
    ).scalar()
    assert handle_count == 0, (
        "zero assessment_answer handles must exist — including for q1/q2, "
        "which succeeded before the bad entry raised"
    )

    version_count = db.execute(
        sqlalchemy.text(
            "SELECT COUNT(*) FROM answer_version av "
            "JOIN assessment_answer a ON a.id = av.answer_id "
            "WHERE a.assessment_id = :aid"
        ),
        {"aid": aid},
    ).scalar()
    assert version_count == 0, (
        "zero answer_version rows must exist — a bad entry anywhere in the "
        "batch must undo every write the batch made, not just skip itself"
    )

    persisted_completeness = db.execute(
        sqlalchemy.text("SELECT completeness FROM privacy_assessment WHERE id = :id"),
        {"id": aid},
    ).scalar()
    assert persisted_completeness in (None, 0.0), (
        "completeness must not have been recomputed/persisted off a batch that failed"
    )


def test_bulk_update_empty_answers_list_is_a_no_op(db):
    tid = _seed_template(db)
    aid = _seed_assessment(db, tid, "Bulk Empty Batch DPIA")
    _seed_question(db, tid, "q1", "necessity", 1)
    db.flush()

    response = _bulk_update_answers(db, aid, [], "alice@example.com")

    assert response.updated_count == 0
    assert response.completeness == 0.0
    count = db.execute(
        sqlalchemy.text(
            "SELECT COUNT(*) FROM assessment_answer WHERE assessment_id = :aid"
        ),
        {"aid": aid},
    ).scalar()
    assert count == 0, "an empty batch must be a no-op, not write anything"


def test_bulk_update_questions_field_carries_the_full_set_not_just_updated(db):
    # THE DECISION: `questions` returns the assessment's FULL question set,
    # not only the entries this batch touched. Evidence (see
    # _all_questions_response's docstring in assessments.py):
    # bulkUpdateAssessmentAnswers (privacy-assessments.slice.ts) has no
    # onQueryStarted/updateQueryData of its own — it only invalidatesTags
    # "Privacy Assessment", which triggers a getAssessment refetch, and
    # AssessmentDetail.tsx reads its questions exclusively off that query's
    # question_groups (wholesale replacement). Returning only q1 here would
    # be the exact failure mode the brief warns about if any future
    # component reads this field directly instead of waiting on that
    # refetch: every untouched question blanked out.
    tid = _seed_template(db)
    aid = _seed_assessment(db, tid, "Bulk Full Set DPIA")
    q1 = _seed_question(db, tid, "q1", "necessity", 1)
    q2 = _seed_question(db, tid, "q2", "necessity", 1)
    db.flush()

    response = _bulk_update_answers(
        db,
        aid,
        [AnswerUpdate(question_id=q1, answer_text="Only q1 touched.")],
        "alice@example.com",
    )

    ids = {q.question_id for q in response.questions}
    assert ids == {q1, q2}, (
        "questions must carry the assessment's full question set — a "
        "subset would blank out q2, which this batch never touched"
    )


def test_bulk_update_route_commits_on_success(db, monkeypatch):
    tid = _seed_template(db)
    aid = _seed_assessment(db, tid, "Bulk Commit DPIA")
    qid = _seed_question(db, tid, "q1", "necessity", 1)
    db.flush()

    committed = []
    monkeypatch.setattr(db, "commit", lambda: committed.append(True))

    response = bulk_update_answers(
        aid,
        BulkUpdateAnswersRequest(
            answers=[AnswerUpdate(question_id=qid, answer_text="Answered.")]
        ),
        db=db,
        client=_fake_client("alice@example.com"),
    )

    assert committed, "bulk_update_answers must commit once the batch succeeds"
    assert response.updated_count == 1


def test_bulk_update_route_maps_foreign_question_to_404_and_writes_nothing(db):
    tid = _seed_template(db)
    other_tid = _seed_second_template(db)
    aid = _seed_assessment(db, tid, "Bulk 404 DPIA")
    qid = _seed_question(db, tid, "q1", "necessity", 1)
    foreign_qid = _seed_question(db, other_tid, "q2", "necessity", 1)
    db.flush()

    with pytest.raises(HTTPException) as exc_info:
        bulk_update_answers(
            aid,
            BulkUpdateAnswersRequest(
                answers=[
                    AnswerUpdate(question_id=qid, answer_text="Good."),
                    AnswerUpdate(question_id=foreign_qid, answer_text="Bad."),
                ]
            ),
            db=db,
            client=_fake_client("alice@example.com"),
        )
    assert exc_info.value.status_code == 404

    count = db.execute(
        sqlalchemy.text(
            "SELECT COUNT(*) FROM assessment_answer WHERE assessment_id = :aid"
        ),
        {"aid": aid},
    ).scalar()
    assert count == 0, "a 404'd batch must not leave any partial rows behind"


def test_bulk_update_route_maps_unknown_assessment_to_404(db):
    with pytest.raises(HTTPException) as exc_info:
        bulk_update_answers(
            "no-such-assessment",
            BulkUpdateAnswersRequest(
                answers=[AnswerUpdate(question_id="no-such-question", answer_text="x")]
            ),
            db=db,
            client=_fake_client("alice@example.com"),
        )
    assert exc_info.value.status_code == 404


def test_bulk_update_route_created_by_comes_from_the_caller_not_the_body(
    db, monkeypatch
):
    tid = _seed_template(db)
    aid = _seed_assessment(db, tid, "Bulk Created By DPIA")
    qid = _seed_question(db, tid, "q1", "necessity", 1)
    db.flush()

    monkeypatch.setattr(db, "commit", lambda: None)

    bulk_update_answers(
        aid,
        BulkUpdateAnswersRequest(
            answers=[AnswerUpdate(question_id=qid, answer_text="Answered.")]
        ),
        db=db,
        client=_fake_client("carol@example.com", id="client_should_not_be_used"),
    )

    created_by = db.execute(
        sqlalchemy.text(
            "SELECT av.created_by FROM assessment_answer a "
            "JOIN answer_version av ON av.id = a.current_version_id "
            "WHERE a.assessment_id = :aid AND a.question_id = :qid"
        ),
        {"aid": aid, "qid": qid},
    ).scalar()
    assert created_by == "carol@example.com", (
        "created_by must be the AUTHENTICATED CLIENT's user, not the "
        "request body (AnswerUpdate has no created_by field at all) and "
        "not a hardcoded/forged value"
    )


# --- PUT .../{assessment_id} (task 4: update the assessment itself) ---
#
# _update_assessment is the non-committing core (build a partial UPDATE off
# exclude_unset, re-read, assemble AssessmentResponse) — tested directly
# here the same way _update_answer/_bulk_update_answers are above.
# update_assessment is the actual HTTP route: a thin shell that maps
# LookupError to 404 and commits on success.


def test_update_assessment_changes_only_the_named_field(db):
    tid = _seed_template(db)
    aid = _seed_assessment(
        db, tid, "Original Name", status="in_progress", risk_level="low"
    )
    db.flush()

    response = _update_assessment(db, aid, {"risk_level": "high"})

    assert response.risk_level == "high"
    assert response.name == "Original Name", "name must be untouched"
    assert response.status == "in_progress", "status must be untouched"


def test_update_assessment_rejects_a_field_outside_the_allow_list(db):
    # The SET clause interpolates column NAMES into the SQL string (values
    # are bound; identifiers cannot be), so the allow-list is the guard
    # against an arbitrary column reaching it. It used to be an `assert`,
    # which `python -O` strips. This pins that it raises for real.
    tid = _seed_template(db)
    aid = _seed_assessment(db, tid, "Allow-list DPIA")
    db.flush()

    with pytest.raises(ValueError, match="_UPDATABLE_ASSESSMENT_FIELDS"):
        _update_assessment(db, aid, {"completeness": 1.0})


def test_update_assessment_name_only_leaves_status_and_risk_level_alone(db):
    tid = _seed_template(db)
    aid = _seed_assessment(
        db, tid, "Old Name", status="outdated", risk_level="medium"
    )
    db.flush()

    response = _update_assessment(db, aid, {"name": "New Name"})

    assert response.name == "New Name"
    assert response.status == "outdated"
    assert response.risk_level == "medium"


def test_update_assessment_can_change_all_three_fields_at_once(db):
    tid = _seed_template(db)
    aid = _seed_assessment(
        db, tid, "Old Name", status="in_progress", risk_level="low"
    )
    db.flush()

    response = _update_assessment(
        db, aid, {"name": "New Name", "status": "completed", "risk_level": "high"}
    )

    assert response.name == "New Name"
    assert response.status == "completed"
    assert response.risk_level == "high"


def test_update_assessment_persists_not_just_returns(db):
    # The envelope alone can't distinguish "wrote it" from "just echoed the
    # request back" — read the row back from the database independently.
    tid = _seed_template(db)
    aid = _seed_assessment(db, tid, "Persist Check", status="in_progress")
    db.flush()

    _update_assessment(db, aid, {"name": "Persisted Name"})

    persisted_name = db.execute(
        sqlalchemy.text("SELECT name FROM privacy_assessment WHERE id = :id"),
        {"id": aid},
    ).scalar()
    assert persisted_name == "Persisted Name"


def test_update_assessment_empty_updates_is_a_no_op_but_returns_current_state(db):
    tid = _seed_template(db)
    aid = _seed_assessment(db, tid, "Untouched", status="in_progress", risk_level="low")
    db.flush()

    response = _update_assessment(db, aid, {})

    assert response.name == "Untouched"
    assert response.status == "in_progress"
    assert response.risk_level == "low"


def test_update_assessment_explicit_null_risk_level_clears_it(db):
    # The one field of the three that IS DB-nullable — an explicit
    # `risk_level: null` is accepted and applied literally, clearing a
    # previously-set risk level. See
    # UpdatePrivacyAssessmentRequest._reject_explicit_null_for_not_null_columns
    # for why name/status do NOT get this same treatment.
    tid = _seed_template(db)
    aid = _seed_assessment(db, tid, "Risk Assessed", risk_level="high")
    db.flush()

    response = _update_assessment(db, aid, {"risk_level": None})

    assert response.risk_level is None
    persisted = db.execute(
        sqlalchemy.text("SELECT risk_level FROM privacy_assessment WHERE id = :id"),
        {"id": aid},
    ).scalar()
    assert persisted is None, "must be persisted, not just returned in the envelope"


def test_empty_body_update_locks_the_assessment_row(db):
    # Fix round 3 (whole-range review, finding 8): the empty-body branch used
    # to take two UNLOCKED reads — the existence check, then the detail read
    # for the response — so under READ COMMITTED a concurrent answer write
    # could commit between them and the returned AssessmentResponse could mix
    # pre-write and post-write state. Every other path on this surface holds
    # the row lock across its reads.
    #
    # Asserted on the emitted SQL via before_cursor_execute, the same idiom
    # test_answers.py uses to prove write_answer's lock — a direct check on
    # what was sent to Postgres, not on our own source.
    tid = _seed_template(db)
    aid = _seed_assessment(db, tid, "Empty Body Lock DPIA")
    db.flush()

    statements: list[str] = []
    engine = db.get_bind()

    def _capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    sqlalchemy.event.listen(engine, "before_cursor_execute", _capture)
    try:
        response = _update_assessment(db, aid, {})
    finally:
        sqlalchemy.event.remove(engine, "before_cursor_execute", _capture)

    assert response.id == aid
    assert any("FOR UPDATE" in statement for statement in statements), (
        "the empty-body branch must hold the parent row lock across its two "
        f"reads, like every other write path here — emitted: {statements!r}"
    )


def test_update_answer_404s_rather_than_500s_if_the_question_read_back_is_gone(
    db, monkeypatch
):
    # Fix round 3 (whole-range review, finding 4): _question_by_id "cannot
    # legitimately come back None" — write_answer validated the question
    # against the template before this point. The guard exists for if that
    # ever stops being true: the un-guarded failure was a TypeError on
    # q["evidence"], surfacing as a 500 AFTER a successful append. The answer
    # is saved and the caller sees a crash — the worst response shape on this
    # surface. Forcing the None here is the only way to exercise it.
    tid = _seed_template(db)
    aid = _seed_assessment(db, tid, "Vanished Question DPIA")
    qid = _seed_question(db, tid, "q1", "necessity", 1)
    db.flush()
    monkeypatch.setattr(assessments_module, "_question_by_id", lambda *a, **k: None)

    with pytest.raises(LookupError):
        _update_answer(db, aid, qid, "An answer.", "alice@example.com")


def test_update_answer_route_maps_a_vanished_question_read_back_to_404(
    db, monkeypatch
):
    tid = _seed_template(db)
    aid = _seed_assessment(db, tid, "Vanished Question Route DPIA")
    qid = _seed_question(db, tid, "q1", "necessity", 1)
    db.flush()
    monkeypatch.setattr(assessments_module, "_question_by_id", lambda *a, **k: None)
    monkeypatch.setattr(db, "commit", lambda: None)

    with pytest.raises(HTTPException) as exc_info:
        update_answer(
            aid,
            qid,
            UpdateAnswerRequest(answer_text="An answer."),
            db=db,
            client=_fake_client("alice@example.com"),
        )
    assert exc_info.value.status_code == 404, (
        "a failed read-back must land on the 404 this route already maps, "
        "never escape as a 500"
    )


def test_update_assessment_unknown_id_raises_lookuperror(db):
    with pytest.raises(LookupError):
        _update_assessment(db, "no-such-assessment", {"name": "Doesn't matter"})


def test_update_assessment_unknown_id_raises_lookuperror_even_with_empty_updates(db):
    # An empty body against an unknown id must still 404 — "nothing to
    # write" must not be mistaken for "nothing to check".
    with pytest.raises(LookupError):
        _update_assessment(db, "no-such-assessment", {})


def test_update_assessment_route_maps_unknown_id_to_404(db):
    with pytest.raises(HTTPException) as exc_info:
        update_assessment(
            "no-such-assessment",
            UpdatePrivacyAssessmentRequest(name="Doesn't matter"),
            db=db,
            client=_fake_client("alice@example.com"),
        )
    assert exc_info.value.status_code == 404


def test_update_assessment_route_commits_on_success(db, monkeypatch):
    tid = _seed_template(db)
    aid = _seed_assessment(db, tid, "Commit Update DPIA")
    db.flush()

    committed = []
    monkeypatch.setattr(db, "commit", lambda: committed.append(True))

    response = update_assessment(
        aid,
        UpdatePrivacyAssessmentRequest(name="Committed Name"),
        db=db,
        client=_fake_client("alice@example.com"),
    )

    assert committed, "update_assessment must commit once _update_assessment succeeds"
    assert response.name == "Committed Name"


def test_update_assessment_route_returns_an_assessment_response(db, monkeypatch):
    tid = _seed_template(db)
    aid = _seed_assessment(db, tid, "Response Shape DPIA")
    db.flush()
    monkeypatch.setattr(db, "commit", lambda: None)

    response = update_assessment(
        aid,
        UpdatePrivacyAssessmentRequest(status="completed"),
        db=db,
        client=_fake_client("alice@example.com"),
    )

    assert response.id == aid
    assert response.status == "completed"
    assert response.template_name == "Kenya DPA 2019 DPIA", (
        "the PUT response must be a real AssessmentResponse, joined with "
        "the template, not a bare echo of the request"
    )


# --- UpdatePrivacyAssessmentRequest's own explicit-null contract ---
#
# These exercise the Pydantic model directly, independent of the route/core
# above: the request-shape decision task 4's brief asks for, pinned at the
# layer that actually enforces it.


def test_update_request_omitted_fields_are_absent_from_the_dump():
    request = UpdatePrivacyAssessmentRequest(name="Only Name")
    dumped = request.model_dump(exclude_unset=True)
    assert dumped == {"name": "Only Name"}, (
        "status/risk_level were never sent — they must not appear at all, "
        "not even as null"
    )


def test_update_request_explicit_null_risk_level_is_present_in_the_dump():
    request = UpdatePrivacyAssessmentRequest(risk_level=None)
    dumped = request.model_dump(exclude_unset=True)
    assert dumped == {"risk_level": None}, (
        "an explicit null must be a DIFFERENT dump than omitting the field "
        "entirely — the key is present with value None, not absent"
    )


def test_update_request_rejects_explicit_null_name():
    with pytest.raises(ValidationError):
        UpdatePrivacyAssessmentRequest(name=None)


def test_update_request_rejects_explicit_null_status():
    with pytest.raises(ValidationError):
        UpdatePrivacyAssessmentRequest(status=None)


def test_update_request_omitting_name_entirely_does_not_raise():
    # Contrast with the two tests above: omission is fine for every field,
    # including name/status — only an EXPLICIT null on those two is rejected.
    request = UpdatePrivacyAssessmentRequest(risk_level="high")
    assert request.model_dump(exclude_unset=True) == {"risk_level": "high"}


# --- Fix round 3 (whole-range review, MAJOR finding): out-of-enum
# status/risk_level must be a 422 at the schema boundary, not a 500 out of
# Postgres ---
#
# Both columns are native Postgres enums and _update_assessment writes them
# through raw sqlalchemy.text(), which bypasses SQLAlchemy's own EnumColumn
# validation — so a bare `Optional[str]` on the request model let
# `{"status": "archived"}` reach the database and raise DataError
# (InvalidTextRepresentation), uncaught, as an opaque 500. The fields are
# now typed against schemas.AssessmentStatus / schemas.RiskLevel, whose
# members are pinned member-for-member against the shipped TypeScript enums
# by test_api_schemas.py.


@pytest.mark.parametrize("bad_status", ["archived", "Completed", "in progress", ""])
def test_update_request_rejects_an_out_of_enum_status(bad_status):
    with pytest.raises(ValidationError):
        UpdatePrivacyAssessmentRequest(status=bad_status)


@pytest.mark.parametrize("bad_risk", ["catastrophic", "High", "severe", ""])
def test_update_request_rejects_an_out_of_enum_risk_level(bad_risk):
    with pytest.raises(ValidationError):
        UpdatePrivacyAssessmentRequest(risk_level=bad_risk)


@pytest.mark.parametrize("value", [s.value for s in AssessmentStatus])
def test_update_request_accepts_every_shipped_status(value):
    # The tightening must not reject anything the UI can legitimately send.
    request = UpdatePrivacyAssessmentRequest(status=value)
    assert request.model_dump(exclude_unset=True) == {"status": value}, (
        "use_enum_values must keep the dump a plain string, so "
        "_update_assessment still binds exactly what it bound before"
    )


@pytest.mark.parametrize("value", [r.value for r in RiskLevel])
def test_update_request_accepts_every_shipped_risk_level(value):
    request = UpdatePrivacyAssessmentRequest(risk_level=value)
    assert request.model_dump(exclude_unset=True) == {"risk_level": value}


def test_an_out_of_enum_status_is_rejected_before_any_sql_runs(db):
    # THE point of the fix, proved on the emitted SQL rather than on our own
    # source: the value is refused at model construction — which in FastAPI
    # happens before the handler function is entered at all — so not one
    # statement reaches Postgres. Before the fix, this same value produced a
    # real UPDATE and a psycopg2 InvalidTextRepresentation behind it.
    #
    # Same `before_cursor_execute` idiom as test_answers.py's
    # _captured_statements: it fires for every statement sent to this
    # session's engine, so an empty list is a direct observation, not an
    # inference.
    tid = _seed_template(db)
    aid = _seed_assessment(db, tid, "No SQL For A Bad Enum DPIA")
    db.flush()

    statements: list[str] = []
    engine = db.get_bind()

    def _capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    sqlalchemy.event.listen(engine, "before_cursor_execute", _capture)
    try:
        with pytest.raises(ValidationError):
            # The route cannot even be called: FastAPI builds this model
            # from the body first, and that is where the rejection lands.
            update_assessment(
                aid,
                UpdatePrivacyAssessmentRequest(status="archived"),
                db=db,
                client=_fake_client("alice@example.com"),
            )
    finally:
        sqlalchemy.event.remove(engine, "before_cursor_execute", _capture)

    assert statements == [], (
        "an out-of-enum status must be refused at the schema boundary — no "
        f"statement may reach Postgres, but these did: {statements!r}"
    )


# --- DELETE .../{assessment_id} (task 4: delete the assessment) ---
#
# _delete_assessment is the non-committing core; delete_assessment is the
# HTTP route. See _delete_assessment's own docstring in assessments.py for
# the hard-delete decision and the FK-cascade reasoning these tests pin.


def test_delete_assessment_removes_the_row(db):
    tid = _seed_template(db)
    aid = _seed_assessment(db, tid, "To Be Deleted")
    db.flush()

    _delete_assessment(db, aid, "alice@example.com")

    remaining = db.execute(
        sqlalchemy.text("SELECT COUNT(*) FROM privacy_assessment WHERE id = :id"),
        {"id": aid},
    ).scalar()
    assert remaining == 0


def test_delete_assessment_cascades_to_assessment_answer_and_answer_version(db):
    # The FK chain (verified against the live schema, quoted in the task
    # report): assessment_answer.assessment_id -> privacy_assessment.id ON
    # DELETE CASCADE, and answer_version.answer_id -> assessment_answer.id
    # ON DELETE CASCADE. Deleting the assessment must therefore leave ZERO
    # rows behind in either table for it — not an error, not an orphan.
    tid = _seed_template(db)
    aid = _seed_assessment(db, tid, "Cascade DPIA")
    qid = _seed_question(db, tid, "q1", "necessity", 1)
    write_answer(db, aid, qid, "An answer with a version history.", "alice@example.com")
    write_answer(db, aid, qid, "A second version of the same answer.", "alice@example.com")
    db.flush()

    answer_count_before = db.execute(
        sqlalchemy.text(
            "SELECT COUNT(*) FROM assessment_answer WHERE assessment_id = :aid"
        ),
        {"aid": aid},
    ).scalar()
    assert answer_count_before == 1, "sanity: the handle exists before delete"
    version_count_before = db.execute(
        sqlalchemy.text(
            "SELECT COUNT(*) FROM answer_version av "
            "JOIN assessment_answer a ON a.id = av.answer_id "
            "WHERE a.assessment_id = :aid"
        ),
        {"aid": aid},
    ).scalar()
    assert version_count_before == 2, "sanity: both versions exist before delete"

    _delete_assessment(db, aid, "alice@example.com")

    answer_count_after = db.execute(
        sqlalchemy.text(
            "SELECT COUNT(*) FROM assessment_answer WHERE assessment_id = :aid"
        ),
        {"aid": aid},
    ).scalar()
    assert answer_count_after == 0, (
        "assessment_answer rows must be gone via ON DELETE CASCADE, not "
        "orphaned pointing at a deleted assessment_id"
    )
    # Whether the answer_version rows themselves are actually gone (not
    # just unreachable via a join through the now-deleted assessment_answer
    # row) is asserted by
    # test_delete_assessment_cascade_leaves_no_dangling_answer_version_rows
    # below, which captures their ids BEFORE the delete.


def test_delete_assessment_cascade_leaves_no_dangling_answer_version_rows(db):
    # Belt-and-braces version of the cascade test above: capture the actual
    # answer_version ids BEFORE deleting the assessment, then assert none of
    # them still exist afterward — proves the versions themselves are gone,
    # not merely unreachable via a join.
    tid = _seed_template(db)
    aid = _seed_assessment(db, tid, "Dangling Version Check DPIA")
    qid = _seed_question(db, tid, "q1", "necessity", 1)
    write_answer(db, aid, qid, "First version.", "alice@example.com")
    write_answer(db, aid, qid, "Second version.", "alice@example.com")
    db.flush()

    version_ids = [
        row[0]
        for row in db.execute(
            sqlalchemy.text(
                "SELECT av.id FROM answer_version av "
                "JOIN assessment_answer a ON a.id = av.answer_id "
                "WHERE a.assessment_id = :aid"
            ),
            {"aid": aid},
        )
    ]
    assert len(version_ids) == 2, "sanity: both versions captured before delete"

    _delete_assessment(db, aid, "alice@example.com")

    remaining = db.execute(
        sqlalchemy.text(
            "SELECT COUNT(*) FROM answer_version WHERE id = ANY(:ids)"
        ),
        {"ids": version_ids},
    ).scalar()
    assert remaining == 0, (
        "every answer_version row that belonged to the deleted assessment's "
        "answers must be gone, not left dangling"
    )


# --- Fix round 3 (whole-range review, MAJOR finding 3): the delete must
# leave a trace naming who did it ---
#
# DELETE is the only call on this surface that destroys evidence, and it was
# the only write route that captured no authenticated principal and emitted
# no log. Under Kenya's DPA 2019 §31 the append-only answer history IS the
# artifact proving the assessment happened; the cascade removes all of it.
# No column on an Ethyca-authored table can hold a deleted_by, so the
# application log is the only record that can exist — which makes it worth
# testing like a feature, not like a debug aid.


@contextlib.contextmanager
def _captured_logs(level="WARNING"):
    # loguru does not route through the stdlib logging module, so pytest's
    # caplog fixture sees nothing from `from loguru import logger`. Adding a
    # list-appending sink is the supported way to observe it.
    from loguru import logger as loguru_logger

    messages: list[str] = []
    sink_id = loguru_logger.add(
        lambda message: messages.append(str(message)), level=level
    )
    try:
        yield messages
    finally:
        loguru_logger.remove(sink_id)


def test_delete_assessment_logs_a_warning_naming_the_actor(db):
    tid = _seed_template(db)
    aid = _seed_assessment(db, tid, "Logged Delete DPIA")
    db.flush()

    with _captured_logs() as messages:
        _delete_assessment(db, aid, "alice@example.com")

    logged = "\n".join(messages)
    assert "alice@example.com" in logged, (
        "the actor who destroyed the assessment must be named in the log — "
        f"got {logged!r}"
    )
    assert aid in logged, "the assessment id must be in the log"
    assert "Logged Delete DPIA" in logged, (
        "the assessment NAME must be in the log too: the id alone is "
        "unresolvable once the row it identifies no longer exists"
    )


def test_delete_assessment_logs_how_many_answer_versions_the_cascade_destroyed(db):
    # The count must be taken BEFORE the delete — afterwards the rows are
    # gone and there is nothing left to count. Two versions of one answer,
    # plus one of another, is three.
    tid = _seed_template(db)
    aid = _seed_assessment(db, tid, "Counted Cascade DPIA")
    q1 = _seed_question(db, tid, "q1", "necessity", 1)
    q2 = _seed_question(db, tid, "q2", "necessity", 1)
    write_answer(db, aid, q1, "First.", "alice@example.com")
    write_answer(db, aid, q1, "Second.", "alice@example.com")
    write_answer(db, aid, q2, "Only.", "alice@example.com")
    db.flush()

    with _captured_logs() as messages:
        _delete_assessment(db, aid, "alice@example.com")

    logged = "\n".join(messages)
    assert "3 answer_version" in logged, (
        "the log must say how much history the cascade destroyed, counted "
        f"before the delete — got {logged!r}"
    )


def test_delete_assessment_logs_nothing_for_an_unknown_id(db):
    # A 404 destroyed nothing; a warning claiming otherwise would be noise
    # in exactly the log a regulator enquiry would be read from.
    with _captured_logs() as messages:
        with pytest.raises(LookupError):
            _delete_assessment(db, "no-such-assessment", "alice@example.com")

    assert not [m for m in messages if "DELETED" in m], messages


def test_delete_assessment_route_names_the_authenticated_client_never_the_body(
    db, monkeypatch
):
    # Same never-null derivation every answer write uses
    # (_created_by_from_client): a machine-to-machine client with no linked
    # FidesUser is still named, as "client:<id>", rather than logged as None.
    tid = _seed_template(db)
    aid = _seed_assessment(db, tid, "Machine Client Delete DPIA")
    db.flush()
    monkeypatch.setattr(db, "commit", lambda: None)

    with _captured_logs() as messages:
        delete_assessment(
            aid, db=db, client=_fake_client(None, id="api_client_abc123")
        )

    logged = "\n".join(messages)
    assert "client:api_client_abc123" in logged, logged


def test_update_assessment_route_logs_the_actor_and_the_fields_it_changed(
    db, monkeypatch
):
    # privacy_assessment has no "who last edited" column, so this is the
    # only place the actor behind a status flip to `completed` — the claim
    # that the §31 assessment was done — is recorded at all.
    tid = _seed_template(db)
    aid = _seed_assessment(db, tid, "Logged Update DPIA")
    db.flush()
    monkeypatch.setattr(db, "commit", lambda: None)

    with _captured_logs(level="INFO") as messages:
        update_assessment(
            aid,
            UpdatePrivacyAssessmentRequest(status="completed"),
            db=db,
            client=_fake_client("alice@example.com"),
        )

    logged = "\n".join(messages)
    assert "alice@example.com" in logged and aid in logged, logged
    assert "status" in logged, "the log must say which field(s) changed"


def test_delete_assessment_unknown_id_raises_lookuperror(db):
    with pytest.raises(LookupError):
        _delete_assessment(db, "no-such-assessment", "alice@example.com")


def test_delete_assessment_route_maps_unknown_id_to_404(db):
    with pytest.raises(HTTPException) as exc_info:
        delete_assessment(
            "no-such-assessment", db=db, client=_fake_client("alice@example.com")
        )
    assert exc_info.value.status_code == 404


def test_delete_assessment_route_commits_on_success(db, monkeypatch):
    tid = _seed_template(db)
    aid = _seed_assessment(db, tid, "Commit Delete DPIA")
    db.flush()

    committed = []
    monkeypatch.setattr(db, "commit", lambda: committed.append(True))

    response = delete_assessment(
        aid, db=db, client=_fake_client("alice@example.com")
    )

    assert committed, "delete_assessment must commit once _delete_assessment succeeds"
    assert response.id == aid
    assert response.deleted is True


def test_delete_assessment_twice_404s_the_second_time(db, monkeypatch):
    # THE double-delete case the brief calls out by name: the first call
    # must succeed, the second call — same id, now gone — must 404, not
    # silently succeed again or 500.
    tid = _seed_template(db)
    aid = _seed_assessment(db, tid, "Double Delete DPIA")
    db.flush()
    monkeypatch.setattr(db, "commit", lambda: None)

    first = delete_assessment(aid, db=db, client=_fake_client("alice@example.com"))
    assert first.deleted is True

    with pytest.raises(HTTPException) as exc_info:
        delete_assessment(aid, db=db, client=_fake_client("alice@example.com"))
    assert exc_info.value.status_code == 404
