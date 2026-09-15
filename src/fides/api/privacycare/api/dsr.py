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
from fides.api.privacycare.dsr.delegation import delegate, fides_request_statuses
from fides.api.privacycare.dsr.register import (
    discard_request,
    get_request,
    list_requests,
    record_decision,
    record_notification,
    record_request,
)
from fides.common.scope_registry import PRIVACYCARE_DSR_READ, PRIVACYCARE_DSR_UPDATE

# Sentinel for fides_privacy_request_status (I5): never a real
# PrivacyRequestStatus value, so a caller can tell "we looked, and there is
# nothing there any more" apart from an ordinary in-progress status.
_VANISHED = "vanished"


def _days_left(deadline_at: Optional[datetime]) -> Optional[int]:
    """None when the right is unclocked (objection, OQ-PRIVACY-02) — never
    0 for either "due today" or "just breached"; those are different facts
    and must read differently.

    A deadline still in the future is rounded UP (ceil): six hours from now
    is still meaningfully "1 day left", not "0 days left" the way naive
    `timedelta.days` truncation would report it. A deadline already passed
    is rounded DOWN (floor) instead, so it always comes back negative —
    `ceil` alone would round anything between "just now" and "23h59m ago"
    UP to 0, which misreads as "due today" exactly the confusion this
    function's docstring exists to avoid (Fides' own PrivacyRequest.
    days_left does whole-date subtraction for the same reason and goes
    negative once due_date has passed).
    """
    if deadline_at is None:
        return None
    aware_deadline = (
        deadline_at
        if deadline_at.tzinfo is not None
        else deadline_at.replace(tzinfo=timezone.utc)
    )
    remaining_days = (
        aware_deadline - datetime.now(timezone.utc)
    ).total_seconds() / 86400
    if remaining_days >= 0:
        return math.ceil(remaining_days)
    return math.floor(remaining_days)


def _fides_privacy_request_status(
    fides_privacy_request_id: Optional[str], statuses: dict
) -> Optional[str]:
    """I5 (narrowed, per ruling): report the delegated request's status out
    of an already-fetched `id -> status` map (`fides_request_statuses`,
    dsr/delegation.py) rather than querying per call. None when the right
    never delegated at all (id is None). `_VANISHED` — never a real status
    string — when an id IS stored but `statuses` has nothing for it (the
    `privacyrequest` row no longer exists), so the caller never mistakes a
    dead id for a live one.

    R2 (task 4, final review of plan 14): this used to call
    `PrivacyRequest.get_by` itself, one full-entity load per row — the same
    N+1 `fides_request_statuses`'s own docstring names as the reason it
    exists (`_filtered_final_upload` / `access_result_urls` are
    multi-megabyte columns). Every caller below now fetches the map once
    (one row for a single-request response, one page's worth for the list
    route) and passes it in here instead of a db handle.
    """
    if fides_privacy_request_id is None:
        return None
    return statuses.get(fides_privacy_request_id, _VANISHED)


def _response_from_row(row: dict, statuses: dict) -> DsrRequestResponse:
    return DsrRequestResponse(
        id=row["id"],
        right=row["right"],
        subject_identifier=row["subject_identifier"],
        received_at=row["received_at"],
        deadline_at=row["deadline_at"],
        owner_email=row["owner_email"],
        # Stored by record_request (register.py), not inferred here — see
        # DsrRequestResponse's own docstring for why a read-time guess was
        # a defect (fix round 1 on task 4) rather than a simplification.
        owner_source=row["owner_source"],
        business_process_id=row["business_process_id"],
        status=row["status"],
        outcome=row["outcome"],
        outcome_grounds=row["outcome_grounds"],
        decided_by=row["decided_by"],
        decided_at=row["decided_at"],
        subject_notified_at=row["subject_notified_at"],
        fides_privacy_request_id=row["fides_privacy_request_id"],
        fides_privacy_request_status=_fides_privacy_request_status(
            row["fides_privacy_request_id"], statuses
        ),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        days_left=_days_left(row["deadline_at"]),
    )


def _response_for_single_row(db: Session, row: dict) -> DsrRequestResponse:
    """The single-row endpoints' path into `_response_from_row`: one
    `fides_request_statuses` call for that row's own id (or none at all
    when it never delegated — see that function's own empty-set short
    circuit)."""
    statuses = fides_request_statuses(db, [row["fides_privacy_request_id"]])
    return _response_from_row(row, statuses)


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
    provisioned (ensure_kenyan_policies not yet run) or whose provisioned
    policy has drifted from the current Kenyan clock (D-DSR-7, I2). Both
    raise ValueError before delegate() ever reaches Fides' own
    PrivacyRequest.create, so for both, db.rollback() still has
    record_request's insert to undo and a register row never survives.

    R1 (task 4, final review of plan 14): those two ValueErrors are not the
    only way delegate() can fail. Once it reaches PrivacyRequest.create,
    Fides' own persist_obj (add/commit/refresh) commits unconditionally —
    a real commit, not ours to skip — which makes record_request's
    still-pending insert durable right along with it. A later failure in
    that same delegate() call (persist_identity, persist_masking_secrets)
    therefore has nothing left for db.rollback() to undo: the register row
    is already on disk. This route closes that gap itself rather than
    leaving it open — any exception from delegate() other than the two
    ValueErrors above triggers a compensating discard_request() of the row
    record_request just inserted, committed explicitly (rollback alone
    cannot undo what Fides has already committed), before the original
    exception is re-raised. So: a register row still never survives a
    failed delegation for every reachable delegate() failure — but two
    narrower things remain true and are NOT claimed here: (1) the
    Fides-side privacyrequest this route created (and its identity/masking
    rows) is not cleaned up by that compensation; it is left as Fides' own
    orphaned, unqueued, pending-approval request (see delegate()'s own
    docstring on why a created-but-unapproved PrivacyRequest is otherwise a
    normal resting state), and (2) fix round 1, Finding 3: if the
    compensation ITSELF fails (discard_request or the commit after it
    raising, e.g. on a dropped connection) the register row is left in
    place too — that failure is only logged, never raised, so the client
    still sees delegate()'s original error rather than the cleanup's, but
    a future reader must check the logs rather than assume a compensation
    failure would surface any other way.

    I4: `received_at` is validated here, not in the core — a future date
    would grant the controller more time than the statute allows, so it is
    rejected with 400 before record_request ever sees it. Naive datetimes
    (no tzinfo) are treated as UTC for the comparison, matching how they
    are stored.
    """
    if request.received_at is not None:
        aware_received_at = (
            request.received_at
            if request.received_at.tzinfo is not None
            else request.received_at.replace(tzinfo=timezone.utc)
        )
        if aware_received_at > datetime.now(timezone.utc):
            raise HTTPException(
                status_code=status_codes.HTTP_400_BAD_REQUEST,
                detail=(
                    f"received_at ({request.received_at.isoformat()}) is in "
                    "the future — a DSR cannot have been received before it "
                    "was reported"
                ),
            )

    created_by = _created_by_from_client(client)
    request_id: Optional[str] = None
    try:
        request_id = record_request(
            db,
            right=request.right,
            subject_identifier=request.subject_identifier,
            owner_email=request.owner_email,
            business_process_id=request.business_process_id,
            received_at=request.received_at,
        )
        delegate(db, request_id=request_id)
    except ValueError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status_codes.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    except Exception as delegate_exc:
        # R1: delegate() failed past the point where Fides' own commit may
        # already have made record_request's insert durable — db.rollback()
        # alone has nothing left to undo THAT insert (it lives in a prior,
        # already-closed transaction). But this exception may itself be a
        # database-level error (not our probe's plain RuntimeError), which
        # leaves the CURRENT transaction aborted — any further statement,
        # including our own compensating DELETE, would be rejected until
        # that is cleared. Rolling back first is always safe here: it only
        # ever discards whatever this (still-open, separate) transaction
        # was in the middle of, never the prior commit. Compensate
        # explicitly so the register row still never survives a failed
        # delegation, then let the original exception continue (this route
        # makes no claim about what kind of failure it was, only that the
        # register stays clean).
        #
        # Fix round 1 (Finding 3): the compensation itself can fail — a
        # dropped connection during discard_request or the commit after it
        # — and an unguarded failure there would propagate IN PLACE of
        # delegate_exc, silently changing what the client sees (and, per
        # Finding 2, leaving the register row behind with no visible sign
        # beyond the logs). The client must always see the real cause of
        # the 500/exception it gets, so cleanup failures are caught,
        # logged, and never allowed to replace delegate_exc.
        if request_id is not None:
            try:
                db.rollback()
                discard_request(db, request_id)
                db.commit()
            except Exception:
                logger.exception(
                    "PrivacyCare DSR request {} — compensating "
                    "discard_request failed after a failed delegation; the "
                    "register row may still exist",
                    request_id,
                )
        raise delegate_exc
    db.commit()
    logger.info(
        "PrivacyCare DSR request {} ({}) recorded by {}",
        request_id,
        request.right,
        created_by,
    )
    return _response_for_single_row(db, get_request(db, request_id))


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
    (oldest first).

    R2 (task 4, final review of plan 14): fides_privacy_request_status used
    to be resolved per row via a full-entity `PrivacyRequest.get_by` — an
    N+1 loading the multi-megabyte columns `query_without_large_columns`
    exists to keep out of exactly this kind of list view. One
    `fides_request_statuses` call now covers every row in this response,
    regardless of how many there are.
    """
    rows = list_requests(db, right=right, status=status)
    statuses = fides_request_statuses(
        db, [row["fides_privacy_request_id"] for row in rows]
    )
    return paginate(
        [_response_from_row(row, statuses) for row in rows], params
    )


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
    return _response_for_single_row(db, row)


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
    return _response_for_single_row(db, get_request(db, request_id))


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

    I6 (final review, guard only): record_notification also had no guard
    against recording a notification before any decision existed at all.
    subject_notified_at means "when the subject was told the outcome" — a
    notification with no outcome is a false regulatory record, not merely
    a premature one, so this route requires status == "closed" first and
    answers 409 Conflict otherwise. An amend/correction route for fixing a
    wrong value is deliberately out of scope for this wave.
    """
    try:
        row = get_request(db, request_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status_codes.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    if row["status"] != "closed":
        raise HTTPException(
            status_code=status_codes.HTTP_409_CONFLICT,
            detail=(
                f"request {request_id} has not been decided yet (status "
                f"{row['status']!r}); refusing to record a notification "
                "with no decision behind it"
            ),
        )
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
    return _response_for_single_row(db, get_request(db, request_id))
