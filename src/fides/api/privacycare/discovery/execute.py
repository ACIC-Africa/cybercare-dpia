"""Execute a discovery monitor once: walk, reconcile, and record that it ran.

SPEC D-EX-9. Task 1's `walk_catalogue` only ever reads; Task 2's `reconcile`
is the first thing in the pipeline that writes a `stagedresource` row. This
module is what wires the two into one run, and — per D-EX-9 — makes sure
every run leaves a `MonitorExecution` row behind, including a run that dies
halfway: an operator asking "did discovery run last night?" deserves an
answer either way, not silence. The resources a failed run already wrote
before it died stay written (they are true observations of what the walk
actually saw before it failed) and the next run's `reconcile` call is what
folds them back into an honest picture.

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
already staged for this monitor — so calling it with an empty list that
came from a FAILED walk would read as "the entire catalogue vanished" and
mark every one of this monitor's staged resources `removal`: a false,
sweeping finding for something that never happened. The `try/except` below
exists precisely to stop that: `walk_catalogue`'s own exception is left to
propagate — caught only long enough to close the execution record and
re-raise — and `reconcile` is never reached on that path. An empty list
that comes back from a SUCCESSFUL walk is a different, legitimate case (the
catalogue really is empty this run) and is meant to reach `reconcile`,
which correctly marks the previous run's resources `removal`.
"""
import uuid
from datetime import datetime, timezone
from typing import List

import sqlalchemy
from sqlalchemy.orm import Session

from fides.api.models.detection_discovery.core import MonitorConfig
from fides.api.privacycare.discovery.reconcile import ReconcileSummary, reconcile
from fides.api.privacycare.discovery.walk import walk_catalogue
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
_STATUS_IN_PROGRESS = "In progress"
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
            "status": _STATUS_IN_PROGRESS,
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


def run_monitor(db: Session, *, monitor_config_id: str) -> ReconcileSummary:
    """Walk `monitor_config_id`'s connection, reconcile what was found, and
    record the run start-to-finish in `MonitorExecution`.

    `monitor_config_id` is the monitor's `key`, matching every other lookup
    on this surface — api/monitors.py's own module docstring: "{monitor_
    config_id} IN THE UI'S URLS IS THE MONITOR's key, not its row id".

    Raises `LookupError` for an unknown monitor, BEFORE any `MonitorExecution`
    row is written — there is nothing to record a run against when there was
    never a monitor to run.
    """
    monitor = db.query(MonitorConfig).filter(MonitorConfig.key == monitor_config_id).first()
    if monitor is None:
        raise LookupError(f"No monitor with key {monitor_config_id}")

    execution_id = _start_execution(db, monitor_key=monitor.key)

    try:
        found = walk_catalogue(
            monitor.connection_config,
            monitor_key=monitor.key,
            databases=list(monitor.databases or []),
            excluded_databases=list(monitor.excluded_databases or []),
        )
    except Exception as exc:  # noqa: BLE001 — D-EX-9: record the failure, then re-raise
        # See this module's docstring, "THE MOST DANGEROUS LINE" — reconcile()
        # is NEVER called on this path. The walk's own exception propagates
        # unchanged after the execution record is closed out.
        _finish_execution(
            db, execution_id, status=_STATUS_ERRORED, messages=[str(exc)]
        )
        raise

    # Reached ONLY when the walk above succeeded. An empty `found` here is a
    # legitimate "nothing there any more" from a real scan, not a symptom of
    # failure, and correctly reaches reconcile().
    summary = reconcile(
        db, monitor_config_id=monitor.key, monitor_key=monitor.key, found=found
    )
    _finish_execution(db, execution_id, status=_STATUS_COMPLETE, messages=[])
    return summary


@celery_app.task(base=DatabaseTask, bind=True, name=EXECUTE_TASK_NAME)
def execute_monitor_task(self: DatabaseTask, monitor_config_id: str) -> None:
    """Celery entry point. All behaviour is in `run_monitor` — this is a
    thin wrapper worth no test beyond confirming it calls the core."""
    with self.get_new_session() as db:
        run_monitor(db, monitor_config_id=monitor_config_id)
