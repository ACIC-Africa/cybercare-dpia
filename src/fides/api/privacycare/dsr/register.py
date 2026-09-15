# The register itself: creating a Kenyan DSR obligation, resolving who owns
# it, recording the decision and the notification that closes it, and
# reading it back. No HTTP (task 4) and no Fides privacyrequest (task 3) —
# this module only ever touches privacycare_dsr_request /
# privacycare_dsr_timeline / privacycare_business_process, all raw SQL, all
# bound parameters, never a commit (the caller's session boundary decides,
# same rule as taxonomy/loader.py and dsr/timelines.py).
import os
import uuid
from datetime import datetime, timezone
from typing import NamedTuple, Optional

import sqlalchemy
from sqlalchemy.orm import Session

from fides.api.privacycare.dsr.timelines import deadline_for, timeline_days

_VALID_OUTCOMES = ("granted", "refused")


class OwnerResolution(NamedTuple):
    # A NamedTuple, not a dataclass: tests compare it directly to a 2-tuple,
    # and the two halves — who, and how we got there — are always read
    # together.
    owner_email: Optional[str]
    source: str  # "explicit" | "business_process" | "configured_dpo" | "unassigned"


_TIMELINE_ROW_EXISTS_SQL = sqlalchemy.text(
    'SELECT 1 FROM privacycare_dsr_timeline WHERE "right" = :right'
)

_BUSINESS_PROCESS_OWNER_SQL = sqlalchemy.text(
    "SELECT owner_email FROM privacycare_business_process WHERE id = :id"
)

_INSERT_REQUEST_SQL = sqlalchemy.text(
    "INSERT INTO privacycare_dsr_request "
    '(id, "right", subject_identifier, received_at, deadline_at, owner_email, '
    'owner_source, business_process_id) '
    "VALUES (:id, :right, :subject_identifier, :received_at, :deadline_at, "
    ":owner_email, :owner_source, :business_process_id)"
)

_DECIDE_SQL = sqlalchemy.text(
    "UPDATE privacycare_dsr_request "
    "SET outcome = :outcome, outcome_grounds = :grounds, decided_by = :decided_by, "
    "decided_at = :decided_at, status = 'closed' "
    "WHERE id = :id"
)

_NOTIFY_SQL = sqlalchemy.text(
    "UPDATE privacycare_dsr_request SET subject_notified_at = :notified_at WHERE id = :id"
)

_DISCARD_SQL = sqlalchemy.text("DELETE FROM privacycare_dsr_request WHERE id = :id")

_REQUEST_COLUMNS = (
    'id, "right", subject_identifier, received_at, deadline_at, owner_email, '
    "owner_source, business_process_id, "
    "status, outcome, outcome_grounds, decided_by, decided_at, subject_notified_at, "
    "fides_privacy_request_id, created_at, updated_at"
)

_GET_REQUEST_SQL = sqlalchemy.text(
    f"SELECT {_REQUEST_COLUMNS} FROM privacycare_dsr_request WHERE id = :id"
)


def resolve_owner(
    db: Session, *, explicit: Optional[str], business_process_id: Optional[str]
) -> OwnerResolution:
    """D-DSR-8's fallback chain: explicit -> the business process's owner ->
    the configured DPO -> unassigned. The DPO email is read from the
    environment on every call, not cached at import time, because a test (and
    an operator) must be able to change it between calls."""
    if explicit:
        return OwnerResolution(explicit, "explicit")

    if business_process_id:
        row = db.execute(
            _BUSINESS_PROCESS_OWNER_SQL, {"id": business_process_id}
        ).first()
        if row is not None and row[0]:
            return OwnerResolution(row[0], "business_process")

    dpo_email = os.environ.get("PRIVACYCARE_DPO_EMAIL")
    if dpo_email:
        return OwnerResolution(dpo_email, "configured_dpo")

    return OwnerResolution(None, "unassigned")


def record_request(
    db: Session,
    *,
    right: str,
    subject_identifier: str,
    owner_email: Optional[str] = None,
    business_process_id: Optional[str] = None,
    received_at: Optional[datetime] = None,
) -> str:
    """Looks the timeline up once, computes and STORES the deadline, resolves
    the owner, inserts, and returns the new id. Never commits.

    timeline_days(db, right) raises for an unrecognised right, but returns
    None for two different situations: objection's real "unclocked" state,
    and a right whose timeline row was never seeded at all (an unseeded
    database, not a policy choice). Conflating them would silently accept
    requests against a broken deployment. So when it comes back None we check
    the row's *presence* directly before trusting that as "unclocked".

    I4 (final review). `received_at` defaults to now, but a caller may pass
    the date the obligation actually arrived — paper/email intake is the
    Kenyan reality, and a request entered three days after it was received
    must not silently grant the controller three extra days by starting the
    clock at data-entry time instead. Rejecting a future date is the
    caller's job (api/dsr.py's route), not this core function's — this
    layer trusts what it is given and simply stops defaulting once
    something is.
    """
    days = timeline_days(db, right)
    if days is None:
        row_exists = db.execute(_TIMELINE_ROW_EXISTS_SQL, {"right": right}).first()
        if row_exists is None:
            raise ValueError(
                f"no timeline row for {right!r} — the database looks unseeded; "
                "run seed_timelines() before recording requests"
            )

    if received_at is None:
        received_at = datetime.now(timezone.utc)
    deadline_at = deadline_for(received_at, days)
    resolution = resolve_owner(
        db, explicit=owner_email, business_process_id=business_process_id
    )

    request_id = str(uuid.uuid4())
    db.execute(
        _INSERT_REQUEST_SQL,
        {
            "id": request_id,
            "right": right,
            "subject_identifier": subject_identifier,
            "received_at": received_at,
            "deadline_at": deadline_at,
            "owner_email": resolution.owner_email,
            # D-DSR-8's chain (explicit | business_process | configured_dpo
            # | unassigned) — resolve_owner already computed this above; a
            # response-layer reader must be able to report the real reason,
            # not re-infer a coarser guess from owner_email alone (fix round
            # 1 on task 4: the previous inferred version could report
            # "explicit" for a business_process- or configured_dpo-derived
            # owner, which is a false claim, not merely an imprecise one).
            "owner_source": resolution.source,
            # Persisted, not merely consulted-and-discarded (final review
            # minor finding): resolve_owner already reads this to resolve
            # the owner when owner_source="business_process", but the row
            # itself used to drop which process that was — half of an
            # auditable claim. NULL whenever no business_process_id was
            # given, regardless of which owner_source was resolved.
            "business_process_id": business_process_id,
        },
    )
    return request_id


def record_decision(
    db: Session, *, request_id: str, outcome: str, grounds: str, decided_by: str
) -> None:
    """Validates before writing anything: an unknown outcome or blank grounds
    must not leave a half-written decision. Closes the request — deciding and
    notifying the subject are separate events (see record_notification)."""
    if outcome not in _VALID_OUTCOMES:
        raise ValueError(
            f"{outcome!r} is not a valid outcome: expected one of {_VALID_OUTCOMES}"
        )
    if not grounds or not grounds.strip():
        raise ValueError(
            "grounds must not be blank — a regulator asks why, not whether"
        )

    result = db.execute(
        _DECIDE_SQL,
        {
            "outcome": outcome,
            "grounds": grounds,
            "decided_by": decided_by,
            "decided_at": datetime.now(timezone.utc),
            "id": request_id,
        },
    )
    if result.rowcount == 0:
        raise ValueError(f"no such request: {request_id!r}")


def record_notification(db: Session, *, request_id: str, notified_at: datetime) -> None:
    """A decision and telling the subject about it are different events with
    different timestamps — the clock the brief names for restriction's
    refusal is this one, not decided_at."""
    result = db.execute(
        _NOTIFY_SQL, {"notified_at": notified_at, "id": request_id}
    )
    if result.rowcount == 0:
        raise ValueError(f"no such request: {request_id!r}")


def discard_request(db: Session, request_id: str) -> None:
    """Compensating delete for `record_request`'s own insert (api/dsr.py's
    create route, R1). Exists because Fides' `PrivacyRequest.create` commits
    unconditionally: once `delegate()` reaches it, that commit can make
    `record_request`'s still-pending insert durable too, along with
    whatever it was itself in the middle of — at which point `db.rollback()`
    has nothing left to undo. This is the caller's explicit undo for that
    case. Never commits (same rule as every other function here) — the
    caller decides the session boundary, same as `record_request` above.
    Idempotent: deleting an id that no longer exists (or never did) is a
    silent no-op, not an error, since the caller will have hit this from an
    `except` block chasing an id it does not know the fate of."""
    db.execute(_DISCARD_SQL, {"id": request_id})


def get_request(db: Session, request_id: str) -> dict:
    row = db.execute(_GET_REQUEST_SQL, {"id": request_id}).mappings().first()
    if row is None:
        raise ValueError(f"no such request: {request_id!r}")
    return dict(row)


def list_requests(
    db: Session, *, right: Optional[str] = None, status: Optional[str] = None
) -> list:
    """Filters are optional and additive; an unfiltered call returns the
    whole register, oldest first."""
    clauses = []
    params: dict = {}
    if right is not None:
        clauses.append('"right" = :right')
        params["right"] = right
    if status is not None:
        clauses.append("status = :status")
        params["status"] = status
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

    sql = sqlalchemy.text(
        f"SELECT {_REQUEST_COLUMNS} FROM privacycare_dsr_request {where} "
        "ORDER BY created_at"
    )
    rows = db.execute(sql, params).mappings().all()
    return [dict(row) for row in rows]
