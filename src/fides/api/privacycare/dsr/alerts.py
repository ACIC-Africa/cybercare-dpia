# Which obligation is due an alert, and the ledger that stops it repeating.
# Nothing else lives here: no channel (Slack/email/whatever sends the
# message), no scheduler (what walks the register and calls this hourly),
# no HTTP. Those are later tasks in plan 15; this module only answers "is an
# alert due" and "has one already gone out".
#
# D-DSR-5: an alert that repeats gets the channel muted, and a muted channel
# is worse than no channel. The once-only guarantee is therefore a unique
# constraint on privacycare_dsr_alert (dsr_request_id, kind), not a flag on
# the request row — a flag loses to a second worker, a restart mid-run, or a
# retry. record_alert degrades a collision to "already sent" (returns
# False) rather than raising, so a concurrent second worker never crashes
# the run.
import uuid
from datetime import datetime, timedelta
from math import ceil
from typing import Optional

import psycopg2.errorcodes  # type: ignore[import-untyped]
import sqlalchemy
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

APPROACHING = "approaching"
BREACHED = "breached"
_VALID_KINDS = (APPROACHING, BREACHED)

# The one constraint record_alert is entitled to swallow. Matched on both
# the Postgres error code AND the constraint's own name (fix round 1,
# Finding 1): `IntegrityError` also covers a foreign-key violation
# (dsr_request_id pointing at nothing) or a not-null violation, and
# `record_alert`'s False return is documented to mean exactly one thing —
# "already sent". Returning it for "never sent, insert was simply invalid"
# is the worst available lie for this function: the caller records nothing,
# the (still-absent) unique row lets a retry through, and the ledger
# implicitly asserts an owner was told when they were not — precisely what
# the ledger exists to prevent. Everything that is not this exact
# constraint re-raises.
_ONCE_ONLY_CONSTRAINT = "uq_privacycare_dsr_alert"

_INSERT_ALERT_SQL = sqlalchemy.text(
    "INSERT INTO privacycare_dsr_alert "
    "(id, dsr_request_id, kind, channel, recipient) "
    "VALUES (:id, :dsr_request_id, :kind, :channel, :recipient)"
)

_ALERTS_SENT_SQL = sqlalchemy.text(
    "SELECT kind FROM privacycare_dsr_alert WHERE dsr_request_id = :dsr_request_id"
)


def warn_threshold(days_allowed: int) -> int:
    """D-AL-2: three days' notice on a 7-day statutory clock is a different
    urgency from three days on a 30-day one, so the warning window is a
    fraction of the clock, not a fixed number. ceil, not floor or round: a
    7-day clock's third is 2.33 days, and rounding that down to 2 would warn
    an owner a day later than the fraction actually means."""
    return ceil(days_allowed / 3)


def alert_due(
    *,
    deadline_at: Optional[datetime],
    days_allowed: Optional[int],
    now: datetime,
    already_sent: frozenset[str],
) -> Optional[str]:
    """Pure: takes `now` instead of reading the clock (every boundary is
    testable without freezing time) and `already_sent` instead of querying
    (no database in this function at all).

    deadline_at is None for an unclocked obligation (objection, until
    OQ-PRIVACY-02 is answered) — never "due today", just never due.

    A breach and the approaching warning are different events for an owner
    ("this is getting close" vs. "this is now late") and are tracked
    independently in `already_sent`: a breach is still due even after the
    warning already went out, but neither kind repeats once it has fired.
    """
    if deadline_at is None:
        return None

    remaining = deadline_at - now
    if remaining <= timedelta(0):
        return BREACHED if BREACHED not in already_sent else None

    if days_allowed is None:
        # Should not happen for a real row (deadline_at and days_allowed are
        # always computed together — see dsr/timelines.py deadline_for), but
        # this function trusts nothing it wasn't given: no threshold means
        # no warning to compare against.
        return None

    threshold = timedelta(days=warn_threshold(days_allowed))
    if remaining <= threshold:
        return APPROACHING if APPROACHING not in already_sent else None

    return None


def record_alert(
    db: Session,
    *,
    dsr_request_id: str,
    kind: str,
    channel: str,
    recipient: Optional[str],
) -> bool:
    """Writes one ledger row and returns True, or returns False if the
    (dsr_request_id, kind) pair was already recorded — never raises on that
    collision, so a second worker racing the same alert degrades to
    "already sent" instead of crashing the run. Never commits; the caller's
    session boundary decides.

    The insert runs inside its own SAVEPOINT (db.begin_nested()), not a bare
    db.rollback(): a bare rollback would discard the whole transaction,
    including whatever the caller already wrote earlier in the same run (or,
    under the test fixture, the seeded timelines and register rows the test
    depends on). Only the failed insert needs undoing.

    Only the once-only unique constraint is caught (see
    _ONCE_ONLY_CONSTRAINT above) — a foreign-key or not-null violation
    (e.g. a `dsr_request_id` that does not exist) is a caller bug, not a
    "already sent" state, and re-raises rather than returning a False that
    would misreport it.
    """
    if kind not in _VALID_KINDS:
        raise ValueError(
            f"{kind!r} is not a recognised alert kind: expected one of {_VALID_KINDS}"
        )

    try:
        with db.begin_nested():
            db.execute(
                _INSERT_ALERT_SQL,
                {
                    "id": str(uuid.uuid4()),
                    "dsr_request_id": dsr_request_id,
                    "kind": kind,
                    "channel": channel,
                    "recipient": recipient,
                },
            )
    except IntegrityError as exc:
        orig = exc.orig
        pgcode = getattr(orig, "pgcode", None)
        diag = getattr(orig, "diag", None)
        constraint_name = getattr(diag, "constraint_name", None)
        if (
            pgcode == psycopg2.errorcodes.UNIQUE_VIOLATION
            and constraint_name == _ONCE_ONLY_CONSTRAINT
        ):
            return False
        raise
    return True


def alerts_sent_for(db: Session, dsr_request_id: str) -> frozenset[str]:
    """The `already_sent` argument alert_due needs, read back from the
    ledger — the one place in this module that touches the database."""
    rows = db.execute(
        _ALERTS_SENT_SQL, {"dsr_request_id": dsr_request_id}
    ).scalars().all()
    return frozenset(rows)
