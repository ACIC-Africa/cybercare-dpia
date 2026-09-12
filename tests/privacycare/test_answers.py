# The versioned answer-write core. Inserts are rolled back (see the `db`
# fixture in test_api_assessments.py, imported below along with the seeding
# helpers rather than duplicated here).
import pytest
import sqlalchemy
from sqlalchemy import event
from sqlalchemy.orm import Session

from fides.api.privacycare.api.answers import (
    QuestionNotInTemplateError,
    recompute_completeness,
    write_answer,
)
from tests.privacycare.test_api_assessments import (
    _seed_assessment,
    _seed_question,
    _seed_template,
)

DB_URL = "postgresql://postgres:fides@127.0.0.1:5442/fides"


@pytest.fixture
def db():
    engine = sqlalchemy.create_engine(DB_URL)
    with Session(engine) as session:
        yield session
        session.rollback()


def _answer_row(db, assessment_id: str, question_id: str):
    return db.execute(
        sqlalchemy.text(
            "SELECT id, current_version_id FROM assessment_answer "
            "WHERE assessment_id = :aid AND question_id = :qid"
        ),
        {"aid": assessment_id, "qid": question_id},
    ).first()


def _version_row(db, version_id: str):
    return db.execute(
        sqlalchemy.text(
            "SELECT id, answer_id, version_number, answer_text, answer_status, "
            " answer_source, change_type, created_by "
            "FROM answer_version WHERE id = :id"
        ),
        {"id": version_id},
    ).first()


def test_first_write_creates_the_handle_and_version_1(db):
    tid = _seed_template(db)
    aid = _seed_assessment(db, tid, "First Write DPIA")
    qid = _seed_question(db, tid, "q1", "necessity", 1)
    db.flush()

    result = write_answer(
        db, aid, qid, "We collect only what's needed.", "alice@example.com"
    )
    db.flush()

    assert result.version_number == 1
    row = _answer_row(db, aid, qid)
    assert row is not None, "first write must create the assessment_answer handle"
    assert row.id == result.answer_id
    assert row.current_version_id == result.version_id, (
        "current_version_id must point at the version just written"
    )
    version = _version_row(db, result.version_id)
    assert version.answer_text == "We collect only what's needed."
    assert version.version_number == 1


def test_second_write_creates_version_2_and_leaves_version_1_readable(db):
    tid = _seed_template(db)
    aid = _seed_assessment(db, tid, "Second Write DPIA")
    qid = _seed_question(db, tid, "q1", "necessity", 1)
    db.flush()

    first = write_answer(db, aid, qid, "Draft answer.", "alice@example.com")
    db.flush()
    second = write_answer(db, aid, qid, "Revised answer.", "bob@example.com")
    db.flush()

    assert second.version_number == 2
    assert second.version_id != first.version_id

    row = _answer_row(db, aid, qid)
    assert row.current_version_id == second.version_id, (
        "current_version_id must be repointed at the newest version"
    )

    # Rule 1: append, never overwrite — the first version's row must still
    # exist, untouched, readable by id.
    first_version = _version_row(db, first.version_id)
    assert first_version is not None, "version 1 must remain readable"
    assert first_version.answer_text == "Draft answer."
    assert first_version.version_number == 1

    second_version = _version_row(db, second.version_id)
    assert second_version.answer_text == "Revised answer."


def test_exactly_one_handle_exists_after_two_writes(db):
    tid = _seed_template(db)
    aid = _seed_assessment(db, tid, "One Handle DPIA")
    qid = _seed_question(db, tid, "q1", "necessity", 1)
    db.flush()

    write_answer(db, aid, qid, "First.", "alice@example.com")
    db.flush()
    write_answer(db, aid, qid, "Second.", "alice@example.com")
    db.flush()

    count = db.execute(
        sqlalchemy.text(
            "SELECT COUNT(*) FROM assessment_answer "
            "WHERE assessment_id = :aid AND question_id = :qid"
        ),
        {"aid": aid, "qid": qid},
    ).scalar()
    assert count == 1, "rule 2: never two handles for one (assessment, question)"


def test_change_type_is_human_edited_and_source_is_user_input(db):
    tid = _seed_template(db)
    aid = _seed_assessment(db, tid, "Human Edited DPIA")
    qid = _seed_question(db, tid, "q1", "necessity", 1)
    db.flush()

    result = write_answer(db, aid, qid, "An answer.", "alice@example.com")
    db.flush()

    assert result.change_type == "human_edited"
    assert result.answer_source == "user_input"
    version = _version_row(db, result.version_id)
    assert version.change_type == "human_edited"
    assert version.answer_source == "user_input"


def test_created_by_is_recorded(db):
    tid = _seed_template(db)
    aid = _seed_assessment(db, tid, "Created By DPIA")
    qid = _seed_question(db, tid, "q1", "necessity", 1)
    db.flush()

    result = write_answer(db, aid, qid, "An answer.", "carol@example.com")
    db.flush()

    assert result.created_by == "carol@example.com"
    version = _version_row(db, result.version_id)
    assert version.created_by == "carol@example.com"


def test_write_answer_defaults_status_to_complete(db):
    # THE DECISION: answer_status defaults to "complete" for a human-typed
    # save. This is a DEFAULT, not a UI-derived finding — see the evidence
    # trail in answers.py's write_answer docstring: QuestionCard.tsx's
    # UpdateAnswerRequest carries only answer_text (no status field, no
    # status control rendered), and AssessmentDetail.tsx's only
    # completion-adjacent computation (`isComplete`) is a text-presence
    # check on the whole assessment, not a per-answer answer_status the
    # save request could carry. Absent any UI signal either way, the brief's
    # explicit fallback applies: a person typed an answer, so it's complete.
    tid = _seed_template(db)
    aid = _seed_assessment(db, tid, "Default Status DPIA")
    qid = _seed_question(db, tid, "q1", "necessity", 1)
    db.flush()

    result = write_answer(db, aid, qid, "An answer.", "alice@example.com")
    db.flush()

    assert result.answer_status == "complete"
    version = _version_row(db, result.version_id)
    assert version.answer_status == "complete"


def test_recompute_completeness_is_complete_only_and_persists(db):
    tid = _seed_template(db)
    aid = _seed_assessment(db, tid, "Completeness DPIA")
    q1 = _seed_question(db, tid, "q1", "necessity", 1)
    _seed_question(db, tid, "q2", "necessity", 1)
    _seed_question(db, tid, "q3", "necessity", 1)
    db.flush()

    write_answer(db, aid, q1, "Answered.", "alice@example.com")
    db.flush()

    completeness = recompute_completeness(db, aid)
    assert completeness == pytest.approx(1 / 3), (
        "1 of 3 questions has a complete answer"
    )

    persisted = db.execute(
        sqlalchemy.text("SELECT completeness FROM privacy_assessment WHERE id = :id"),
        {"id": aid},
    ).scalar()
    assert persisted == pytest.approx(1 / 3), (
        "recompute_completeness must write the value, not just return it"
    )


def test_recompute_completeness_excludes_non_complete_statuses(db):
    # Same complete-only definition as answered_count in assessments.py's
    # _question_group_response (settled by reading AnswerStatusTags.tsx in
    # plan 03b) — a "needs_input" answer must not count. write_answer always
    # produces "complete" (the decision above), so this seeds a
    # non-"complete" current version directly to exercise the exclusion.
    tid = _seed_template(db)
    aid = _seed_assessment(db, tid, "Needs Input Completeness DPIA")
    q1 = _seed_question(db, tid, "q1", "necessity", 1)
    _seed_question(db, tid, "q2", "necessity", 1)
    db.flush()

    answer_id = "aa_needsinput"
    version_id = "av_needsinput"
    db.execute(
        sqlalchemy.text(
            "INSERT INTO assessment_answer (id, assessment_id, question_id) "
            "VALUES (:id, :aid, :qid)"
        ),
        {"id": answer_id, "aid": aid, "qid": q1},
    )
    db.execute(
        sqlalchemy.text(
            "INSERT INTO answer_version "
            "(id, answer_id, answer_status, answer_source, change_type) "
            "VALUES (:id, :answer_id, 'needs_input', 'system', 'ai_generated')"
        ),
        {"id": version_id, "answer_id": answer_id},
    )
    db.execute(
        sqlalchemy.text(
            "UPDATE assessment_answer SET current_version_id = :vid WHERE id = :id"
        ),
        {"vid": version_id, "id": answer_id},
    )
    db.flush()

    completeness = recompute_completeness(db, aid)
    assert completeness == 0.0, "a needs_input answer must not count as complete"


def _seed_second_template(db) -> str:
    # _seed_template/_seed_template_named (test_api_assessments.py) both
    # hardcode assessment_type="dpia", version="1.0" — calling either of
    # them twice in the same test collides with
    # uq_assessment_template_type_version_revision (verified against the
    # live schema: UNIQUE on assessment_type, version, fides_revision).
    # This test needs two genuinely DISTINCT templates, so it seeds a
    # second one with a different assessment_type directly, rather than
    # duplicating either helper's full body for a case they don't support.
    import uuid

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


def test_foreign_question_raises_rather_than_writing(db):
    tid = _seed_template(db)
    other_tid = _seed_second_template(db)
    aid = _seed_assessment(db, tid, "Own Template DPIA")
    foreign_qid = _seed_question(db, other_tid, "q1", "necessity", 1)
    db.flush()

    with pytest.raises(QuestionNotInTemplateError):
        write_answer(
            db, aid, foreign_qid, "Should not be written.", "alice@example.com"
        )
    db.flush()

    assert _answer_row(db, aid, foreign_qid) is None, (
        "rule 4: a foreign question must be a client error, not a silent no-op"
    )


def _captured_statements(db):
    # Fix round 1: proves the lock is actually taken, not just documented.
    # `before_cursor_execute` fires for every statement sent to this
    # session's engine, so collecting them and asserting "FOR UPDATE"
    # appears is a direct check on the emitted SQL, not on our own source.
    statements: list[str] = []
    engine = db.get_bind()

    def _capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", _capture)
    return statements, engine, _capture


def test_write_answer_locks_the_parent_assessment_row(db):
    tid = _seed_template(db)
    aid = _seed_assessment(db, tid, "Lock DPIA")
    qid = _seed_question(db, tid, "q1", "necessity", 1)
    db.flush()

    statements, engine, capture = _captured_statements(db)
    try:
        write_answer(db, aid, qid, "Locked answer.", "alice@example.com")
        db.flush()
    finally:
        event.remove(engine, "before_cursor_execute", capture)

    assert any("FOR UPDATE" in s for s in statements), (
        "write_answer must take a FOR UPDATE lock on the parent "
        "privacy_assessment row before its handle/version reads — "
        "closing the three read-then-write races fix round 1 found"
    )


def test_recompute_completeness_locks_the_parent_assessment_row(db):
    tid = _seed_template(db)
    aid = _seed_assessment(db, tid, "Recompute Lock DPIA")
    _seed_question(db, tid, "q1", "necessity", 1)
    db.flush()

    statements, engine, capture = _captured_statements(db)
    try:
        recompute_completeness(db, aid)
        db.flush()
    finally:
        event.remove(engine, "before_cursor_execute", capture)

    assert any("FOR UPDATE" in s for s in statements), (
        "recompute_completeness must take its own lock — it is also called "
        "on its own, not only right after write_answer in the same "
        "transaction"
    )


def test_write_answer_against_nonexistent_assessment_raises_without_inserting(db):
    with pytest.raises(LookupError):
        write_answer(
            db, "no-such-assessment", "no-such-question", "text", "alice@example.com"
        )
    db.flush()

    count = db.execute(
        sqlalchemy.text(
            "SELECT COUNT(*) FROM assessment_answer WHERE assessment_id = :aid"
        ),
        {"aid": "no-such-assessment"},
    ).scalar()
    assert count == 0, (
        "a write against a nonexistent assessment must raise before any "
        "insert, not leave an orphaned assessment_answer row behind"
    )
