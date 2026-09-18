"""Turn what a scan found into something a person can act on.

SPEC: 2026-09-18 discovery-findings-API brief, DESIGN.md Screen 4. Task 1
(walk.py) only ever reads a catalogue; Task 2 (reconcile.py) is the first
thing that writes a `stagedresource` row, one per discovered database,
schema, table and field. Neither of those, nor anything under api/
monitors.py, ever exposes a SINGLE finding to a caller, or lets anyone say
what one is. This module is that missing half: list every discovered
table as a work item, and record a person's decision about one.

THE THREE STATES, and where each lives. A discovered table (a `Table`-typed
`stagedresource` row) is in exactly one of three states at any moment:

  - "mapped"       — a privacycare_discovery_reconciliation row says it
                      belongs to a system already in the data map.
  - "ignored"       — a privacycare_discovery_reconciliation row says
                      somebody looked and it holds no personal data, with
                      a written reason.
  - "needs_review"  — NEITHER of the above: no reconciliation row exists
                      for this table's urn at all. This is never a stored
                      value — it is the absence of one, read as a LEFT
                      JOIN miss by `list_findings` below. This is the
                      queue Screen 4 exists to surface, and it is the
                      brief's own point: discovery's output is a question
                      put to a human, and this state is the unanswered
                      ones.

WHY THE RECONCILIATION LIVES IN OUR OWN TABLE, NOT ON `stagedresource`
ITSELF. See models.py's discovery_reconciliation_table docstring and the
migration that created it (f9597c9058e8) for the full argument: Ethyca's
own `stagedresource` already carries columns for exactly this
(`user_assigned_system_id` etc.), but that table is scan output, rebuilt
and diffed by `reconcile()` on every run — D-EX-6 says a re-scan that
finds the same table again touches its row not at all, and resurrection
(reconcile.py's own paragraph on the subject) can bring a urn back under
a fresh row. Writing our decision as our own append-only record, keyed to
the table's stable `urn`, means a re-scan can never overwrite or orphan a
privacy officer's decision, and it stays as available evidence — "who
decided this, when, and why" — the same discipline
`privacycare_screening_decision` already keeps for a screening verdict.

ONE QUERY FOR THE WHOLE LIST, same discipline as api/screening.py's
`_LIST_SCREENING_STATUS_SQL` and its own module docstring: this list
route exists PRECISELY to avoid a per-row round trip, and with 176 tables
already staged against our own database alone, a per-row cost here would
not be an optimisation opportunity, it would be a defect.

All raw SQL, all bound parameters, never a commit — the caller's session
boundary decides, same rule as gate.py, dsr/register.py, risk/register.py
and taxonomy/loader.py.
"""
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, List, Optional

import sqlalchemy
from sqlalchemy.orm import Session

if TYPE_CHECKING:
    # sqlalchemy-stubs (the mypy plugin pinned in pyproject.toml) still
    # models 1.4's raw row type as RowProxy, not the runtime sqlalchemy
    # package's own Row — same type-checking-only import gate.py's own
    # _to_verdict uses for the identical reason.
    from sqlalchemy.engine import RowProxy

STATE_MAPPED = "mapped"
STATE_IGNORED = "ignored"
STATE_NEEDS_REVIEW = "needs_review"

_VALID_FILTER_STATES = frozenset({STATE_MAPPED, STATE_IGNORED, STATE_NEEDS_REVIEW})
_VALID_RECONCILE_STATES = frozenset({STATE_MAPPED, STATE_IGNORED})

# A discovered TABLE (never a Field — 1703 of those is not a work queue; a
# table with its own column count is, per the brief). Joined, in one
# statement, to:
#   - its parent Schema row (for the schema name a person recognises,
#     rather than a urn segment nobody but this codebase reads),
#   - a COUNT of its own Field-typed children (matched by `parent = t.urn`
#     — `stagedresource.children` is never populated by reconcile.py, so
#     this is the only way to get a real count),
#   - the newest privacycare_discovery_reconciliation row for its urn, via
#     LEFT JOIN LATERAL (a table may be reconciled more than once — see
#     that table's own APPEND-ONLY docstring — and this list needs only
#     the current one, with the SAME decided_at/id tie-break
#     get_finding_history below uses, so the two can never disagree about
#     which reconciliation is "current"), and
#   - ctl_systems, ONLY to resolve a mapped finding's system name for
#     display — never to validate anything at read time.
#
# GROUP BY t.id rather than every selected column: t.id is stagedresource's
# real primary key, so every other stagedresource column (urn, name,
# monitor_config_id, diff_status, meta) is functionally dependent on it and
# Postgres does not require repeating them. Columns from the OTHER joined
# tables (schema.name, latest.*, sys.name) are NOT functionally dependent on
# t.id and are listed explicitly.
_LIST_FINDINGS_SQL = sqlalchemy.text(
    """
    SELECT
        t.urn AS urn,
        t.name AS table_name,
        schema_row.name AS schema_name,
        t.monitor_config_id AS monitor_key,
        t.diff_status AS diff_status,
        t.meta ->> 'table_type' AS table_type,
        COUNT(field_row.urn) AS field_count,
        latest.state AS state,
        latest.system_id AS system_id,
        sys.name AS system_name,
        latest.reason AS reason,
        latest.decided_by AS decided_by,
        latest.decided_at AS decided_at
    FROM stagedresource t
    JOIN stagedresource schema_row
        ON schema_row.urn = t.parent AND schema_row.resource_type = 'Schema'
    LEFT JOIN stagedresource field_row
        ON field_row.parent = t.urn AND field_row.resource_type = 'Field'
    LEFT JOIN LATERAL (
        SELECT r.state, r.system_id, r.reason, r.decided_by, r.decided_at
        FROM privacycare_discovery_reconciliation r
        WHERE r.stagedresource_urn = t.urn
        ORDER BY r.decided_at DESC, r.id DESC
        LIMIT 1
    ) latest ON true
    LEFT JOIN ctl_systems sys ON sys.id = latest.system_id
    WHERE t.resource_type = 'Table'
      AND (:monitor_key IS NULL OR t.monitor_config_id = :monitor_key)
    GROUP BY
        t.id, schema_row.name, latest.state, latest.system_id, sys.name,
        latest.reason, latest.decided_by, latest.decided_at
    ORDER BY schema_row.name, t.name, t.urn
    """
)

_TABLE_EXISTS_SQL = sqlalchemy.text(
    "SELECT 1 FROM stagedresource WHERE urn = :urn AND resource_type = 'Table'"
)

_SYSTEM_EXISTS_SQL = sqlalchemy.text("SELECT 1 FROM ctl_systems WHERE id = :id")

_INSERT_RECONCILIATION_SQL = sqlalchemy.text(
    "INSERT INTO privacycare_discovery_reconciliation "
    "(id, stagedresource_urn, state, system_id, reason, decided_by) "
    "VALUES (:id, :stagedresource_urn, :state, :system_id, :reason, :decided_by) "
    "RETURNING decided_at"
)

_RECONCILIATION_COLUMNS = (
    "id, stagedresource_urn, state, system_id, reason, decided_by, decided_at"
)

# Newest first, tie-broken by id — same reasoning gate.py's own
# _SELECT_DECISIONS_SQL gives: two reconciliations written in the same
# transaction can share a server_default now(), and this list route and
# `list_findings`'s own LATERAL join must never be able to disagree about
# which row is "current" for a urn.
_SELECT_RECONCILIATION_HISTORY_SQL = sqlalchemy.text(
    f"SELECT {_RECONCILIATION_COLUMNS} FROM privacycare_discovery_reconciliation "
    "WHERE stagedresource_urn = :urn "
    "ORDER BY decided_at DESC, id DESC"
)

# Resolves a system name for the history route's own display, in the SAME
# per-urn cost list_findings already accepts — a table's history is a
# handful of rows at most, never 176 of them, so a per-row lookup here
# carries none of the N-calls hazard the list route exists to avoid.
_SYSTEM_NAME_SQL = sqlalchemy.text("SELECT name FROM ctl_systems WHERE id = :id")


@dataclass(frozen=True)
class FindingRow:
    urn: str
    table_name: str
    schema_name: str
    monitor_key: str
    diff_status: Optional[str]
    table_type: Optional[str]  # "view" | "materialized_view" | None
    field_count: int
    state: str  # one of STATE_MAPPED / STATE_IGNORED / STATE_NEEDS_REVIEW
    system_id: Optional[str]
    system_name: Optional[str]
    reason: Optional[str]
    decided_by: Optional[str]
    decided_at: Optional[datetime]


@dataclass(frozen=True)
class Reconciliation:
    id: str
    urn: str
    state: str  # STATE_MAPPED or STATE_IGNORED — never STATE_NEEDS_REVIEW
    system_id: Optional[str]
    system_name: Optional[str]
    reason: Optional[str]
    decided_by: str
    decided_at: datetime


def _state_for(latest_state: Optional[str]) -> str:
    """A stored reconciliation's own `state` column, or STATE_NEEDS_REVIEW
    when no reconciliation row exists at all — the LEFT JOIN LATERAL in
    `_LIST_FINDINGS_SQL` (and the plain LEFT JOIN in `_reconciliation_for`
    below) returns NULL for `latest.state` in exactly that case. This is
    the ONE place that turns "no row" into the third state's name, so the
    list route and the single-finding lookup can never disagree about
    what an absent reconciliation means.
    """
    return latest_state if latest_state is not None else STATE_NEEDS_REVIEW


def _to_finding_row(row: "RowProxy") -> FindingRow:
    return FindingRow(
        urn=row.urn,
        table_name=row.table_name,
        schema_name=row.schema_name,
        monitor_key=row.monitor_key,
        diff_status=row.diff_status,
        table_type=row.table_type,
        field_count=row.field_count,
        state=_state_for(row.state),
        system_id=row.system_id,
        system_name=row.system_name,
        reason=row.reason,
        decided_by=row.decided_by,
        decided_at=row.decided_at,
    )


def list_findings(
    db: Session,
    *,
    monitor_key: Optional[str] = None,
    state: Optional[str] = None,
) -> List[FindingRow]:
    """Every discovered table, one row each, with its reconciliation state
    — in ONE statement (see this module's docstring for why a per-row cost
    here would be a defect, not an optimisation opportunity).

    `monitor_key`, when given, scopes the list to one monitor's tables —
    harmless with today's single connection, and correct the day a second
    one exists. `state`, when given, filters to exactly one of the three
    states CLIENT-side is never asked to compute itself: "needs_review" is
    the queue the design calls out as the point of this screen, and a
    caller filtering to it must get exactly the tables with no
    reconciliation row, not an approximation of that.

    Raises ValueError for an unknown `state` value — the caller mistyped a
    filter, and silently returning everything (or nothing) would hide
    that.
    """
    if state is not None and state not in _VALID_FILTER_STATES:
        raise ValueError(
            f"unknown finding state filter: {state!r}; must be one of "
            f"{sorted(_VALID_FILTER_STATES)}"
        )
    rows = db.execute(
        _LIST_FINDINGS_SQL, {"monitor_key": monitor_key}
    ).all()
    findings = [_to_finding_row(row) for row in rows]
    if state is not None:
        findings = [f for f in findings if f.state == state]
    return findings


def _table_exists(db: Session, urn: str) -> bool:
    return db.execute(_TABLE_EXISTS_SQL, {"urn": urn}).first() is not None


def _system_exists(db: Session, system_id: str) -> bool:
    return db.execute(_SYSTEM_EXISTS_SQL, {"id": system_id}).first() is not None


def _system_name(db: Session, system_id: Optional[str]) -> Optional[str]:
    if system_id is None:
        return None
    row = db.execute(_SYSTEM_NAME_SQL, {"id": system_id}).first()
    return row.name if row is not None else None


def reconcile_finding(
    db: Session,
    *,
    urn: str,
    state: str,
    system_id: Optional[str],
    reason: Optional[str],
    decided_by: str,
) -> Reconciliation:
    """Records one reconciliation decision for a discovered table. NEVER
    updates: re-reconciling appends, same discipline
    privacycare_screening_decision already keeps for a screening verdict
    (models.py's discovery_reconciliation_table docstring: APPEND-ONLY).
    There is no update path here to accidentally call.

    Validates, in order:
      1. `state` is 'mapped' or 'ignored' — never 'needs_review', which is
         never a value a caller writes, only ever the absence of one.
      2. `urn` names a real, currently-discovered table — 404 territory
         for the caller, raised here as ValueError("no such finding: ...")
         so api/discovery.py can map it the same way api/screening.py maps
         "no such business process".
      3. The mapped/ignored shape: mapped requires a system_id (and
         forbids a reason — the linked system is the context, the same
         way gate.record_decision forbids a justification on a screen-IN);
         ignored requires a non-blank reason (and forbids a system_id).
      4. For 'mapped', that system_id actually names a real system —
         otherwise a typo'd id would silently record a reconciliation
         pointing at nothing.

    Returns the recorded Reconciliation, with the system's name resolved
    for display when one was given — one extra lookup for the single row
    just written, never a per-row cost (same reasoning
    record_screening_decision's own display resolution gives in
    api/screening.py).
    """
    if state not in _VALID_RECONCILE_STATES:
        raise ValueError(
            f"unknown reconciliation state: {state!r}; must be one of "
            f"{sorted(_VALID_RECONCILE_STATES)}"
        )

    if not _table_exists(db, urn):
        raise ValueError(f"no such finding: {urn!r}")

    if state == STATE_MAPPED:
        if reason is not None:
            raise ValueError(
                "reason must not be given when marking a finding as "
                "mapped — the linked system is the context, and a "
                "reason is only for ignoring a finding"
            )
        if not system_id:
            raise ValueError(
                "system_id is required when marking a finding as mapped"
            )
        if not _system_exists(db, system_id):
            raise ValueError(f"unknown system: {system_id!r}")
    else:  # STATE_IGNORED
        if system_id is not None:
            raise ValueError(
                "system_id must not be given when ignoring a finding — "
                "an ignored finding does not belong to a system"
            )
        if reason is None or not reason.strip():
            raise ValueError(
                "reason is required when ignoring a finding — this is "
                "the compliance record that explains why this table "
                "holds no personal data"
            )

    new_id = str(uuid.uuid4())
    result = db.execute(
        _INSERT_RECONCILIATION_SQL,
        {
            "id": new_id,
            "stagedresource_urn": urn,
            "state": state,
            "system_id": system_id,
            "reason": reason,
            "decided_by": decided_by,
        },
    ).first()

    return Reconciliation(
        id=new_id,
        urn=urn,
        state=state,
        system_id=system_id,
        system_name=_system_name(db, system_id),
        reason=reason,
        decided_by=decided_by,
        decided_at=result.decided_at,
    )


def reconciliation_history(db: Session, urn: str) -> List[Reconciliation]:
    """Every reconciliation ever recorded for this table's urn, newest
    first (see _SELECT_RECONCILIATION_HISTORY_SQL's own comment for the
    decided_at/id tie-break). Empty when the table has never been
    reconciled — that is "needs review", not an error, and this function
    does not raise for it; api/discovery.py's own existence check (mirrored
    from api/screening.py's _require_business_process) is what turns an
    unknown urn into a 404 ahead of this call.
    """
    rows = db.execute(_SELECT_RECONCILIATION_HISTORY_SQL, {"urn": urn}).all()
    return [
        Reconciliation(
            id=row.id,
            urn=row.stagedresource_urn,
            state=row.state,
            system_id=row.system_id,
            system_name=_system_name(db, row.system_id),
            reason=row.reason,
            decided_by=row.decided_by,
            decided_at=row.decided_at,
        )
        for row in rows
    ]
