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


def _current_version(db, assessment_id: str, question_id: str):
    # Fix round 3 (whole-range review, finding 7): write_answer no longer
    # returns an AnswerWriteResult — the model was deleted as dead in
    # production (no caller could build its response from it; see the
    # comment where it used to live in api/answers.py). These helpers read
    # the same facts back out of the database instead, which is the stronger
    # assertion for an append-only audit trail: it pins what was PERSISTED,
    # not what the function reported having written.
    row = _answer_row(db, assessment_id, question_id)
    assert row is not None, "no assessment_answer handle for this pair"
    return _version_row(db, row.current_version_id)


def _versions(db, assessment_id: str, question_id: str):
    # Every version ever written for this (assessment, question), oldest
    # first — the chain itself.
    return db.execute(
        sqlalchemy.text(
            "SELECT av.id, av.answer_id, av.version_number, av.answer_text, "
            " av.answer_status, av.answer_source, av.change_type, av.created_by "
            "FROM answer_version av "
            "JOIN assessment_answer a ON a.id = av.answer_id "
            "WHERE a.assessment_id = :aid AND a.question_id = :qid "
            "ORDER BY av.version_number"
        ),
        {"aid": assessment_id, "qid": question_id},
    ).all()


def test_first_write_creates_the_handle_and_version_1(db):
    tid = _seed_template(db)
    aid = _seed_assessment(db, tid, "First Write DPIA")
    qid = _seed_question(db, tid, "q1", "necessity", 1)
    db.flush()

    write_answer(db, aid, qid, "We collect only what's needed.", "alice@example.com")
    db.flush()

    row = _answer_row(db, aid, qid)
    assert row is not None, "first write must create the assessment_answer handle"

    versions = _versions(db, aid, qid)
    assert len(versions) == 1, "exactly one version after one write"
    assert versions[0].version_number == 1
    assert versions[0].answer_id == row.id
    assert row.current_version_id == versions[0].id, (
        "current_version_id must point at the version just written"
    )
    assert versions[0].answer_text == "We collect only what's needed."


def test_second_write_creates_version_2_and_leaves_version_1_readable(db):
    tid = _seed_template(db)
    aid = _seed_assessment(db, tid, "Second Write DPIA")
    qid = _seed_question(db, tid, "q1", "necessity", 1)
    db.flush()

    write_answer(db, aid, qid, "Draft answer.", "alice@example.com")
    db.flush()
    write_answer(db, aid, qid, "Revised answer.", "bob@example.com")
    db.flush()

    # Rule 1: append, never overwrite — BOTH versions must still exist,
    # version 1 untouched.
    versions = _versions(db, aid, qid)
    assert len(versions) == 2, "version 1 must remain readable after version 2"
    first_version, second_version = versions
    assert first_version.version_number == 1
    assert first_version.answer_text == "Draft answer."
    assert first_version.created_by == "alice@example.com"
    assert second_version.version_number == 2
    assert second_version.answer_text == "Revised answer."
    assert second_version.created_by == "bob@example.com"
    assert second_version.id != first_version.id

    row = _answer_row(db, aid, qid)
    assert row.current_version_id == second_version.id, (
        "current_version_id must be repointed at the newest version"
    )


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

    write_answer(db, aid, qid, "An answer.", "alice@example.com")
    db.flush()

    version = _current_version(db, aid, qid)
    assert version.change_type == "human_edited"
    assert version.answer_source == "user_input"


def test_created_by_is_recorded(db):
    tid = _seed_template(db)
    aid = _seed_assessment(db, tid, "Created By DPIA")
    qid = _seed_question(db, tid, "q1", "necessity", 1)
    db.flush()

    write_answer(db, aid, qid, "An answer.", "carol@example.com")
    db.flush()

    version = _current_version(db, aid, qid)
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

    write_answer(db, aid, qid, "An answer.", "alice@example.com")
    db.flush()

    version = _current_version(db, aid, qid)
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


def _seed_complete_answer_directly(db, assessment_id: str, question_id: str) -> None:
    # Insert an assessment_answer + a "complete" current answer_version
    # WITHOUT going through write_answer. Needed because write_answer's own
    # _require_question_in_template refuses to write an off-template answer
    # — the state under test here is one this module's routes cannot create
    # and only the (out-of-range) generation path or a template_id move can
    # reach, so it has to be seeded at the SQL level, exactly as
    # test_recompute_completeness_excludes_non_complete_statuses already
    # seeds a status write_answer never produces.
    import uuid

    answer_id = f"aa_{uuid.uuid4().hex[:8]}"
    version_id = f"av_{uuid.uuid4().hex[:8]}"
    db.execute(
        sqlalchemy.text(
            "INSERT INTO assessment_answer (id, assessment_id, question_id) "
            "VALUES (:id, :aid, :qid)"
        ),
        {"id": answer_id, "aid": assessment_id, "qid": question_id},
    )
    db.execute(
        sqlalchemy.text(
            "INSERT INTO answer_version "
            "(id, answer_id, version_number, answer_text, answer_status, "
            " answer_source, change_type, created_by) "
            "VALUES (:id, :answer_id, 1, 'Off-template answer.', 'complete', "
            " 'user_input', 'human_edited', 'alice@example.com')"
        ),
        {"id": version_id, "answer_id": answer_id},
    )
    db.execute(
        sqlalchemy.text(
            "UPDATE assessment_answer SET current_version_id = :vid WHERE id = :id"
        ),
        {"vid": version_id, "id": answer_id},
    )


def test_recompute_completeness_ignores_answers_whose_question_is_not_on_the_template(
    db,
):
    # Fix round 3 (whole-range review, MAJOR finding): completeness's
    # NUMERATOR must be scoped to the assessment's current template exactly
    # the way its denominator (_TOTAL_QUESTIONS_SQL) and answered_count
    # (_QUESTION_SQL in api/assessments.py) already are. An answer row whose
    # question belongs to a DIFFERENT template used to count in the
    # numerator while contributing nothing to the denominator and staying
    # invisible to answered_count — so the same document reported two
    # different numbers, and (with enough off-template answers) a
    # completeness above 1.0.
    tid = _seed_template(db)
    other_tid = _seed_second_template(db)
    aid = _seed_assessment(db, tid, "Off-Template Numerator DPIA")
    q1 = _seed_question(db, tid, "q1", "necessity", 1)
    _seed_question(db, tid, "q2", "necessity", 1)
    foreign_qid = _seed_question(db, other_tid, "q1", "necessity", 1)
    db.flush()

    write_answer(db, aid, q1, "On-template answer.", "alice@example.com")
    _seed_complete_answer_directly(db, aid, foreign_qid)
    db.flush()

    completeness = recompute_completeness(db, aid)
    assert completeness == pytest.approx(1 / 2), (
        "only the on-template answer may count: 1 complete of 2 template "
        "questions. The off-template answer must not inflate the numerator"
    )

    persisted = db.execute(
        sqlalchemy.text("SELECT completeness FROM privacy_assessment WHERE id = :id"),
        {"id": aid},
    ).scalar()
    assert persisted == pytest.approx(1 / 2)


def test_recompute_completeness_never_exceeds_one_with_only_off_template_answers(db):
    # The unbounded case the review names: every question on the template
    # unanswered, several complete answers sitting against questions from
    # another template. Before the fix this returned 2/1 == 2.0 and the
    # detail screen rendered "Fields: 0/1" beside a completeness of 200%.
    tid = _seed_template(db)
    other_tid = _seed_second_template(db)
    aid = _seed_assessment(db, tid, "Above One Hundred Percent DPIA")
    _seed_question(db, tid, "q1", "necessity", 1)
    foreign_a = _seed_question(db, other_tid, "fq1", "necessity", 1)
    foreign_b = _seed_question(db, other_tid, "fq2", "necessity", 2)
    db.flush()

    _seed_complete_answer_directly(db, aid, foreign_a)
    _seed_complete_answer_directly(db, aid, foreign_b)
    db.flush()

    completeness = recompute_completeness(db, aid)
    assert completeness == 0.0, (
        "no question on the assessment's own template is answered, so "
        "completeness is 0 — not 2.0"
    )


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

    # Position, not just presence. A lock taken AFTER the reads closes no
    # race at all, and an assertion that only looks for the string anywhere
    # in the statement list would pass that regression unchanged.
    assert statements, "write_answer emitted no SQL at all"
    assert "FOR UPDATE" in statements[0], (
        "write_answer must take a FOR UPDATE lock on the parent "
        "privacy_assessment row as its FIRST statement, before its "
        "handle/version reads — closing the three read-then-write races "
        f"fix round 1 found. First statement was: {statements[0]!r}"
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

    assert statements, "recompute_completeness emitted no SQL at all"
    assert "FOR UPDATE" in statements[0], (
        "recompute_completeness must take its own lock FIRST — it is also "
        "called on its own, not only right after write_answer in the same "
        f"transaction. First statement was: {statements[0]!r}"
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
