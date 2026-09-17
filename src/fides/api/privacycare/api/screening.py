"""The DPIA screening gate's HTTP surface (spec 2026-09-16 D-W2-7, Task 4).

NAMESPACE. Our own /api/v1/privacycare/screening, NOT /api/v1/plus — the
screening gate is new, Kenyan-specific ground with no Plus analogue (see
screening/gate.py's own module docstring for what it computes and why), so
taking a path in Plus's namespace would only risk colliding with a real
Plus endpoint later. Same reasoning api/risk.py and api/consent.py record
for their own surfaces.

SCOPES. PRIVACYCARE_SCREENING_READ guards the three read routes (list
triggers, current verdict, decision history); PRIVACYCARE_SCREENING_CREATE
guards the one write route (record a decision) — one write scope for the
one write verb, the same shape PRIVACYCARE_RISK_CREATE and
PRIVACYCARE_DSR_UPDATE already use. PRIVACYCARE_SCREENING_READ IS granted
to Viewer (roles.py's viewer_scopes) — a screening decision
(gate.ScreeningVerdict) names an activity, which triggers were ticked and a
free-text justification for a screen-out, never a data subject, the same
distinction roles.py's PRIVACYCARE_RISK_READ comment draws for a
risk-register row. See roles.py's own PRIVACYCARE_SCREENING_READ comment
for the fuller version of this argument.

THE DISTINCTION THIS FILE EXISTS TO ENFORCE. gate.py's own read functions
(current_verdict, decision_history) have NO existence check on
declaration_id at all — called with a fabricated id, current_verdict
silently returns None (its own documented "never screened" answer) and
decision_history silently returns []. Both of those are also the CORRECT
answer for a REAL declaration that has simply never been screened, so the
two cases are indistinguishable from inside gate.py alone. This module adds
its own existence check (_require_declaration, below — a local copy of the
same query gate.py's own private _DECLARATION_EXISTS_SQL runs, per that
module's comment on why a FK to privacydeclaration is deliberately absent)
ahead of every declaration_id-keyed route, and answers 404 for an unknown
id. A REAL declaration that has never been screened still passes
_require_declaration and reads as a 200 with verdict=None / decisions=[] —
that is the brief's own requirement, and the two must both hold at once:
"never screened" and "no such declaration" are different answers, and a
caller must be able to tell them apart.

record_decision (gate.py) already runs this same existence check itself,
after its own trigger-key and justification checks, and raises ValueError
for it — the record route below maps that by string prefix into 404, the
same "no such X" / anything-else split api/risk.py's add_assessment_risk
route already uses for register.add_risk's two ValueError cases. This
module's own _require_declaration is therefore only needed on the three
read routes, where gate.py has no check to defer to at all.
"""
from fastapi import Depends, HTTPException, Security
from fastapi import status as status_codes
from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

from fides.api.deps import get_db
from fides.api.models.client import ClientDetail
from fides.api.oauth.utils import verify_oauth_client
from fides.api.privacycare.api.identity import _created_by_from_client
from fides.api.privacycare.api.router import privacycare_screening_router
from fides.api.privacycare.api.screening_schemas import (
    CurrentScreeningResponse,
    ScreeningDecisionRequest,
    ScreeningHistoryResponse,
    ScreeningVerdictResponse,
    TriggerListResponse,
    TriggerResponse,
)
from fides.api.privacycare.screening.gate import (
    ScreeningVerdict,
    current_verdict,
    decision_history,
    list_triggers,
    record_decision,
)
from fides.common.scope_registry import (
    PRIVACYCARE_SCREENING_CREATE,
    PRIVACYCARE_SCREENING_READ,
)

# A local copy of gate.py's own private _DECLARATION_EXISTS_SQL — see this
# module's docstring for why it lives here rather than being exported from
# there (gate.py is one of this task's locked interfaces: "do not rename or
# reimplement"). Reading privacydeclaration for exactly this existence
# check is the one permitted contact with an Ethyca table this task allows.
_DECLARATION_EXISTS_SQL = sql_text("SELECT 1 FROM privacydeclaration WHERE id = :id")


def _declaration_exists(db: Session, declaration_id: str) -> bool:
    return db.execute(_DECLARATION_EXISTS_SQL, {"id": declaration_id}).first() is not None


def _require_declaration(db: Session, declaration_id: str) -> None:
    if not _declaration_exists(db, declaration_id):
        raise HTTPException(
            status_code=status_codes.HTTP_404_NOT_FOUND,
            detail=f"no such declaration: {declaration_id!r}",
        )


def _response_from_verdict(verdict: ScreeningVerdict) -> ScreeningVerdictResponse:
    return ScreeningVerdictResponse(
        declaration_id=verdict.declaration_id,
        dpia_required=verdict.dpia_required,
        triggered_keys=verdict.triggered_keys,
        justification=verdict.justification,
        decided_by=verdict.decided_by,
        decided_at=verdict.decided_at,
    )


# ORDERING NOTE: "/triggers" is registered here, on THIS router, before
# "/{declaration_id}" immediately below. Both are GET on the same router,
# and fides.api.util.api_router.APIRouter (like Starlette generally)
# matches in registration order — if "/{declaration_id}" came first, a
# request for GET /triggers would match it instead, with
# declaration_id="triggers", and the real triggers route would 404 forever
# against a route that demonstrably exists. This is the exact hazard
# api/router.py's register() documents at length for its own
# tasks-vs-{assessment_id} ordering; the fix here is the same one, just
# local to a single file rather than three imports.
@privacycare_screening_router.get(
    "/triggers",
    dependencies=[Security(verify_oauth_client, scopes=[PRIVACYCARE_SCREENING_READ])],
    response_model=TriggerListResponse,
)
def list_screening_triggers(*, db: Session = Depends(get_db)) -> TriggerListResponse:
    """The screener's questions, ordered by display_order (gate.
    list_triggers' own ordering). Empty when nothing has been seeded yet —
    the live privacycare_screening_trigger table is empty as of this task;
    seeding it for real is Task 5's job."""
    rows = list_triggers(db)
    return TriggerListResponse(triggers=[TriggerResponse(**row) for row in rows])


@privacycare_screening_router.get(
    "/{declaration_id}",
    dependencies=[Security(verify_oauth_client, scopes=[PRIVACYCARE_SCREENING_READ])],
    response_model=CurrentScreeningResponse,
)
def get_current_screening_verdict(
    declaration_id: str,
    *,
    db: Session = Depends(get_db),
) -> CurrentScreeningResponse:
    """The newest screening decision for this declaration (gate.
    current_verdict), or verdict=None when it has never been screened at
    all.

    404 for an unknown declaration_id; 200 with verdict=None for a real
    declaration that simply has not been screened yet — see this module's
    docstring for why the two must not read the same.
    """
    _require_declaration(db, declaration_id)
    verdict = current_verdict(db, declaration_id)
    return CurrentScreeningResponse(
        declaration_id=declaration_id,
        verdict=_response_from_verdict(verdict) if verdict is not None else None,
    )


@privacycare_screening_router.get(
    "/{declaration_id}/history",
    dependencies=[Security(verify_oauth_client, scopes=[PRIVACYCARE_SCREENING_READ])],
    response_model=ScreeningHistoryResponse,
)
def get_screening_history(
    declaration_id: str,
    *,
    db: Session = Depends(get_db),
) -> ScreeningHistoryResponse:
    """Every screening decision ever recorded for this declaration, newest
    first (gate.decision_history's own ordering). 404 for an unknown
    declaration_id; 200 with decisions=[] for a real declaration that has
    never been screened — same distinction as the current-verdict route
    above, and this route carries none of the two-different-Nones ambiguity
    that route has (an empty list already means exactly one thing)."""
    _require_declaration(db, declaration_id)
    history = decision_history(db, declaration_id)
    return ScreeningHistoryResponse(
        declaration_id=declaration_id,
        decisions=[_response_from_verdict(v) for v in history],
    )


@privacycare_screening_router.post(
    "/{declaration_id}",
    dependencies=[Security(verify_oauth_client, scopes=[PRIVACYCARE_SCREENING_CREATE])],
    response_model=ScreeningVerdictResponse,
    status_code=status_codes.HTTP_201_CREATED,
)
def record_screening_decision(
    declaration_id: str,
    request: ScreeningDecisionRequest,
    *,
    db: Session = Depends(get_db),
    client: ClientDetail = Security(
        verify_oauth_client, scopes=[PRIVACYCARE_SCREENING_CREATE]
    ),
) -> ScreeningVerdictResponse:
    """Records one screening decision (gate.record_decision) and returns
    the resulting verdict. Re-screening APPENDS, per gate.py's own
    docstring — there is no update path to call.

    gate.record_decision already runs its own declaration_id existence
    check — after its own trigger-key and justification checks — and
    raises ValueError("no such declaration: ...") for it; this route does
    NOT duplicate that check ahead of the call (unlike the two GET routes
    above, which have no core check to defer to). See this module's
    docstring for why: "no such declaration" is mapped to 404 here, the
    same way api/risk.py's add_assessment_risk maps register.add_risk's own
    "no such assessment" ValueError, and anything else (an unknown trigger
    key, a missing or blank screen-out justification, a justification
    supplied on a screen-in) becomes 400 — the declaration_id path segment
    names a resource that either does or doesn't exist; every other
    rejection is otherwise-valid-resource, bad input.
    """
    decided_by = _created_by_from_client(client)
    try:
        verdict = record_decision(
            db,
            declaration_id=declaration_id,
            triggered_keys=request.triggered_keys,
            justification=request.justification,
            decided_by=decided_by,
        )
    except ValueError as exc:
        db.rollback()
        detail = str(exc)
        status_code = (
            status_codes.HTTP_404_NOT_FOUND
            if detail.startswith("no such declaration")
            else status_codes.HTTP_400_BAD_REQUEST
        )
        raise HTTPException(status_code=status_code, detail=detail) from exc
    db.commit()
    return _response_from_verdict(verdict)
