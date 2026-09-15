"""Execute a discovery monitor once: walk, reconcile, and record that it ran.

SPEC D-EX-9. Task 1's `walk_catalogue` only ever reads; Task 2's `reconcile`
is the first thing in the pipeline that writes a `stagedresource` row. This
module is what wires the two into one run, and — per D-EX-9 — makes sure
every run leaves a `MonitorExecution` row behind, including a run that dies
halfway: an operator asking "did discovery run last night?" deserves an
answer either way, not silence. F2 fix -- corrected from a claim that no
longer described this module: `reconcile`'s writes to `db` are NOT committed
until `_finish_execution` runs (the row-level `db.execute()` calls inside it
are just buffered in the open transaction), so a run that dies partway
through `reconcile` discards everything it had written so far -- this run is
atomic, all-or-nothing, not partial-write-survives. The NEXT successful
run's `reconcile` call still folds a true, complete picture back in; nothing
about D-EX-9's "leaves evidence either way" guarantee depends on partial
writes surviving.

`run_monitor` is the synchronous core and is what every test below drives
directly; `execute_monitor_task` is a thin Celery wrapper around it and
needs no test beyond confirming it calls the core (see
tests/privacycare/test_discovery_execute.py).

`walk_catalogue` and `reconcile` are imported BY NAME into this module's
OWN namespace (controller ruling) — `from ...walk import walk_catalogue`,
never called through `walk.walk_catalogue(...)`. The failure-path test
monkeypatches `fides.api.privacycare.discovery.execute.walk_catalogue`
directly; patching a different module's attribute would leave this call
site untouched and the test would assert against a run that never failed.

THE MOST DANGEROUS LINE IN THIS MODULE is the call to `reconcile()` below.
`reconcile` decides what has GONE by diffing `found` against what is
already staged for this monitor — so calling it with an empty (or
effectively empty) list that does not reflect the real catalogue would read
as "the entire catalogue vanished" and mark every one of this monitor's
staged resources `removal`: a false, sweeping finding for something that
never happened. Two guards protect against that, both raising before
`reconcile` is ever reached:

  1. A FAILED walk: `walk_catalogue`'s own exception is left to propagate —
     caught only long enough to close the execution record and re-raise.
  2. F2 fix — reconcile()'s OWN failure (a unique violation from a
     concurrent run, a deadlock, a DBAPI error) previously escaped with the
     execution row still `In progress` and `completed` NULL, contradicting
     this module's own D-EX-9 promise. Wrapped the same way: closed out as
     `Errored` naming the exception, then re-raised.
  3. F4 fix — a SUCCESSFUL walk whose `databases` scope is non-empty but
     matched NO schema at all (a renamed schema, a stale scope, a role that
     lost visibility) returns just the lone Database node. That is not the
     same as "the catalogue really is empty" (an unscoped walk against a
     genuinely bare connection, which correctly reaches `reconcile` and
     marks the previous run's resources `removal`) — a scope naming only
     non-existent schemas is far likelier a misconfiguration than a
     vanished estate, so this case is closed out as `Errored`, naming the
     scope and what it failed to match, and never reaches `reconcile`.
"""
import uuid
from datetime import datetime, timezone
from typing import List

import sqlalchemy
from sqlalchemy.orm import Session

from fides.api.models.detection_discovery.core import (
    MonitorConfig,
    StagedResourceType,
)
from fides.api.privacycare.discovery.reconcile import ReconcileSummary, reconcile
from fides.api.privacycare.discovery.walk import FoundResource, walk_catalogue
from fides.api.tasks import DatabaseTask, celery_app

# Pinned rather than derived from the module path, same reasoning as
# tasks.py's GENERATION_TASK_NAME: the module path is ours to refactor, the
# registered name is what a queued message carries, and a rename would
# orphan every in-flight message.
EXECUTE_TASK_NAME = "privacycare.discovery.execute"

# MonitorExecutionStatus.ts's three values (clients/admin-ui/src/types/api/
# models/MonitorExecutionStatus.ts) — the shipped UI's status column reads
# this column against exactly this vocabulary, so these are spelled out
# verbatim rather than invented. There is no IN_PROGRESS -> ERRORED
# transition test surface here for the "still running" state: this module
# only ever writes IN_PROGRESS at start and one of the two terminal values
# when it closes the row out, in the same call that observes success or
# failure.
STATUS_IN_PROGRESS = "In progress"
_STATUS_COMPLETE = "Completed"
_STATUS_ERRORED = "Errored"

# classification_instances and messages are both NOT NULL arrays on
# monitorexecution (measured live) with no ORM-side default reached by a raw
# INSERT — spelled out as empty array literals rather than bound params, the
# same pattern reconcile.py's own _INSERT_SQL uses for classifications /
# user_assigned_data_categories / children: Postgres infers the literal's
# type from the target column, so no explicit CAST is needed on INSERT.
_INSERT_EXECUTION_SQL = sqlalchemy.text(
    """
    INSERT INTO monitorexecution (
        id, monitor_config_key, status, started,
        classification_instances, messages
    ) VALUES (
        :id, :monitor_config_key, :status, :started, '{}', '{}'
    )
    """
)

# `messages` is bound as a real Python list here (unlike the INSERT above),
# so it is CAST explicitly — the same belt reconcile.py's own queries use
# whenever an array-typed bind parameter's Postgres type could otherwise be
# ambiguous to the driver.
_FINISH_EXECUTION_SQL = sqlalchemy.text(
    """
    UPDATE monitorexecution
    SET status = :status, completed = :completed,
        messages = CAST(:messages AS text[])
    WHERE id = :id
    """
)

def _start_execution(db: Session, *, monitor_key: str) -> str:
    """INSERT the MonitorExecution row for this run and commit it
    immediately — before the walk even opens a connection to the target —
    so the record that a run STARTED survives even a crash the `except`
    block below never gets to run (a killed process, an OOM). D-EX-9 asks
    for evidence a run started and stopped; this half is what supplies
    "started" unconditionally.
    """
    execution_id = f"mxn_{uuid.uuid4().hex[:12]}"
    db.execute(
        _INSERT_EXECUTION_SQL,
        {
            "id": execution_id,
            "monitor_config_key": monitor_key,
            "status": STATUS_IN_PROGRESS,
            "started": datetime.now(timezone.utc),
        },
    )
    db.commit()
    return execution_id


def _finish_execution(
    db: Session, execution_id: str, *, status: str, messages: List[str]
) -> None:
    db.execute(
        _FINISH_EXECUTION_SQL,
        {
            "id": execution_id,
            "status": status,
            "completed": datetime.now(timezone.utc),
            "messages": messages,
        },
    )
    db.commit()


def _scope_matched_nothing(
    databases: List[str], found: List[FoundResource]
) -> bool:
    """F4: a non-empty `databases` scope that matched no schema at all. Every
    real walk unconditionally emits the Database node (see walk.py's
    `_walk_database`), so `found` is never truly empty on a successful walk —
    the signal that nothing in the scope matched is the absence of any
    Schema-level resource, not an empty list."""
    if not databases:
        return False
    return not any(
        resource.resource_type == StagedResourceType.SCHEMA.value
        for resource in found
    )


class EmptyScopeError(Exception):
    """F4: `run_monitor` raises this — after closing the execution record as
    `Errored` — when a monitor's `databases` scope is non-empty but matched
    no schema in the target catalogue. Its own exception type (rather than a
    bare Exception, the way `walk_catalogue` failures propagate) so a caller
    can distinguish "the scope is almost certainly misconfigured" from an
    actual connection or catalogue-read failure."""


def run_monitor(db: Session, *, monitor_config_id: str) -> ReconcileSummary:
    """Walk `monitor_config_id`'s connection, reconcile what was found, and
    record the run start-to-finish in `MonitorExecution`.

    `monitor_config_id` is the monitor's `key`, matching every other lookup
    on this surface — api/monitors.py's own module docstring: "{monitor_
    config_id} IN THE UI'S URLS IS THE MONITOR's key, not its row id".

    Raises `LookupError` for an unknown monitor, BEFORE any `MonitorExecution`
    row is written — there is nothing to record a run against when there was
    never a monitor to run. Every other raise below happens AFTER the
    execution row is closed out — see this module's docstring, "THE MOST
    DANGEROUS LINE".
    """
    monitor = db.query(MonitorConfig).filter(MonitorConfig.key == monitor_config_id).first()
    if monitor is None:
        raise LookupError(f"No monitor with key {monitor_config_id}")

    databases = list(monitor.databases or [])
    execution_id = _start_execution(db, monitor_key=monitor.key)

    try:
        found = walk_catalogue(
            monitor.connection_config,
            monitor_key=monitor.key,
            databases=databases,
            excluded_databases=list(monitor.excluded_databases or []),
        )
    except Exception as exc:  # noqa: BLE001 — D-EX-9: record the failure, then re-raise
        # reconcile() is NEVER called on this path.
        _finish_execution(
            db, execution_id, status=_STATUS_ERRORED, messages=[str(exc)]
        )
        raise

    # F4: a scope that matched nothing is closed out as Errored and never
    # reaches reconcile() — see this module's docstring, guard 3. Checked
    # here, after a SUCCESSFUL walk, so it is never confused with guard 1
    # (a failed walk) or reached when `databases` is empty (an intentionally
    # unscoped, whole-catalogue walk, where an empty `found` past the
    # Database node is a legitimate "nothing there").
    if _scope_matched_nothing(databases, found):
        message = (
            "Monitor's databases scope "
            f"{sorted(databases)!r} matched no schema in the catalogue; "
            "the previous scan's resources were left untouched rather than "
            "reconciling what looks like a misconfigured or stale scope."
        )
        _finish_execution(
            db, execution_id, status=_STATUS_ERRORED, messages=[message]
        )
        raise EmptyScopeError(message)

    # F2: reconcile()'s own failure (a unique violation from a concurrent
    # run, a deadlock, a DBAPI error) must close the execution record the
    # same way a failed walk does — previously it did not, and the row was
    # left `In progress` forever. reconcile() runs inside a SAVEPOINT
    # (`db.begin_nested()`) rather than plain `db.rollback()` on failure:
    # unlike a walk failure (which never touches `db` at all — it opens its
    # own separate connection), a DBAPI-level failure INSIDE reconcile()
    # aborts `db`'s current transaction, and the UPDATE inside
    # `_finish_execution` below would itself fail against that poisoned
    # transaction — a plain `db.rollback()` would recover it, but would also
    # discard `_start_execution`'s own row along with it in any caller whose
    # "commit" does not actually commit (exactly what this module's own test
    # fixtures do for isolation — see test_discovery_execute.py's `db`
    # fixture). `ROLLBACK TO SAVEPOINT` undoes only what reconcile() itself
    # wrote, leaving the execution-started row (and the transaction) intact
    # either way.
    try:
        with db.begin_nested():
            summary = reconcile(
                db, monitor_config_id=monitor.key, monitor_key=monitor.key, found=found
            )
    except Exception as exc:  # noqa: BLE001 — D-EX-9: record the failure, then re-raise
        _finish_execution(
            db, execution_id, status=_STATUS_ERRORED, messages=[str(exc)]
        )
        raise

    _finish_execution(db, execution_id, status=_STATUS_COMPLETE, messages=[])
    return summary


@celery_app.task(base=DatabaseTask, bind=True, name=EXECUTE_TASK_NAME)
def execute_monitor_task(self: DatabaseTask, monitor_config_id: str) -> None:
    """Celery entry point. All behaviour is in `run_monitor` — this is a
    thin wrapper worth no test beyond confirming it calls the core."""
    with self.get_new_session() as db:
        run_monitor(db, monitor_config_id=monitor_config_id)
