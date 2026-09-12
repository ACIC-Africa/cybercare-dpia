"""The generation task: turn one privacy_assessment_task row into DPIAs.

Split deliberately in two. run_generation() is the whole behaviour and takes
a Session, so it is testable directly against the database with no broker.
generate_assessments() is a thin Celery wrapper that opens a session and
calls it. Nothing that matters lives in the wrapper.
"""
import json
import uuid
from datetime import datetime, timezone

import sqlalchemy
from loguru import logger
from sqlalchemy.orm import Session

from fides.api.privacycare.api.answers import recompute_completeness
from fides.api.privacycare.context import (
    UNSUPPORTED_SOURCE_ROOTS,
    GenerationTarget,
    build_context,
    select_targets,
    unresolvable_roots,
)
from fides.api.privacycare.generator import GENERATOR_AUTHOR, answer_questions
from fides.api.tasks import (
    PRIVACY_ASSESSMENTS_QUEUE_NAME,
    DatabaseTask,
    celery_app,
)

# Fides already defines this queue name (fides/api/tasks/__init__.py:
# PRIVACY_ASSESSMENTS_QUEUE_NAME = "fidesplus.privacy_assessments"), so we
# reuse it rather than inventing a parallel one — an Ethyca-authored
# constant, imported, not copied.
GENERATION_QUEUE = PRIVACY_ASSESSMENTS_QUEUE_NAME

# The task's registered name is pinned rather than derived from the module
# path, because the module path is ours to refactor and the name is what a
# queued message carries. A rename would orphan every in-flight message.
GENERATION_TASK_NAME = "privacycare.generate_assessments"

# The only task statuses this module writes. Named rather than scattered as
# literals so there is ONE thing to pin: privacy_assessment_task.status is a
# plain varchar with no database enum, so Postgres accepts any string we hand
# it, and Ethyca's own ORM then raises LookupError reading a row we wrote.
# tests/privacycare/test_vocabularies.py checks this set against BOTH
# authorities — Ethyca's ExecutionLogStatus (what their code can read back)
# and the shipped TaskStatus in types.ts (what the UI can render).
TASK_STATUSES_WRITTEN = frozenset({"pending", "in_processing", "complete", "error"})

# The action_type every task this module creates carries. Ethyca's
# AssessmentTaskType is the authority; RE_EVALUATE is not built.
TASK_ACTION_TYPE = "generate"

_LOAD_TASK_SQL = sqlalchemy.text(
    "SELECT assessment_types, system_fides_keys, use_llm, llm_model, "
    "       high_risk_only, created_by "
    "FROM privacy_assessment_task WHERE id = :task_id"
)

_ACTIVE_TEMPLATE_SQL = sqlalchemy.text(
    "SELECT id, name FROM assessment_template "
    "WHERE assessment_type = :assessment_type AND is_active"
)

# Every fides_sources path across a template's questions, used only for the
# coverage-gap logging below (see _log_coverage_gaps) — not for answer
# generation itself, which reads per-question sources in generator.py.
_TEMPLATE_SOURCES_SQL = sqlalchemy.text(
    "SELECT fides_sources FROM assessment_question WHERE template_id = :template_id"
)

_INSERT_ASSESSMENT_SQL = sqlalchemy.text(
    "INSERT INTO privacy_assessment "
    "(id, template_id, name, status, system_fides_key, system_name, "
    " declaration_id, declaration_name, data_use, data_use_name, "
    " data_categories, created_by, privacy_assessment_task_id, "
    " context_snapshot) "
    "VALUES (:id, :template_id, :name, 'generating', :system_fides_key, "
    " :system_name, :declaration_id, :declaration_name, :data_use, "
    " :data_use_name, :data_categories, :created_by, :task_id, "
    " CAST(:context_snapshot AS JSONB))"
)

_FINISH_ASSESSMENT_SQL = sqlalchemy.text(
    "UPDATE privacy_assessment "
    "SET status = 'in_progress', last_evaluated_at = :now "
    "WHERE id = :assessment_id"
)

# updated_at is bumped EXPLICITLY, for the same reason _update_assessment in
# api/assessments.py bumps it explicitly: Base.updated_at's
# onupdate=func.now() is an ORM-level construct, and a raw
# db.execute(text(...)) bypasses it entirely. The column's only
# database-level default is now() at INSERT and there is no trigger
# (verified against information_schema.columns), so without this clause
# privacy_assessment_task.updated_at never advances — every task in every
# state reports having been last updated the instant it was queued.
#
# That is not cosmetic. AssessmentTaskResponse.updated_at is what
# AssessmentTaskStatusIndicator.tsx renders as a run's finish time, and no
# other column holds it: a forty-minute run, and a run that errored, would
# both tell the DPO they finished the moment Generate was pressed. For an
# artifact whose purpose is answering "when was this done, and by whom",
# the *when* half of the task record was unrecoverable.
#
# clock_timestamp(), not now(): now() is the TRANSACTION's start time, so a
# status write would report when its transaction began rather than when the
# status actually changed — and, since the row is inserted with now() too,
# a write sharing that transaction would not move updated_at off created_at
# at all. clock_timestamp() records the moment of the write, which is the
# fact being stored.
_SET_TASK_STATUS_SQL = sqlalchemy.text(
    "UPDATE privacy_assessment_task "
    "SET status = :status, total_count = :total_count, "
    "    completed_count = :completed_count, message = :message, "
    "    updated_at = clock_timestamp() "
    "WHERE id = :task_id"
)

_TASK_COUNTS_SQL = sqlalchemy.text(
    "SELECT total_count, completed_count FROM privacy_assessment_task "
    "WHERE id = :task_id"
)


def _assessment_name(target: GenerationTarget) -> str:
    """What the DPO sees on the assessment card.

    The template's own name is already displayed beside it on the detail
    screen (the detail response carries template_name), so repeating it here
    would be noise. What distinguishes two assessments in a list is which
    system and which processing activity they cover.
    """
    system = target.system_name or target.system_fides_key
    declaration = target.declaration_name or target.data_use
    return f"{system} — {declaration}"


def _resolve_templates(db: Session, assessment_types: list[str]) -> dict[str, str]:
    """assessment_type -> active template id.

    Exactly one template per type is active (verified live: the GROUP BY …
    HAVING COUNT(*) > 1 over is_active rows returns nothing), and two types
    carry an inactive older version. Raising on an unknown type rather than
    skipping it is deliberate: silently generating four of the five
    assessment types someone asked for is the kind of partial success that
    reads as complete.
    """
    resolved: dict[str, str] = {}
    for assessment_type in assessment_types:
        rows = db.execute(
            _ACTIVE_TEMPLATE_SQL, {"assessment_type": assessment_type}
        ).mappings().all()
        if not rows:
            raise LookupError(
                f"no active assessment template for type {assessment_type!r}"
            )
        if len(rows) > 1:
            raise LookupError(
                f"{len(rows)} active templates for type {assessment_type!r} — "
                "exactly one is expected"
            )
        resolved[assessment_type] = rows[0]["id"]
    return resolved


def _log_coverage_gaps(
    db: Session, assessment_id: str, template_id: str, context: dict
) -> None:
    """Log which fides_sources roots this assessment's template asked for
    but this context could not answer, split into the two shapes that gap
    can take.

    unresolvable_roots() lumps both shapes into one set; set arithmetic
    against the exported UNSUPPORTED_SOURCE_ROOTS constant is what separates
    them again:

      - a root IN UNSUPPORTED_SOURCE_ROOTS is our backlog — a Fides
        subsystem (consent, DSR policy, integrations, Fides' own config)
        that phase 1 does not operate, true regardless of context;
      - any OTHER unresolvable root is a customer data-quality gap — a
        supported root (system/privacy_declaration/data_use/data_category)
        that happened to have nothing to say for this particular system, and
        which the customer could fill in.

    Logging them separately means a PrivacyCare engineer scanning logs for
    "what should we build next" and a DPO/CS engineer scanning for "which
    customer's data is thin" are reading two different, correctly-labelled
    lists rather than one undifferentiated set.
    """
    rows = db.execute(
        _TEMPLATE_SOURCES_SQL, {"template_id": template_id}
    ).mappings().all()
    source_paths = [
        path for row in rows for path in (row["fides_sources"] or [])
    ]
    if not source_paths:
        return

    unresolved = unresolvable_roots(context, source_paths)
    backlog = sorted(unresolved & UNSUPPORTED_SOURCE_ROOTS)
    data_quality_gap = sorted(unresolved - UNSUPPORTED_SOURCE_ROOTS)

    if backlog:
        logger.debug(
            "PrivacyCare assessment {}: phase-1-unsupported roots (backlog, "
            "not a data problem): {}",
            assessment_id,
            backlog,
        )
    if data_quality_gap:
        logger.info(
            "PrivacyCare assessment {}: customer data-quality gap roots "
            "(record has nothing for these, could be filled in): {}",
            assessment_id,
            data_quality_gap,
        )


def run_generation(db: Session, task_id: str) -> None:
    """Generate every assessment this task asked for.

    Commits after each assessment rather than once at the end. The UI polls
    GET tasks every 15 seconds while a task is active and renders
    completed_count/progress; one transaction around a fifty-system run
    would show 0% for minutes and then jump to 100%. The cost is that a
    crash mid-run leaves finished assessments behind and the task stuck in
    `in_processing` — which is the honest state, and recoverable, unlike
    silently rolling back an hour of work.

    One failing target does not fail the run (see the failure handling
    below): a single system's gateway refusal must not discard the other
    forty-nine. The run reports `error` only when it produced nothing.
    """
    task = db.execute(_LOAD_TASK_SQL, {"task_id": task_id}).mappings().first()
    if task is None:
        raise LookupError(f"No privacy_assessment_task with id {task_id}")

    assessment_types = list(task["assessment_types"] or [])
    # `is not None`: NULL means "every system" (select_targets' contract),
    # an empty array means "no systems". Collapsing the second into the
    # first would run a caller's explicit "none" over the whole estate.
    system_fides_keys = (
        list(task["system_fides_keys"])
        if task["system_fides_keys"] is not None
        else None
    )

    try:
        templates = _resolve_templates(db, assessment_types)
    except LookupError as exc:
        _finish(db, task_id, "error", 0, 0, str(exc))
        return

    targets = select_targets(db, system_fides_keys, bool(task["high_risk_only"]))
    total = len(targets) * len(templates)
    if total == 0:
        _finish(
            db,
            task_id,
            "complete",
            0,
            0,
            "No processing activities matched this request — no assessments "
            "were generated.",
        )
        return

    _set_status(db, task_id, "in_processing", total, 0, None)
    db.commit()

    completed = 0
    failures: list[str] = []
    for target in targets:
        # Fix round 1 (coordinator review, MAJOR finding): build_context used
        # to sit OUTSIDE this loop's try/except, so a single target's
        # exception (a malformed declaration, a query timeout) aborted the
        # entire run for every remaining target and never reached _finish —
        # the task row stayed `in_processing` forever, with the UI polling a
        # run that would never report. That defeated the whole point of the
        # per-(target, template) try/except below: "one failing target does
        # not discard the others."
        #
        # Guarded here, once per target rather than once per template
        # (context does not depend on template_id, so recomputing it per
        # template would be wasted queries) — a context-building failure is
        # recorded as a failure for every assessment_type this target would
        # have produced, since none of them can proceed without it, and the
        # run moves on to the next target.
        try:
            context = build_context(db, target)
        except Exception as exc:  # noqa: BLE001 - a bad target must not abort the run
            db.rollback()
            for assessment_type in templates:
                failures.append(
                    f"{target.system_fides_key}/{target.data_use}/{assessment_type}: "
                    f"context build failed: {exc}"
                )
            logger.warning(
                "PrivacyCare generation could not build context for {}: {}",
                target.system_fides_key,
                exc,
            )
            continue

        for assessment_type, template_id in templates.items():
            try:
                assessment_id = _create_assessment(
                    db, task_id, target, template_id, context, task["created_by"]
                )
                _log_coverage_gaps(db, assessment_id, template_id, context)
                answer_questions(
                    db,
                    assessment_id,
                    context,
                    use_llm=bool(task["use_llm"]),
                    model=task["llm_model"],
                )
                recompute_completeness(db, assessment_id)
                db.execute(
                    _FINISH_ASSESSMENT_SQL,
                    {
                        "assessment_id": assessment_id,
                        "now": datetime.now(timezone.utc),
                    },
                )
                completed += 1
                _set_status(db, task_id, "in_processing", total, completed, None)
                db.commit()
            except Exception as exc:  # noqa: BLE001 - see the docstring
                db.rollback()
                failures.append(
                    f"{target.system_fides_key}/{target.data_use}/{assessment_type}: {exc}"
                )
                logger.warning(
                    "PrivacyCare generation failed for {} ({}): {}",
                    target.system_fides_key,
                    assessment_type,
                    exc,
                )

    if completed == 0:
        message = f"All {total} assessments failed. First: {failures[0]}"
        _finish(db, task_id, "error", total, 0, message)
        return

    message = f"Generated {completed} of {total} assessments."
    if failures:
        message += f" {len(failures)} failed: {failures[0]}"
    _finish(db, task_id, "complete", total, completed, message)


def _create_assessment(
    db: Session,
    task_id: str,
    target: GenerationTarget,
    template_id: str,
    context: dict,
    created_by: str | None,
) -> str:
    assessment_id = f"pa_{uuid.uuid4().hex[:12]}"
    db.execute(
        _INSERT_ASSESSMENT_SQL,
        {
            "id": assessment_id,
            "template_id": template_id,
            "name": _assessment_name(target),
            "system_fides_key": target.system_fides_key,
            "system_name": target.system_name,
            "declaration_id": target.declaration_id,
            "declaration_name": target.declaration_name,
            "data_use": target.data_use,
            "data_use_name": target.data_use_name,
            "data_categories": target.data_categories,
            # The person who pressed Generate owns the assessment; the
            # machine owns the individual answers (GENERATOR_AUTHOR on each
            # answer_version). Those are two different questions a regulator
            # asks, and they get two different answers.
            "created_by": created_by,
            "task_id": task_id,
            "context_snapshot": json.dumps(context),
        },
    )
    return assessment_id


def _set_status(db, task_id, status, total, completed, message) -> None:
    db.execute(
        _SET_TASK_STATUS_SQL,
        {
            "task_id": task_id,
            "status": status,
            "total_count": total,
            "completed_count": completed,
            "message": message,
        },
    )


def _finish(db, task_id, status, total, completed, message) -> None:
    _set_status(db, task_id, status, total, completed, message)
    db.commit()


def _current_counts(db: Session, task_id: str) -> tuple[int, int]:
    """The task row's most recently COMMITTED (total_count, completed_count).

    run_generation commits after every assessment (see its own docstring),
    so by the time an exception escapes it to the Celery wrapper below,
    whatever progress the run actually made is already durable in this row.
    Used only by the wrapper's failure path, to avoid clobbering that real
    progress with zeros. Falls back to (0, 0) if the row is somehow gone —
    it should always exist by this point, but a missing row is not worth
    raising a second exception over.
    """
    row = db.execute(_TASK_COUNTS_SQL, {"task_id": task_id}).mappings().first()
    if row is None:
        return 0, 0
    return row["total_count"], row["completed_count"]


def _fail_task(db: Session, task_id: str, exc: Exception) -> None:
    """What the Celery wrapper does when run_generation raises instead of
    returning normally — extracted so it is testable directly against the
    database, the same way run_generation itself is, with no Celery/broker
    involved.

    Fix round 1 (coordinator review, MAJOR finding): this used to call
    _finish(db, task_id, "error", 0, 0, ...) unconditionally — overwriting
    whatever total_count/completed_count the run had already committed with
    zero. A run that produced forty assessments before an unhandled
    exception (a DB connection drop, a bug outside every per-target guard
    run_generation itself has) would then report having produced NONE. The
    counts are re-read from the row (see _current_counts) rather than
    assumed, and preserved in the error message.
    """
    db.rollback()
    total, completed = _current_counts(db, task_id)
    _finish(db, task_id, "error", total, completed, f"Generation failed: {exc}")


@celery_app.task(base=DatabaseTask, bind=True, name=GENERATION_TASK_NAME)
def generate_assessments(self: DatabaseTask, task_id: str) -> None:
    """Celery entry point. All behaviour is in run_generation."""
    with self.get_new_session() as db:
        try:
            run_generation(db, task_id)
        except Exception as exc:  # noqa: BLE001
            # A task that dies without updating its row leaves the UI
            # polling `in_processing` forever with nothing to show for it.
            logger.exception("PrivacyCare generation task {} failed", task_id)
            _fail_task(db, task_id, exc)
            raise
