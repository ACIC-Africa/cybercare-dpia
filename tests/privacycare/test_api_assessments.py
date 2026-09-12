# Endpoint behaviour against real rows. Inserts are rolled back.
import json
import types
import uuid

import pytest
import sqlalchemy
from fastapi import HTTPException
from sqlalchemy.orm import Session

from fides.api.privacycare.api import assessments as assessments_module
from fides.api.privacycare.api.answers import QuestionNotInTemplateError
from fides.api.privacycare.api.assessments import (
    _assessment_detail,
    _assessment_to_response,
    _bulk_update_answers,
    _created_by_from_client,
    _evidence_for,
    _grouped_assessments,
    _list_assessments,
    _list_templates,
    _questions_for,
    _summary,
    _update_answer,
    bulk_update_answers,
    update_answer,
)
from fides.api.privacycare.api.schemas import (
    AnswerUpdate,
    BulkUpdateAnswersRequest,
    UpdateAnswerRequest,
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
