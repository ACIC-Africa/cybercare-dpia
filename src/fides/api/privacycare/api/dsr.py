"""The DSR register's HTTP surface (spec D-DSR-1, Barbara's 2026-09-15
ruling: PrivacyCare owns the register, Fides executes).

NAMESPACE. These routes live under our own `/api/v1/privacycare/
dsr-requests`, NOT under `/api/v1/plus`. The assessment routes squat
Ethyca's `plus` namespace because the shipped admin UI calls those exact
paths and the UI's path is the requirement. Nothing in the shipped UI calls
THESE — the register is new, Kenyan-specific ground with no Plus analogue —
so taking a path in Plus's namespace would only risk colliding with a real
Plus endpoint later. Same reasoning api/processes.py records for the
business-process routes; see that module's docstring for the fuller version.

FIVE ROUTES, NO MORE. Create, read one, list (filterable), record a
decision, record a notification. Deadline alerting and the outcome log
(spec W5.4/W5.5) need a scheduler and Teams/WhatsApp channels and are
explicitly out of this plan; a bespoke intake UI is out too — the Privacy
Center is the intake, this surface is the register behind it.

THE CORE VALIDATES, THIS MODULE MAPS. Every rule about what a right, an
outcome, or blank grounds means lives in dsr/register.py and
dsr/delegation.py, raised as ValueError. Every route below catches exactly
that and turns it into an HTTP status the caller can act on — nothing here
re-implements a check the core already makes.
"""
import math
from datetime import datetime, timezone
from typing import Optional

from fastapi import Depends, HTTPException, Security
from fastapi import status as status_codes
from fastapi_pagination import Page, Params, paginate
from loguru import logger
from sqlalchemy.orm import Session

from fides.api.deps import get_db
from fides.api.models.client import ClientDetail
from fides.api.oauth.utils import verify_oauth_client
from fides.api.privacycare.api.dsr_schemas import (
    DsrDecisionRequest,
    DsrNotificationRequest,
    DsrRequestCreate,
    DsrRequestResponse,
)
from fides.api.privacycare.api.identity import _created_by_from_client
from fides.api.privacycare.api.router import privacycare_dsr_router
from fides.api.privacycare.dsr.delegation import delegate
from fides.api.privacycare.dsr.register import (
    get_request,
    list_requests,
    record_decision,
    record_notification,
    record_request,
)
from fides.common.scope_registry import PRIVACYCARE_DSR_READ, PRIVACYCARE_DSR_UPDATE


def _owner_source(owner_email: Optional[str]) -> str:
    """A coarse, always-honest read of the CURRENT owner_email column —
    "explicit" (someone is on record) or "unassigned" (nobody is) — not a
    replay of D-DSR-8's four-step chain (explicit -> business process ->
    configured DPO -> unassigned) that register.resolve_owner walks at
    creation time.

    That chain cannot be replayed honestly after the fact:
    privacycare_dsr_request stores only the resolved owner_email, never the
    business_process_id that was passed in, and PRIVACYCARE_DPO_EMAIL is
    read from the environment at call time rather than stored at all. A
    version of this that called resolve_owner() again with the stored email
    as `explicit` would always answer "explicit" — even for a row the DPO
    fallback actually produced — and a version that re-read the DPO env var
    would drift if that var changed after the row was written. Reporting
    the coarser, always-true fact is more honest than reporting a specific
    wrong one.
    """
    return "explicit" if owner_email else "unassigned"


def _days_left(deadline_at: Optional[datetime]) -> Optional[int]:
    """None when the right is unclocked (objection, OQ-PRIVACY-02) — never
    0, which would misread as "due today" rather than "no deadline exists
    at all". Rounded UP (ceil), not truncated: a deadline six hours from now
    is still meaningfully "1 day left" to the person reading this, not "0
    days left" the way naive `timedelta.days` truncation would report it.
    """
    if deadline_at is None:
        return None
    aware_deadline = (
        deadline_at
        if deadline_at.tzinfo is not None
        else deadline_at.replace(tzinfo=timezone.utc)
    )
    remaining = aware_deadline - datetime.now(timezone.utc)
    return math.ceil(remaining.total_seconds() / 86400)


def _response_from_row(row: dict) -> DsrRequestResponse:
    return DsrRequestResponse(
        id=row["id"],
        right=row["right"],
        subject_identifier=row["subject_identifier"],
        received_at=row["received_at"],
        deadline_at=row["deadline_at"],
        owner_email=row["owner_email"],
        owner_source=_owner_source(row["owner_email"]),
        status=row["status"],
        outcome=row["outcome"],
        outcome_grounds=row["outcome_grounds"],
        decided_by=row["decided_by"],
        decided_at=row["decided_at"],
        subject_notified_at=row["subject_notified_at"],
        fides_privacy_request_id=row["fides_privacy_request_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        days_left=_days_left(row["deadline_at"]),
    )


@privacycare_dsr_router.post(
    "",
    dependencies=[Security(verify_oauth_client, scopes=[PRIVACYCARE_DSR_UPDATE])],
    response_model=DsrRequestResponse,
    status_code=status_codes.HTTP_201_CREATED,
)
def create_dsr_request(
    request: DsrRequestCreate,
    *,
    db: Session = Depends(get_db),
    client: ClientDetail = Security(
        verify_oauth_client, scopes=[PRIVACYCARE_DSR_UPDATE]
    ),
) -> DsrRequestResponse:
    """Record a Kenyan DSR obligation and, in the same call, delegate it to
    Fides for the three rights that move data. One call rather than two —
    record then a separate delegate — so a caller can never create a
    register row that silently never gets a Fides privacyrequest because a
    second call was forgotten.

    record_request rejects an unrecognised right (or an unseeded database);
    delegate rejects a delegating right whose Fides policy was never
    provisioned (ensure_kenyan_policies not yet run). Both raise ValueError,
    both map to the same 400 here, carrying the core's own message — the
    core decided what's wrong, this route only reports it.
    """
    created_by = _created_by_from_client(client)
    try:
        request_id = record_request(
            db,
            right=request.right,
            subject_identifier=request.subject_identifier,
            owner_email=request.owner_email,
            business_process_id=request.business_process_id,
        )
        delegate(db, request_id=request_id)
    except ValueError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status_codes.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    db.commit()
    logger.info(
        "PrivacyCare DSR request {} ({}) recorded by {}",
        request_id,
        request.right,
        created_by,
    )
    return _response_from_row(get_request(db, request_id))


@privacycare_dsr_router.get(
    "",
    dependencies=[Security(verify_oauth_client, scopes=[PRIVACYCARE_DSR_READ])],
    response_model=Page[DsrRequestResponse],
)
def list_dsr_requests(
    right: Optional[str] = None,
    status: Optional[str] = None,
    *,
    params: Params = Depends(),
    db: Session = Depends(get_db),
    client: ClientDetail = Security(
        verify_oauth_client, scopes=[PRIVACYCARE_DSR_READ]
    ),
) -> Page[DsrRequestResponse]:
    """Both filters are optional and additive — an unfiltered call returns
    the whole register. See register.list_requests for the ordering
    (oldest first)."""
    rows = list_requests(db, right=right, status=status)
    return paginate([_response_from_row(row) for row in rows], params)


@privacycare_dsr_router.get(
    "/{request_id}",
    dependencies=[Security(verify_oauth_client, scopes=[PRIVACYCARE_DSR_READ])],
    response_model=DsrRequestResponse,
)
def get_dsr_request(
    request_id: str,
    *,
    db: Session = Depends(get_db),
    client: ClientDetail = Security(
        verify_oauth_client, scopes=[PRIVACYCARE_DSR_READ]
    ),
) -> DsrRequestResponse:
    try:
        row = get_request(db, request_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status_codes.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    return _response_from_row(row)


@privacycare_dsr_router.post(
    "/{request_id}/decision",
    dependencies=[Security(verify_oauth_client, scopes=[PRIVACYCARE_DSR_UPDATE])],
    response_model=DsrRequestResponse,
)
def record_dsr_decision(
    request_id: str,
    request: DsrDecisionRequest,
    *,
    db: Session = Depends(get_db),
    client: ClientDetail = Security(
        verify_oauth_client, scopes=[PRIVACYCARE_DSR_UPDATE]
    ),
) -> DsrRequestResponse:
    """Records the outcome that closes a request.

    Controller ruling (carried from task 2's review): record_decision itself
    has no guard against re-deciding an already-closed request — it will
    UPDATE a second time without complaint. Silently overwriting a recorded
    regulatory decision is a defect, not a convenience, so this route checks
    status BEFORE calling the core and answers 409 Conflict rather than
    accepting a second, different outcome as if the first had never
    happened. The original decision is left exactly as it was.
    """
    try:
        row = get_request(db, request_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status_codes.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    if row["status"] == "closed":
        raise HTTPException(
            status_code=status_codes.HTTP_409_CONFLICT,
            detail=(
                f"request {request_id} is already closed with outcome "
                f"{row['outcome']!r}; refusing to overwrite it with a second "
                "decision"
            ),
        )

    decided_by = _created_by_from_client(client)
    try:
        record_decision(
            db,
            request_id=request_id,
            outcome=request.outcome,
            grounds=request.grounds,
            decided_by=decided_by,
        )
    except ValueError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status_codes.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    db.commit()
    logger.info(
        "PrivacyCare DSR request {} decided ({}) by {}",
        request_id,
        request.outcome,
        decided_by,
    )
    return _response_from_row(get_request(db, request_id))


@privacycare_dsr_router.post(
    "/{request_id}/notification",
    dependencies=[Security(verify_oauth_client, scopes=[PRIVACYCARE_DSR_UPDATE])],
    response_model=DsrRequestResponse,
)
def record_dsr_notification(
    request_id: str,
    request: DsrNotificationRequest,
    *,
    db: Session = Depends(get_db),
    client: ClientDetail = Security(
        verify_oauth_client, scopes=[PRIVACYCARE_DSR_UPDATE]
    ),
) -> DsrRequestResponse:
    """Records when the subject was told the outcome — a separate event
    from the decision itself (see register.record_notification), on its
    own clock.

    Same controller ruling as record_dsr_decision above, applied to the
    other half of the pair it named: record_notification has no guard
    against overwriting an existing subject_notified_at, so this route
    checks for one first and answers 409 Conflict rather than silently
    replacing a recorded notification date with a new one.
    """
    try:
        row = get_request(db, request_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status_codes.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    if row["subject_notified_at"] is not None:
        raise HTTPException(
            status_code=status_codes.HTTP_409_CONFLICT,
            detail=(
                f"request {request_id} was already notified at "
                f"{row['subject_notified_at']}; refusing to overwrite it"
            ),
        )

    notified_at = request.notified_at or datetime.now(timezone.utc)
    notified_by = _created_by_from_client(client)
    try:
        record_notification(db, request_id=request_id, notified_at=notified_at)
    except ValueError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status_codes.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    db.commit()
    logger.info(
        "PrivacyCare DSR request {} notification recorded by {}",
        request_id,
        notified_by,
    )
    return _response_from_row(get_request(db, request_id))
