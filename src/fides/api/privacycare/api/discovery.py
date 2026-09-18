"""The discovery-findings HTTP surface (2026-09-18 discovery-findings-API
brief).

WHAT THIS CLOSES. A discovery scan already walks a real catalogue and
writes real `stagedresource` rows (api/monitors.py, discovery/walk.py,
discovery/reconcile.py) — 1 Database, 1 Schema, 176 Tables, 1703 Fields
against our own `fides-db` as of this task. Nothing before this module
lists a single one of them, or lets a person say what one is. Screen 4's
whole reason for existing — Josephine's brief, area 2: "flag any newly
discovered or undocumented processing activity for review" — depended on
exactly this surface, and did not have it (see docs/design/
privacycare-screens/04-discovery-build-report.md, the earlier build's own
honest admission of the gap).

NAMESPACE. Our own /api/v1/privacycare/discovery, NOT /api/v1/plus. The
shipped admin UI's discovery-monitor screen calls `/plus/discovery-monitor*`
for MONITOR CONFIGURATION (api/monitors.py's own module docstring), but
nothing in that screen — or anywhere else in the shipped UI — calls a
per-finding list or reconcile route, because none existed until now. Taking
a path in Plus's namespace would only risk colliding with a real Plus
endpoint later. Same reasoning api/risk.py, api/screening.py and
api/consent.py record for their own surfaces.

SCOPES. PRIVACYCARE_DISCOVERY_READ (already granted to Viewer — roles.py)
guards the two read routes (list findings, one finding's reconciliation
history); PRIVACYCARE_DISCOVERY_UPDATE (already withheld from Viewer)
guards the one write route (reconcile). Both scopes already existed before
this task — see monitors.py's own use of the same pair for running a scan
— so no scope registry edit was needed.

ONE ROW PER TABLE, NEVER PER FIELD. 1703 discovered Fields is not a work
queue; a Table with its own field count is (the brief's own words: "one
row per discovered table: the schema and table, how many columns, and...
whether it is already accounted for"). Every Field this task's own scan
produced is still visible — through field_count on its parent Table's
row — without ever becoming its own line the way Screen 4 designs
explicitly warn against ("1703 fields is not a work queue").

THE THREE STATES, and the 404-vs-empty distinction that already governs
every other single-resource route in this codebase. See discovery/
findings.py's own module docstring for what "mapped" / "ignored" /
"needs_review" mean and why the third is never a stored value. This
module's own existence check (_require_finding, below — a local copy of
findings.py's private _table_exists query, per the same "do not export a
private check out of a locked module" precedent api/screening.py's own
docstring and api/risk.py's "THE PARKED TASK-3 FINDING" both give) answers
404 for an unknown urn ahead of the history route, so a caller can tell
"this table was never discovered" apart from "this table was discovered
and has simply never been reconciled" (200, empty history) — the exact
distinction api/screening.py's decision_history and current_verdict
already keep for a business process nobody has screened.

reconcile_finding (findings.py) already runs its own existence check —
after its own state/system_id/reason validation — and raises
ValueError("no such finding: ...") for it; the reconcile route below does
NOT duplicate that check ahead of the call, the same choice
record_screening_decision makes in api/screening.py and add_assessment_risk
makes in api/risk.py, for the same reason: the resource-existence failure
and the input-validation failures are both ValueError out of the same
call, and this route's job is only to decide which HTTP status each one
becomes on the way out.
"""
from typing import Optional

import sqlalchemy
from fastapi import Depends, HTTPException, Security
from fastapi import status as status_codes
from sqlalchemy.orm import Session

from fides.api.deps import get_db
from fides.api.models.client import ClientDetail
from fides.api.oauth.utils import verify_oauth_client
from fides.api.privacycare.api.discovery_schemas import (
    FindingHistoryResponse,
    FindingListResponse,
    FindingResponse,
    ReconcileFindingRequest,
    ReconciliationResponse,
)
from fides.api.privacycare.api.identity import _created_by_from_client
from fides.api.privacycare.api.router import privacycare_discovery_findings_router
from fides.api.privacycare.discovery.findings import (
    FindingRow,
    Reconciliation,
    list_findings,
    reconcile_finding,
    reconciliation_history,
)
from fides.common.scope_registry import (
    PRIVACYCARE_DISCOVERY_READ,
    PRIVACYCARE_DISCOVERY_UPDATE,
)

# A local copy of findings.py's own private _TABLE_EXISTS_SQL — see this
# module's docstring for why it lives here rather than being exported from
# there (findings.py's exists-check is deliberately private; the same
# "do not export a private check" precedent api/screening.py and
# api/risk.py both already give for their own core modules).
_FINDING_EXISTS_SQL = sqlalchemy.text(
    "SELECT 1 FROM stagedresource WHERE urn = :urn AND resource_type = 'Table'"
)


def _finding_exists(db: Session, urn: str) -> bool:
    return db.execute(_FINDING_EXISTS_SQL, {"urn": urn}).first() is not None


def _require_finding(db: Session, urn: str) -> None:
    if not _finding_exists(db, urn):
        raise HTTPException(
            status_code=status_codes.HTTP_404_NOT_FOUND,
            detail=f"no such finding: {urn!r}",
        )


def _response_from_finding(row: FindingRow) -> FindingResponse:
    return FindingResponse(
        urn=row.urn,
        table_name=row.table_name,
        schema_name=row.schema_name,
        monitor_key=row.monitor_key,
        field_count=row.field_count,
        table_type=row.table_type,
        diff_status=row.diff_status,
        state=row.state,
        system_id=row.system_id,
        system_name=row.system_name,
        reason=row.reason,
        decided_by=row.decided_by,
        decided_at=row.decided_at,
    )


def _response_from_reconciliation(entry: Reconciliation) -> ReconciliationResponse:
    return ReconciliationResponse(
        id=entry.id,
        urn=entry.urn,
        state=entry.state,
        system_id=entry.system_id,
        system_name=entry.system_name,
        reason=entry.reason,
        decided_by=entry.decided_by,
        decided_at=entry.decided_at,
    )


@privacycare_discovery_findings_router.get(
    "",
    dependencies=[Security(verify_oauth_client, scopes=[PRIVACYCARE_DISCOVERY_READ])],
    response_model=FindingListResponse,
)
def list_discovery_findings(
    state: Optional[str] = None,
    monitor_key: Optional[str] = None,
    *,
    db: Session = Depends(get_db),
) -> FindingListResponse:
    """Every discovered table, one row each, in ONE call — see
    discovery/findings.py's module docstring for why a per-row fetch here
    would be a defect against 176 tables (and counting), not an
    optimisation opportunity.

    `state`, when given, filters to exactly one of "mapped" / "ignored" /
    "needs_review" — "needs_review" is the queue this screen exists to
    surface (DESIGN.md Screen 4: "Default the list to Needs review,
    because that is the work"); that default is a UI concern, this route
    simply makes the filter available and correct. `monitor_key`, when
    given, scopes the list to one monitor's tables.

    A bad `state` value is a 400, not a silently-empty or silently-full
    list — list_findings raises ValueError for one and this route maps it
    accordingly.
    """
    try:
        findings = list_findings(db, monitor_key=monitor_key, state=state)
    except ValueError as exc:
        raise HTTPException(
            status_code=status_codes.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    return FindingListResponse(
        findings=[_response_from_finding(row) for row in findings]
    )


@privacycare_discovery_findings_router.get(
    "/{urn}/history",
    dependencies=[Security(verify_oauth_client, scopes=[PRIVACYCARE_DISCOVERY_READ])],
    response_model=FindingHistoryResponse,
)
def get_finding_history(
    urn: str,
    *,
    db: Session = Depends(get_db),
) -> FindingHistoryResponse:
    """Every reconciliation ever recorded for this table, newest first
    (findings.reconciliation_history's own ordering) — who decided, when,
    and why (DESIGN.md Screen 4's own requirement: "a reconciliation is a
    record, not a toggle").

    404 for a urn that was never discovered at all; 200 with
    reconciliations=[] for a real, currently-discovered table nobody has
    reconciled yet — see this module's docstring for why the two must not
    read the same.
    """
    _require_finding(db, urn)
    history = reconciliation_history(db, urn)
    return FindingHistoryResponse(
        urn=urn,
        reconciliations=[_response_from_reconciliation(entry) for entry in history],
    )


@privacycare_discovery_findings_router.post(
    "/{urn}/reconcile",
    dependencies=[
        Security(verify_oauth_client, scopes=[PRIVACYCARE_DISCOVERY_UPDATE])
    ],
    response_model=ReconciliationResponse,
    status_code=status_codes.HTTP_201_CREATED,
)
def reconcile_discovery_finding(
    urn: str,
    request: ReconcileFindingRequest,
    *,
    db: Session = Depends(get_db),
    client: ClientDetail = Security(
        verify_oauth_client, scopes=[PRIVACYCARE_DISCOVERY_UPDATE]
    ),
) -> ReconciliationResponse:
    """Records one reconciliation decision for a discovered table: mark it
    as belonging to a system (`state="mapped"`, `system_id` required), or
    ignore it with a written reason (`state="ignored"`, `reason`
    required). Re-reconciling APPENDS — findings.reconcile_finding's own
    docstring — there is no update path to call; an earlier decision
    survives being revisited, same discipline
    privacycare_screening_decision already keeps for a screening verdict.

    See this module's docstring for why "no such finding" (404) and every
    other rejection out of reconcile_finding's single ValueError type
    (unknown state; a mapped request missing system_id or naming an
    unknown one; a mapped request carrying a reason; an ignored request
    missing a reason, carrying only whitespace, or naming a system_id) —
    all mapped to 400 here — are told apart the same way
    record_screening_decision already tells apart "no such business
    process" from every other rejection out of gate.record_decision.
    """
    decided_by = _created_by_from_client(client)
    try:
        result = reconcile_finding(
            db,
            urn=urn,
            state=request.state,
            system_id=request.system_id,
            reason=request.reason,
            decided_by=decided_by,
        )
    except ValueError as exc:
        db.rollback()
        detail = str(exc)
        status_code = (
            status_codes.HTTP_404_NOT_FOUND
            if detail.startswith("no such finding")
            else status_codes.HTTP_400_BAD_REQUEST
        )
        raise HTTPException(status_code=status_code, detail=detail) from exc
    db.commit()
    return _response_from_reconciliation(result)
