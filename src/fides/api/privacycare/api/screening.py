"""The DPIA screening gate's HTTP surface (spec 2026-09-16 D-W2-7, Task 4;
re-keyed to the business process in plan 20, Task 3; the mapping route
added in plan 20, Task 4).

NAMESPACE. Our own /api/v1/privacycare/screening, NOT /api/v1/plus — the
screening gate is new, Kenyan-specific ground with no Plus analogue (see
screening/gate.py's own module docstring for what it computes and why), so
taking a path in Plus's namespace would only risk colliding with a real
Plus endpoint later. Same reasoning api/risk.py and api/consent.py record
for their own surfaces.

RE-KEY (plan 20, Task 3). Task 1 moved gate.py's whole interface from a
processing activity (privacydeclaration — 2 of those, both invented) to a
business process (privacycare_business_process — 86 of the customer's own,
real ones); Task 2 fixed the generation gate's read of it. This module was
the last one still speaking declaration_id: every route below is now keyed
to {business_process_id}, ScreeningVerdictResponse/CurrentScreeningResponse/
ScreeningHistoryResponse (screening_schemas.py) carry business_process_id
instead of declaration_id, and this module's own existence check reads
privacycare_business_process instead of privacydeclaration.

SCOPES. PRIVACYCARE_SCREENING_READ guards the four read routes (list every
business process's status, list triggers, current verdict, decision
history); PRIVACYCARE_SCREENING_CREATE guards both write routes (record a
decision, save a data mapping) — Task 4 reuses the existing write scope
rather than adding a second one, since the brief named none and a mapping
is, structurally, one more way of recording facts about a business
process's assessability. PRIVACYCARE_SCREENING_READ IS granted to Viewer
(roles.py's viewer_scopes) — a screening decision (gate.ScreeningVerdict)
names a business process, which triggers were ticked and a free-text
justification for a screen-out, never a data subject, the same distinction
roles.py's PRIVACYCARE_RISK_READ comment draws for a risk-register row.
This still holds after Task 4: nothing in this router lets a caller READ
back a mapping's data subjects, and Viewer lacks the CREATE scope the
mapping route requires, so a data subject is still never something Viewer
can reach through this surface. See roles.py's own
PRIVACYCARE_SCREENING_READ comment for the fuller version of this argument.

THE DISTINCTION THIS FILE EXISTS TO ENFORCE. gate.py's own read functions
(current_verdict, decision_history) have NO existence check on
business_process_id at all — called with a fabricated id, current_verdict
silently returns None (its own documented "never screened" answer) and
decision_history silently returns []. Both of those are also the CORRECT
answer for a REAL business process that has simply never been screened, so
the two cases are indistinguishable from inside gate.py alone. This module
adds its own existence check (_require_business_process, below — a local
copy of the same query gate.py's own private _BUSINESS_PROCESS_EXISTS_SQL
runs, per that module's comment on why record_decision's own FK-backed
check is not exported) ahead of every business_process_id-keyed read route,
and answers 404 for an unknown id. A REAL business process that has never
been screened still passes _require_business_process and reads as a 200
with verdict=None / decisions=[] — that is the brief's own requirement, and
the two must both hold at once: "never screened" and "no such business
process" are different answers, and a caller must be able to tell them
apart.

record_decision (gate.py) already runs this same existence check itself,
after its own trigger-key and justification checks, and raises ValueError
("no such business process: ...") for it — the record route below maps
that by string prefix into 404, the same "no such X" / anything-else split
api/risk.py's add_assessment_risk route already uses for register.add_risk's
two ValueError cases. This module's own _require_business_process is
therefore only needed on the two single-resource read routes, where gate.py
has no check to defer to at all — the list route (below) has nothing to
404 on: it always returns every row that exists, which may be zero.
"""
from fastapi import Depends, HTTPException, Security
from fastapi import status as status_codes
from fastapi.security import SecurityScopes
from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

from fides.api.deps import get_db
from fides.api.models.client import ClientDetail
from fides.api.models.sql_models import System  # type: ignore[attr-defined]
from fides.api.oauth.system_manager_oauth_util import (
    SystemAuthContainer,
    has_system_permissions,
)
from fides.api.oauth.utils import (
    PermissionCheckerCallback,
    _resolve_depends,
    extract_token_and_load_client,
    get_permission_checker,
    oauth2_scheme,
    verify_oauth_client,
)
from fides.api.privacycare.api.identity import _created_by_from_client
from fides.api.privacycare.api.router import privacycare_screening_router
from fides.api.privacycare.api.screening_schemas import (
    CurrentScreeningResponse,
    DataMappingRequest,
    DataMappingResponse,
    ScreeningDecisionRequest,
    ScreeningHistoryResponse,
    ScreeningListResponse,
    ScreeningStatusResponse,
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
from fides.api.privacycare.screening.mapping import (
    MappingResult,
    existing_system_id_for_process,
    save_mapping,
)
from fides.common.scope_registry import (
    PRIVACYCARE_SCREENING_CREATE,
    PRIVACYCARE_SCREENING_READ,
    SYSTEM_UPDATE,
)

# A local copy of gate.py's own private _BUSINESS_PROCESS_EXISTS_SQL — see
# this module's docstring for why it lives here rather than being exported
# from there (gate.py is one of this task's locked interfaces: "do not
# rename or reimplement"). Deliberately NOT filtered on deleted_at IS NULL:
# gate.record_decision's own existence check (the one this route's POST
# path defers to) carries no such filter either, and a GET that 404s on an
# id the POST would still accept — or vice versa — would be a new
# inconsistency this task did not introduce and was not asked to close.
# Carried forward as a named, not silent, gap — see this task's report.
_BUSINESS_PROCESS_EXISTS_SQL = sql_text(
    "SELECT 1 FROM privacycare_business_process WHERE id = :id"
)

# Every business process (excluding soft-deleted ones, same filter
# api/processes.py's own _SELECT_PROCESSES_SQL applies) with its current
# screening status, in ONE statement. A per-row current_verdict/
# decision_history call for each of the customer's 86+ business processes
# would be 86+ round trips for a table she works through in a single
# sitting — see this module's docstring and the list route below.
#
# has_mapping: the "mapped" subquery INNER JOINs privacycare_process_
# declaration to privacydeclaration, so a link whose privacy_declaration_id
# no longer resolves to a real row (one exists in this customer's live
# data: a declaration deleted after the link was made) produces no row
# there at all — it is silently excluded rather than raised on, so an
# orphan link never counts as "mapped" and never crashes this query. That
# tolerance comes from the join itself, not a special case in Python.
#
# latest: LEFT JOIN LATERAL rather than a plain join against
# privacycare_screening_decision, because a business process can carry more
# than one decision (re-screening APPENDS — gate.record_decision's own
# docstring) and this list needs only the newest one per process, with the
# same decided_at/id tie-break gate.py's own _SELECT_DECISIONS_SQL uses, so
# this list can never disagree with GET /{business_process_id} about which
# decision is "current".
_LIST_SCREENING_STATUS_SQL = sql_text(
    """
    SELECT
        bp.id AS business_process_id,
        bp.name AS name,
        bp.business_cycle AS business_cycle,
        latest.dpia_required AS dpia_required,
        latest.decided_by AS decided_by,
        latest.decided_at AS decided_at,
        (mapped.business_process_id IS NOT NULL) AS has_mapping
    FROM privacycare_business_process bp
    LEFT JOIN LATERAL (
        SELECT d.dpia_required, d.decided_by, d.decided_at
        FROM privacycare_screening_decision d
        WHERE d.business_process_id = bp.id
        ORDER BY d.decided_at DESC, d.id DESC
        LIMIT 1
    ) latest ON true
    LEFT JOIN (
        SELECT DISTINCT pd.business_process_id
        FROM privacycare_process_declaration pd
        JOIN privacydeclaration decl ON decl.id = pd.privacy_declaration_id
    ) mapped ON mapped.business_process_id = bp.id
    WHERE bp.deleted_at IS NULL
    ORDER BY bp.business_cycle NULLS LAST, bp.name, bp.id
    """
)


def _business_process_exists(db: Session, business_process_id: str) -> bool:
    return (
        db.execute(_BUSINESS_PROCESS_EXISTS_SQL, {"id": business_process_id}).first()
        is not None
    )


def _require_business_process(db: Session, business_process_id: str) -> None:
    if not _business_process_exists(db, business_process_id):
        raise HTTPException(
            status_code=status_codes.HTTP_404_NOT_FOUND,
            detail=f"no such business process: {business_process_id!r}",
        )


def _response_from_verdict(verdict: ScreeningVerdict) -> ScreeningVerdictResponse:
    return ScreeningVerdictResponse(
        business_process_id=verdict.business_process_id,
        dpia_required=verdict.dpia_required,
        triggered_keys=verdict.triggered_keys,
        justification=verdict.justification,
        decided_by=verdict.decided_by,
        decided_at=verdict.decided_at,
    )


def _response_from_mapping(result: MappingResult) -> DataMappingResponse:
    return DataMappingResponse(
        business_process_id=result.business_process_id,
        privacy_declaration_id=result.privacy_declaration_id,
        system_id=result.system_id,
        name=result.name,
        data_subjects=result.data_subjects,
        data_categories=result.data_categories,
        ground=result.ground,
        fides_legal_basis=result.fides_legal_basis,
        purpose=result.purpose,
        retention_period=result.retention_period,
        third_parties=result.third_parties,
        processes_special_category_data=result.processes_special_category_data,
        created=result.created,
    )


def _screening_status_response(row) -> ScreeningStatusResponse:
    return ScreeningStatusResponse(
        business_process_id=row.business_process_id,
        name=row.name,
        business_cycle=row.business_cycle,
        dpia_required=row.dpia_required,
        decided_by=row.decided_by,
        decided_at=row.decided_at,
        has_mapping=row.has_mapping,
    )


# ORDERING NOTE: "" and "/triggers" are both registered here, ahead of
# "/{business_process_id}" below. Both are GET on the same router, and
# fides.api.util.api_router.APIRouter (like Starlette generally) matches in
# registration order — if "/{business_process_id}" came first, a request
# for GET /triggers would match it instead, with
# business_process_id="triggers", and the real triggers route would 404
# forever against a route that demonstrably exists. This is the exact
# hazard api/router.py's register() documents at length for its own
# tasks-vs-{assessment_id} ordering; the fix here is the same one, just
# local to a single file rather than three imports.
#
# "" (the bare prefix) carries no risk of that particular collision — it
# requires ZERO extra path segments, where "{business_process_id}" requires
# exactly one non-empty one, so the two route templates can never match the
# same request regardless of registration order. It is placed here anyway,
# ahead of "/{business_process_id}", purely so the two routes with no id in
# their path read together.
@privacycare_screening_router.get(
    "",
    dependencies=[Security(verify_oauth_client, scopes=[PRIVACYCARE_SCREENING_READ])],
    response_model=ScreeningListResponse,
)
def list_screening_status(*, db: Session = Depends(get_db)) -> ScreeningListResponse:
    """Every business process with its screening status, in ONE call — the
    screen this route serves lists all 86+ of the customer's business
    processes at once, filterable by business cycle client-side, so a
    per-row fetch is not an optimisation opportunity here, it is a defect.

    Sorted by business cycle then name (bp.id breaks a tie between two
    processes that share both), because that is the order Carol works
    through her register.

    A business process that has never been screened comes back with
    dpia_required/decided_by/decided_at all None — never omitted from the
    list, never a 404 — same "never screened is not an error" contract
    GET /{business_process_id} and its /history sibling already keep.
    """
    rows = db.execute(_LIST_SCREENING_STATUS_SQL).all()
    return ScreeningListResponse(
        processes=[_screening_status_response(row) for row in rows]
    )


@privacycare_screening_router.get(
    "/triggers",
    dependencies=[Security(verify_oauth_client, scopes=[PRIVACYCARE_SCREENING_READ])],
    response_model=TriggerListResponse,
)
def list_screening_triggers(*, db: Session = Depends(get_db)) -> TriggerListResponse:
    """The screener's questions, ordered by display_order (gate.
    list_triggers' own ordering). Carol's own six rows are seeded
    permanently as of Task 5 of the plan that shipped this route."""
    rows = list_triggers(db)
    return TriggerListResponse(triggers=[TriggerResponse(**row) for row in rows])


@privacycare_screening_router.get(
    "/{business_process_id}",
    dependencies=[Security(verify_oauth_client, scopes=[PRIVACYCARE_SCREENING_READ])],
    response_model=CurrentScreeningResponse,
)
def get_current_screening_verdict(
    business_process_id: str,
    *,
    db: Session = Depends(get_db),
) -> CurrentScreeningResponse:
    """The newest screening decision for this business process (gate.
    current_verdict), or verdict=None when it has never been screened at
    all.

    404 for an unknown business_process_id; 200 with verdict=None for a
    real business process that simply has not been screened yet — see
    this module's docstring for why the two must not read the same.
    """
    _require_business_process(db, business_process_id)
    verdict = current_verdict(db, business_process_id)
    return CurrentScreeningResponse(
        business_process_id=business_process_id,
        verdict=_response_from_verdict(verdict) if verdict is not None else None,
    )


@privacycare_screening_router.get(
    "/{business_process_id}/history",
    dependencies=[Security(verify_oauth_client, scopes=[PRIVACYCARE_SCREENING_READ])],
    response_model=ScreeningHistoryResponse,
)
def get_screening_history(
    business_process_id: str,
    *,
    db: Session = Depends(get_db),
) -> ScreeningHistoryResponse:
    """Every screening decision ever recorded for this business process,
    newest first (gate.decision_history's own ordering). 404 for an
    unknown business_process_id; 200 with decisions=[] for a real business
    process that has never been screened — same distinction as the
    current-verdict route above, and this route carries none of the
    two-different-Nones ambiguity that route has (an empty list already
    means exactly one thing)."""
    _require_business_process(db, business_process_id)
    history = decision_history(db, business_process_id)
    return ScreeningHistoryResponse(
        business_process_id=business_process_id,
        decisions=[_response_from_verdict(v) for v in history],
    )


@privacycare_screening_router.post(
    "/{business_process_id}",
    dependencies=[Security(verify_oauth_client, scopes=[PRIVACYCARE_SCREENING_CREATE])],
    response_model=ScreeningVerdictResponse,
    status_code=status_codes.HTTP_201_CREATED,
)
def record_screening_decision(
    business_process_id: str,
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

    gate.record_decision already runs its own business_process_id
    existence check — after its own trigger-key and justification checks
    — and raises ValueError("no such business process: ...") for it; this
    route does NOT duplicate that check ahead of the call (unlike the two
    GET routes above, which have no core check to defer to). See this
    module's docstring for why: "no such business process" is mapped to
    404 here, the same way api/risk.py's add_assessment_risk maps
    register.add_risk's own "no such assessment" ValueError, and anything
    else (an unknown trigger key, a missing or blank screen-out
    justification, a justification supplied on a screen-in) becomes 400 —
    the business_process_id path segment names a resource that either
    does or doesn't exist; every other rejection is
    otherwise-valid-resource, bad input.

    ScreeningDecisionRequest carries no dpia_required field at all (see
    its own docstring in screening_schemas.py) — gate.record_decision
    derives it from triggered_keys and does not accept it as an argument,
    so nothing this route does could let a caller override that even if
    it wanted to.
    """
    decided_by = _created_by_from_client(client)
    try:
        verdict = record_decision(
            db,
            business_process_id=business_process_id,
            triggered_keys=request.triggered_keys,
            justification=request.justification,
            decided_by=decided_by,
        )
    except ValueError as exc:
        db.rollback()
        detail = str(exc)
        status_code = (
            status_codes.HTTP_404_NOT_FOUND
            if detail.startswith("no such business process")
            else status_codes.HTTP_400_BAD_REQUEST
        )
        raise HTTPException(status_code=status_code, detail=detail) from exc
    db.commit()
    return _response_from_verdict(verdict)


# fix round 2, item I-3 (SECURITY). This route writes ctl_systems and
# privacydeclaration — an Ethyca table, the customer's own Fides system
# inventory and processing activities — but was authorised behind
# PRIVACYCARE_SCREENING_CREATE alone. grounds.py deliberately does the
# opposite for a write to the SAME privacycare_declaration_ground table
# (see its own verify_oauth_client_for_declaration_system): it also
# requires Fides' own system-write authorisation — global SYSTEM_UPDATE,
# OR system-manager rights on the specific system, via has_system_
# permissions. Left as PRIVACYCARE_SCREENING_CREATE alone, an M2M client
# minted with only that one scope could create systems and processing
# activities in the customer's Fides estate through this route, which no
# Ethyca endpoint would let it do. Role-based callers are unaffected —
# Owner and Contributor already hold both scopes — so requiring this
# costs real users nothing.
#
# _system_for_business_process_mapping mirrors grounds.py's own
# _system_for_declaration exactly, keyed to business_process_id (this
# route's own path parameter) instead of declaration_id, and resolved
# through mapping.existing_system_id_for_process — the SAME query
# mapping.py's own _system_id_for_process reads, so this dependency and
# the write path it is gating can never disagree about which system a
# call is about to touch. A process with NO existing system yet (84 of
# 86 today) resolves system=None, which has_system_permissions reads as
# "not a manager of it" — so provisioning a BRAND NEW system requires the
# GLOBAL scope, never a per-system grant that could not possibly have been
# made for a system that does not exist yet. Same "unknown/absent ->
# system=None -> global scope only" order grounds.py's own docstring
# already documents and accepts for the identical situation.
def _system_for_business_process_mapping(
    business_process_id: str, db: Session = Depends(get_db)
) -> SystemAuthContainer:
    system_id = existing_system_id_for_process(db, business_process_id)
    system = (
        db.query(System).filter(System.id == system_id).first()
        if system_id is not None
        else None
    )
    return SystemAuthContainer(original_data=business_process_id, system=system)


async def verify_oauth_client_for_business_process_mapping(
    security_scopes: SecurityScopes,
    authorization: str = Security(oauth2_scheme),
    db: Session = Depends(get_db),
    system_auth_data: SystemAuthContainer = Depends(_system_for_business_process_mapping),
    permission_checker: PermissionCheckerCallback = Depends(get_permission_checker),
) -> ClientDetail:
    """Authorise this write the way Fides authorises its own system writes
    — see the module-level comment just above for why. Reuses has_system_
    permissions (Fides' own helper) rather than restating its rules, same
    reasoning grounds.py's identically-shaped dependency gives."""
    permission_checker = _resolve_depends(permission_checker, get_permission_checker)
    has_system_permissions(
        system_auth_data=system_auth_data,
        authorization=authorization,
        security_scopes=security_scopes,
        db=db,
        permission_checker=permission_checker,
    )
    _, client = extract_token_and_load_client(authorization, db)
    return client


@privacycare_screening_router.post(
    "/{business_process_id}/mapping",
    dependencies=[
        Security(verify_oauth_client, scopes=[PRIVACYCARE_SCREENING_CREATE]),
        Security(verify_oauth_client_for_business_process_mapping, scopes=[SYSTEM_UPDATE]),
    ],
    response_model=DataMappingResponse,
    status_code=status_codes.HTTP_201_CREATED,
)
def save_data_mapping(
    business_process_id: str,
    request: DataMappingRequest,
    *,
    db: Session = Depends(get_db),
    client: ClientDetail = Security(
        verify_oauth_client, scopes=[PRIVACYCARE_SCREENING_CREATE]
    ),
) -> DataMappingResponse:
    """Captures the data mapping behind an applicable business process
    (screening/mapping.save_mapping) — the route that gets 85 of her 86
    processes their first ever mapping. Creates a `privacydeclaration` and
    links it to the process, or updates the ONE activity this route already
    created for it (see mapping.py's own module docstring for why
    idempotency is keyed to the activity, never to the process).

    Requires BOTH PRIVACYCARE_SCREENING_CREATE (this whole surface's one
    write scope) AND Fides' own system-write authorisation (fix round 2,
    item I-3 — see the two dependency functions just above this route).
    Role-based callers are unaffected: Owner and Contributor already hold
    both PRIVACYCARE_SCREENING_CREATE and SYSTEM_UPDATE.

    Same ValueError-to-status-code split as record_screening_decision just
    above: save_mapping's own existence check raises "no such business
    process: ..." for an unknown id, mapped to 404 here; every other
    rejection (an unknown data subject, category or ground; a ground with
    no determined legal basis; a missing name or empty data_categories) is
    otherwise-valid-resource, bad input, and becomes 400.

    `client` is back (fix round 1, item 2) purely to name WHO recorded a
    ground's provenance — `_created_by_from_client` feeds
    save_mapping's `recorded_by`, written to `privacycare_declaration_
    ground.recorded_by` exactly as grounds.py's own route already does for
    the same column.
    """
    recorded_by = _created_by_from_client(client)
    try:
        result = save_mapping(
            db,
            business_process_id=business_process_id,
            name=request.name,
            data_categories=request.data_categories,
            recorded_by=recorded_by,
            data_subjects=request.data_subjects,
            ground=request.ground,
            purpose=request.purpose,
            retention_period=request.retention_period,
            third_parties=request.third_parties,
        )
    except ValueError as exc:
        db.rollback()
        detail = str(exc)
        status_code = (
            status_codes.HTTP_404_NOT_FOUND
            if detail.startswith("no such business process")
            else status_codes.HTTP_400_BAD_REQUEST
        )
        raise HTTPException(status_code=status_code, detail=detail) from exc
    except LookupError as exc:
        # fix round 2, item M-6: defense in depth. save_mapping only ever
        # reaches grounds.py's _record_declaration_ground after validating
        # the SAME declaration_id/ground_id itself, so this branch is
        # unreachable by construction today — but if it ever did fire
        # (only conceivable under a genuine invariant break, e.g. a row
        # vanishing mid-transaction), it must roll back and return a real
        # HTTPException rather than an unhandled 500 with no rollback on
        # the way out, same as every other failure path on this route.
        db.rollback()
        raise HTTPException(
            status_code=status_codes.HTTP_404_NOT_FOUND,
            detail=f"ground provenance could not be recorded: no such {exc}",
        ) from exc
    except PermissionError as exc:
        # Same defense-in-depth reasoning as the LookupError branch above —
        # save_mapping writes legal_basis_for_processing from this SAME
        # ground immediately before calling _record_declaration_ground, so
        # its own consistency check passes by construction; this exists
        # only so an invariant break surfaces as a real HTTPException, not
        # a bare 500.
        db.rollback()
        raise HTTPException(
            status_code=status_codes.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "the derived legal basis does not agree with the ground's "
                f"class ({exc.args[0]!r}); this should not happen — please "
                "retry the mapping"
            ),
        ) from exc
    db.commit()
    return _response_from_mapping(result)
