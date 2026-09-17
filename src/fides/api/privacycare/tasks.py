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
from fides.api.privacycare.screening.gate import is_screened_out
from fides.api.privacycare.settings import resolve_assessment_model
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

# One row per task_id: a re-run of the same task (see
# test_re_running_generation_for_the_same_task_does_not_duplicate_the_skip_row)
# must update the existing row rather than collide with the unique index or
# leave a stale count behind — hence upsert, not plain INSERT.
_UPSERT_GENERATION_SKIP_SQL = sqlalchemy.text(
    "INSERT INTO privacycare_generation_skip (id, task_id, skipped_count) "
    "VALUES (:id, :task_id, :skipped_count) "
    "ON CONFLICT (task_id) DO UPDATE SET skipped_count = EXCLUDED.skipped_count"
)

_SKIPPED_FOR_TASK_SQL = sqlalchemy.text(
    "SELECT skipped_count FROM privacycare_generation_skip WHERE task_id = :task_id"
)

# Task 1 (plan 20, spec 2026-09-17-privacycare-20-screening-rekey) re-keyed
# privacycare_screening_decision, and so is_screened_out(), from a
# processing activity (privacydeclaration) to a business process: this
# customer runs 86 real business processes and only 2 declarations exist
# system-wide, and the privacy SME confirmed screening belongs on the
# actual operational unit. This is the join _is_activity_screened_out below
# uses to resolve a GenerationTarget's declaration_id to the process(es)
# that process it.
_BUSINESS_PROCESSES_FOR_DECLARATION_SQL = sqlalchemy.text(
    "SELECT business_process_id FROM privacycare_process_declaration "
    "WHERE privacy_declaration_id = :declaration_id"
)


def _is_activity_screened_out(db: Session, declaration_id: str) -> bool:
    """Whether the activity behind this generation target should be
    skipped, resolved through privacycare_process_declaration.

    Before Task 2 of this plan, this call site passed a declaration_id
    straight into is_screened_out(), which Task 1 had already re-keyed to
    expect a business_process_id. That mismatch never crashed —
    is_screened_out() has no existence check, it just finds zero decision
    rows for an id that matches nothing and returns False — so it silently
    evaluated to "not screened out" for every activity, no error, no log,
    no signal. Every activity would have generated, including ones an
    officer had explicitly marked not applicable, and nobody would have
    gone looking, because a silent wrong answer never complains. This
    function is what closes that: it resolves the declaration to the real
    business process(es) linked to it (privacycare_process_declaration,
    written by link_process_declarations in api/processes.py) and asks
    is_screened_out() about THOSE, which is what it has always actually
    needed.

    The gate stays opt-in at this new boundary, exactly as it already is at
    is_screened_out()'s own: a declaration with NO row in
    privacycare_process_declaration at all is not screened — it generates
    exactly as if the gate did not exist. Today only one such link exists
    in the whole system (Task 4 of this plan is what populates the rest),
    so almost every activity is in this state, and getting this backwards
    would silently halt nearly every existing workflow.

    A declaration CAN, in principle, be linked to more than one business
    process — the link table's unique constraint is on the (process,
    declaration) pair, not on the declaration alone, so two different
    processes are free to name the same underlying activity. Skipping
    generation is the stronger claim ("no DPIA needed for this activity at
    all"), so it is only made when EVERY linked process currently screens
    out; one linked process that has not been screened out (or has never
    been screened) is enough to let the activity generate. That keeps the
    same error direction plan 18's opt-in default already chose:
    generating an assessment nobody strictly needed is recoverable, a
    missing one is not.
    """
    process_ids = [
        row[0]
        for row in db.execute(
            _BUSINESS_PROCESSES_FOR_DECLARATION_SQL,
            {"declaration_id": declaration_id},
        ).all()
    ]
    if not process_ids:
        return False
    return all(is_screened_out(db, process_id) for process_id in process_ids)


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
        rows = (
            db.execute(_ACTIVE_TEMPLATE_SQL, {"assessment_type": assessment_type})
            .mappings()
            .all()
        )
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
    rows = (
        db.execute(_TEMPLATE_SOURCES_SQL, {"template_id": template_id}).mappings().all()
    )
    source_paths = [path for row in rows for path in (row["fides_sources"] or [])]
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

    Before any of that, a target whose ACTIVITY is screened OUT is skipped
    entirely: no assessment row, no LLM call, no completeness computation.
    The verdict itself lives on a business process (screening/gate.py's
    is_screened_out, re-keyed by Task 1 of plan 20), reached from this
    target's declaration_id through privacycare_process_declaration — see
    _is_activity_screened_out for that resolution and why it stays opt-in
    at the new boundary. In Fides today "prior consultation" is asked six
    times inside GDPR question text and enforced nowhere; a screening
    verdict this task recorded but never acted on would be the same defect
    wearing our own badge. A skip is not a failure — it is counted and
    reported on its own terms (see the `skipped` handling below and
    _finish's message), and the gate is opt-in: an activity with no linked
    business process, or a business process that has never been screened,
    is never blocked.
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

    # Resolved ONCE for the whole run, not per assessment: every assessment
    # in one run must be drafted by the same model, or the task's own
    # metadata ("which model produced this?") describes only some of what it
    # produced. `task["llm_model"]` is what the officer chose for THIS run
    # when they started it and takes precedence; with no per-run choice this
    # falls back to the configured override and then to llm.DEFAULT_MODEL.
    # See privacycare/settings.py for the precedence and why the settings
    # screen needed a reader at all.
    model = resolve_assessment_model(db, task["llm_model"])

    completed = 0
    skipped = 0
    failures: list[str] = []
    for target in targets:
        # The gate check comes before build_context, not inside its
        # try/except: a screen-out is not a failure mode of context
        # building, it is a decision that was already made about this
        # declaration, and it must pre-empt every downstream step — no
        # _create_assessment row, no LLM call, no recompute_completeness —
        # for every assessment_type this target would otherwise have
        # produced. Counted against `skipped`, not `failures`: reporting a
        # screen-out as an error would tell whoever ran this task something
        # false about their own estate.
        if _is_activity_screened_out(db, target.declaration_id):
            skipped += len(templates)
            # Fix round 1 (coordinator review, Important finding): persisted
            # and committed HERE, immediately — not only at the run's final
            # _finish call. _is_activity_screened_out() above sits outside
            # every per-target try/except (see this loop's own comment), so an
            # exception on the very NEXT target, or anywhere else between
            # iterations, escapes run_generation entirely and _finish's
            # final call — the one call site that used to write this row —
            # is never reached. Without a durable write here, activities
            # already, genuinely screened out earlier in this same run
            # would read back as zero the instant a later target's lookup
            # hit a transient error. This is the same crash window
            # _current_counts already closes for total_count/completed_count
            # by being committed as they accrue; skipped had not been
            # extended to match until now. Safe against a later per-target
            # db.rollback() in this same loop: SQLAlchemy's rollback() only
            # undoes the transaction opened since the last commit, so this
            # commit is unaffected by any later one.
            _record_skip(db, task_id, skipped)
            db.commit()
            logger.info(
                "PrivacyCare generation skipping {} ({}): screened out, no "
                "DPIA required",
                target.system_fides_key,
                target.declaration_id,
            )
            continue

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
                    model=model,
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

    # Fix round 1 (coordinator review, Important finding): completed==0 used
    # to branch on `if failures:` alone and, whenever true, report "All
    # {total} assessments failed" — folding any screened-out targets
    # silently into that count. Three targets, one screened out and two
    # failing, used to read "All 3 assessments failed" even though only two
    # did. The message below is built the same way regardless of how many
    # completed (uniform with the completed>0 case just below it), so
    # `completed`, `skipped`, and `len(failures)` always show up as three
    # separate, addable numbers rather than two of them colliding into one.
    #
    # `status` is decided on its own, independently of the message: `error`
    # only when at least one target genuinely failed AND nothing completed
    # — a real problem produced zero output. Zero completions caused solely
    # by screening (skipped == total, failures empty) is the gate doing
    # exactly its job, not a run with nothing to show for itself, and is
    # reported as `complete`.
    status = "error" if (completed == 0 and failures) else "complete"

    message = f"Generated {completed} of {total} assessments."
    if skipped:
        message += f" {skipped} screened out (no DPIA required)."
    if failures:
        message += f" {len(failures)} failed: {failures[0]}"
    _finish(db, task_id, status, total, completed, message, skipped=skipped)


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


def _finish(db, task_id, status, total, completed, message, skipped: int = 0) -> None:
    """Writes the task row's final status AND, in the same transaction, how
    many activities this run screened out — so the two can never disagree
    (plan 19, Task 1). `skipped` defaults to 0 for every call site that has
    no target loop to have skipped anything (a template-resolution error, an
    empty target set): those runs correctly leave no
    privacycare_generation_skip row, the same as a real run that skipped
    nothing.
    """
    _set_status(db, task_id, status, total, completed, message)
    if skipped:
        _record_skip(db, task_id, skipped)
    db.commit()


def _record_skip(db: Session, task_id: str, skipped_count: int) -> None:
    """Persists how many activities this run's screening gate has skipped
    SO FAR. Called only when skipped_count is non-zero (see the call sites
    in run_generation's loop, _finish, and _fail_task) — a run that skipped
    nothing has nothing to say, and an absent row reads as zero
    (skipped_for_task below).

    Fix round 1 (coordinator review, Important finding): called twice, not
    once. run_generation's loop calls this immediately after every skip,
    committed right there, so the count is durable before the NEXT target's
    processing can throw and escape the run entirely (see that call site's
    own comment for the crash this closes). _finish's own call at the end
    of a normal run re-upserts the same final total — a harmless no-op
    write, kept because it is what makes _finish's "record it in the same
    transaction as the task row" guarantee hold for the ordinary
    (non-crash) case.

    Upserts on task_id rather than plain-inserting: a re-run of generation
    for the same task_id must update the one row a unique index allows,
    never collide with it or leave a stale count from an earlier run.
    """
    db.execute(
        _UPSERT_GENERATION_SKIP_SQL,
        {"id": str(uuid.uuid4()), "task_id": task_id, "skipped_count": skipped_count},
    )


def skipped_for_task(db: Session, task_id: str) -> int:
    """How many activities a generation run skipped because the screening
    gate said no DPIA was needed. 0 when no row exists — a run that
    skipped nothing, or a task_id that was never run at all — never None,
    never an error."""
    row = db.execute(_SKIPPED_FOR_TASK_SQL, {"task_id": task_id}).first()
    if row is None:
        return 0
    return row[0]


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

    Fix round 1, Task 1 (coordinator review, Important finding): `skipped`
    gets the identical treatment, via skipped_for_task rather than a second
    private reader — there is nothing task-row-specific left to duplicate,
    unlike _current_counts, which reads privacy_assessment_task itself.
    Passed through to _finish so a rolled-back-and-retried write does not
    disturb the row run_generation's loop already committed; _finish's
    `if skipped:` guard makes this call a no-op when nothing was ever
    skipped, matching skipped_for_task's own "0 for no row" contract rather
    than writing a stray zero row.
    """
    db.rollback()
    total, completed = _current_counts(db, task_id)
    skipped = skipped_for_task(db, task_id)
    _finish(
        db,
        task_id,
        "error",
        total,
        completed,
        f"Generation failed: {exc}",
        skipped=skipped,
    )


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
