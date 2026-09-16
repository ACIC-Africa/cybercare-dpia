"""The DPIA risk register's HTTP surface (spec 2026-09-16 D-W2-2, Task 5).

NAMESPACE. Our own /api/v1/privacycare/risk, NOT /api/v1/plus. Nothing in
the shipped admin UI calls this route — the risk register is new,
Kenyan-specific ground with no Plus analogue (see risk/register.py's own
module docstring for what it computes and why) — so taking a path in
Plus's namespace would only risk colliding with a real Plus endpoint
later. Same reasoning api/dsr.py and api/consent.py record for their own
surfaces.

SCOPES. PRIVACYCARE_RISK_READ guards the two read routes (list, ODPC
finding); PRIVACYCARE_RISK_CREATE guards both write routes (add, remove) —
one write scope for both, the same shape PRIVACYCARE_DSR_UPDATE already
uses across create/decision/notification. Unlike the DSR register
(PRIVACYCARE_DSR_READ) and the stale-consent report
(PRIVACYCARE_CONSENT_READ), PRIVACYCARE_RISK_READ IS granted to Viewer
(roles.py's viewer_scopes) — a risk register row (register.RiskEntry)
names a category, a free-text description, a likelihood and a severity;
it never names a data subject the way a DSR row or a stale-consent
finding does. See roles.py's own PRIVACYCARE_RISK_READ comment for the
fuller version of this argument.

THE PARKED TASK-3 FINDING, AND WHY THIS FILE FIXES IT AT THE ROUTE LAYER.
register.sync_projection has no existence check on assessment_id — its own
docstring and register.py's module comment both note this was unreachable
before this task, because its only callers (add_risk, remove_risk) both
prove the assessment exists first. This file is the first place outside
input reaches register.py's OTHER two assessment_id-keyed entry points —
list_risks, and (through odpc.evaluate) assessment_band — NEITHER of which
checks existence either. Called with a fabricated assessment_id,
list_risks silently returns [] and assessment_band silently returns LOW
(its own documented empty-register default): a caller who mistyped an id
would see "no risks, low residual risk, consultation not required" and
have no way to tell that apart from a real, freshly-created assessment
that legitimately has none yet.

The two cases are NOT the same and must not read the same. This module
adds its own existence check (_assessment_exists, below) ahead of both the
list route and the ODPC route, and answers 404 for an unknown id — the
obvious answer the brief names, and the one this module takes. The check
is a local copy of register.py's own private _ASSESSMENT_EXISTS_SQL query
rather than an import: register.py is one of this task's locked
interfaces ("do not rename or reimplement"; its function list does not
include this file among the modules to touch), so the check is added here
instead of exported from there. A REAL assessment with zero risks
recorded still passes _assessment_exists and returns 200 with an empty
list / a "not required" finding — that is the OTHER half of this task's
own test requirement, and the two must both hold at once.

The add route (add_risk) already raises ValueError for an unknown
assessment_id — register.py's own existence check, run before any row is
written — alongside its OTHER ValueError case (a category outside Carol's
seven). Both were already safe against a bad id before this task; this
route only decides what status code each becomes on the way out: "no such
assessment" becomes 404 (the path segment names a resource — the parent
assessment a risk is being added under — that does not exist; REST
convention for POSTing a sub-resource under a missing parent), anything
else becomes 400 (an otherwise-valid assessment, bad input). The remove
route is keyed by risk_id, not assessment_id, and carries no version of
this problem at all: remove_risk's own DELETE ... RETURNING already tells
a real removal apart from a no-op, with nothing to silently default to.
"""
import sqlalchemy
from fastapi import Depends, HTTPException, Security
from fastapi import status as status_codes
from fastapi_pagination import Page, Params, paginate
from sqlalchemy.orm import Session

from fides.api.deps import get_db
from fides.api.oauth.utils import verify_oauth_client
from fides.api.privacycare.api.risk_schemas import (
    OdpcFindingResponse,
    RemoveRiskResponse,
    RiskCreate,
    RiskResponse,
)
from fides.api.privacycare.api.router import privacycare_risk_router
from fides.api.privacycare.risk.odpc import evaluate
from fides.api.privacycare.risk.register import (
    RiskEntry,
    add_risk,
    list_risks,
    remove_risk,
)
from fides.common.scope_registry import PRIVACYCARE_RISK_CREATE, PRIVACYCARE_RISK_READ

# A local copy of register.py's own private _ASSESSMENT_EXISTS_SQL — see
# this module's docstring ("THE PARKED TASK-3 FINDING") for why it lives
# here rather than being exported from register.py.
_ASSESSMENT_EXISTS_SQL = sqlalchemy.text(
    "SELECT 1 FROM privacy_assessment WHERE id = :id"
)


def _assessment_exists(db: Session, assessment_id: str) -> bool:
    return db.execute(_ASSESSMENT_EXISTS_SQL, {"id": assessment_id}).first() is not None


def _require_assessment(db: Session, assessment_id: str) -> None:
    if not _assessment_exists(db, assessment_id):
        raise HTTPException(
            status_code=status_codes.HTTP_404_NOT_FOUND,
            detail=f"no such assessment: {assessment_id!r}",
        )


def _response_from_entry(entry: RiskEntry) -> RiskResponse:
    return RiskResponse(
        id=entry.id,
        assessment_id=entry.assessment_id,
        category=entry.category,
        description=entry.description,
        likelihood=entry.likelihood,
        severity=entry.severity,
        score=entry.score,
        band=entry.band,
    )


@privacycare_risk_router.get(
    "/{assessment_id}",
    dependencies=[Security(verify_oauth_client, scopes=[PRIVACYCARE_RISK_READ])],
    response_model=Page[RiskResponse],
)
def list_assessment_risks(
    assessment_id: str,
    *,
    params: Params = Depends(),
    db: Session = Depends(get_db),
) -> Page[RiskResponse]:
    """Every risk recorded against this assessment, highest score first
    (register.list_risks' own ordering).

    404 for an unknown assessment_id; 200 with an empty page for a real
    assessment that has none recorded yet — see this module's docstring
    for why the two must not read the same.
    """
    _require_assessment(db, assessment_id)
    entries = list_risks(db, assessment_id)
    return paginate([_response_from_entry(entry) for entry in entries], params)


@privacycare_risk_router.post(
    "/{assessment_id}",
    dependencies=[Security(verify_oauth_client, scopes=[PRIVACYCARE_RISK_CREATE])],
    response_model=RiskResponse,
    status_code=status_codes.HTTP_201_CREATED,
)
def add_assessment_risk(
    assessment_id: str,
    request: RiskCreate,
    *,
    db: Session = Depends(get_db),
) -> RiskResponse:
    """Records one risk and resyncs the assessment's projected risk_level
    (register.add_risk's own side effect) in the same call.

    See this module's docstring for why "no such assessment" and a bad
    category — both a ValueError out of add_risk — are answered with
    different status codes here.
    """
    try:
        entry = add_risk(
            db,
            assessment_id=assessment_id,
            category=request.category,
            description=request.description,
            likelihood=request.likelihood,
            severity=request.severity,
        )
    except ValueError as exc:
        db.rollback()
        detail = str(exc)
        status_code = (
            status_codes.HTTP_404_NOT_FOUND
            if detail.startswith("no such assessment")
            else status_codes.HTTP_400_BAD_REQUEST
        )
        raise HTTPException(status_code=status_code, detail=detail) from exc
    db.commit()
    return _response_from_entry(entry)


@privacycare_risk_router.delete(
    "/{risk_id}",
    dependencies=[Security(verify_oauth_client, scopes=[PRIVACYCARE_RISK_CREATE])],
    response_model=RemoveRiskResponse,
)
def remove_assessment_risk(
    risk_id: str,
    *,
    db: Session = Depends(get_db),
) -> RemoveRiskResponse:
    """Removes one risk and resyncs its assessment's projected risk_level
    (register.remove_risk's own side effect) in the same call.

    Keyed by risk_id, not assessment_id — register.remove_risk's own
    DELETE ... RETURNING already tells a real removal apart from a no-op,
    so a False return maps straight to 404 with no existence-check gap of
    the kind this module's docstring names for the other two routes.
    """
    removed = remove_risk(db, risk_id)
    if not removed:
        raise HTTPException(
            status_code=status_codes.HTTP_404_NOT_FOUND,
            detail=f"no such risk: {risk_id!r}",
        )
    db.commit()
    return RemoveRiskResponse(id=risk_id, removed=True)


@privacycare_risk_router.get(
    "/{assessment_id}/odpc",
    dependencies=[Security(verify_oauth_client, scopes=[PRIVACYCARE_RISK_READ])],
    response_model=OdpcFindingResponse,
)
def get_odpc_finding(
    assessment_id: str,
    *,
    db: Session = Depends(get_db),
) -> OdpcFindingResponse:
    """Whether this assessment's current register requires prior
    consultation with the ODPC (risk/odpc.py's evaluate), and what drove
    it.

    404 for an unknown assessment_id — see this module's docstring for why
    evaluate() would otherwise silently report "low, not required" for a
    typo'd id, indistinguishable from a real, freshly-created assessment.
    """
    _require_assessment(db, assessment_id)
    finding = evaluate(db, assessment_id)
    return OdpcFindingResponse(
        required=finding.required,
        band=finding.band,
        window_days=finding.window_days,
        reason=finding.reason,
        highest_risk=(
            _response_from_entry(finding.highest_risk)
            if finding.highest_risk is not None
            else None
        ),
    )
