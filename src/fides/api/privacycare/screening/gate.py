# The screening gate's verdict (plan 18, Task 2). Before a DPIA is
# generated, someone answers whether the activity needs one at all — this
# module is that answer, recorded.
#
# Carol's rule is one sentence: any single trigger means a DPIA is
# required; none means the activity is screened out and needs a one-line
# justification. There is no weighting, no threshold, no scoring — a
# screening gate is not a risk assessment, and turning it into one would be
# inventing a rule the privacy SME did not give. Same discipline
# risk/register.py applies to Carol's seven risk categories and
# risk/odpc.py applies to the HIGH/CRITICAL escalation line: the rule is
# reproduced exactly, not "improved."
#
# All raw SQL, all bound parameters, never a commit — the caller's session
# boundary decides, same rule as dsr/register.py, risk/register.py, and
# taxonomy/loader.py.
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import sqlalchemy
from sqlalchemy.orm import Session

_LIST_TRIGGERS_SQL = sqlalchemy.text(
    "SELECT id, trigger_key, label, description, display_order "
    "FROM privacycare_screening_trigger ORDER BY display_order"
)

_VALID_TRIGGER_KEYS_SQL = sqlalchemy.text(
    "SELECT trigger_key FROM privacycare_screening_trigger"
)

# No ForeignKey from privacycare_screening_decision.declaration_id to
# privacydeclaration — see models.py's screening_decision_table comment:
# a constraint from our chain into Ethyca's is the coupling that breaks an
# upstream merge. Existence is checked here, at write time, instead.
_DECLARATION_EXISTS_SQL = sqlalchemy.text(
    "SELECT 1 FROM privacydeclaration WHERE id = :id"
)

_INSERT_DECISION_SQL = sqlalchemy.text(
    "INSERT INTO privacycare_screening_decision "
    "(id, declaration_id, dpia_required, triggered_keys, justification, decided_by) "
    "VALUES (:id, :declaration_id, :dpia_required, :triggered_keys, :justification, :decided_by) "
    "RETURNING decided_at"
)

_DECISION_COLUMNS = (
    "declaration_id, dpia_required, triggered_keys, justification, "
    "decided_by, decided_at"
)

# Newest first; ties in decided_at (two decisions written in the same
# transaction share a server_default now(), which is resolved once per
# transaction, not once per statement) break by id so that current_verdict
# and decision_history can never disagree between two calls, or return a
# different row depending on whatever order Postgres happens to hand tied
# rows back in. This is the same discipline risk/odpc.py's highest_risk
# documents for list_risks' own (-score, id) ordering — a plan-17 sibling
# shipped the equivalent tie-break here without a comment and it was
# flagged in review.
_SELECT_DECISIONS_SQL = sqlalchemy.text(
    f"SELECT {_DECISION_COLUMNS} FROM privacycare_screening_decision "
    "WHERE declaration_id = :declaration_id "
    "ORDER BY decided_at DESC, id DESC"
)


@dataclass(frozen=True)
class ScreeningVerdict:
    declaration_id: str
    dpia_required: bool
    triggered_keys: list[str]  # sorted; empty when not required
    justification: Optional[str]  # present exactly when not required
    decided_by: str
    decided_at: datetime


def _to_verdict(row) -> ScreeningVerdict:
    # triggered_keys comes back from the ARRAY column already sorted (see
    # record_decision: it is sorted before the INSERT), so no re-sort
    # happens here — this is a straight read of what was written, not a
    # second place that could apply a different rule.
    return ScreeningVerdict(
        declaration_id=row.declaration_id,
        dpia_required=row.dpia_required,
        triggered_keys=list(row.triggered_keys),
        justification=row.justification,
        decided_by=row.decided_by,
        decided_at=row.decided_at,
    )


def list_triggers(db: Session) -> list[dict]:
    """The screener's questions, ordered by display_order. Empty when
    nothing has been seeded yet (the live table is empty as of this task —
    seeding it for real is Task 5's job)."""
    rows = db.execute(_LIST_TRIGGERS_SQL).mappings().all()
    return [dict(row) for row in rows]


def record_decision(
    db: Session,
    *,
    declaration_id: str,
    triggered_keys: list[str],
    justification: Optional[str],
    decided_by: str,
) -> ScreeningVerdict:
    """Inserts a new screening decision. NEVER updates: re-screening
    appends, because an activity screened out last quarter may need a DPIA
    this one, and the earlier decision is the evidence of what was decided
    and why (models.py's screening_decision_table docstring: APPEND-ONLY).
    There is no update path here at all to accidentally call.

    dpia_required is DERIVED from whether triggered_keys is non-empty and
    is not, and cannot be, an argument to this function — a caller cannot
    tick three triggers and also declare no DPIA needed. This is the
    invariant models.py:~417 promises in its "empty exactly when
    dpia_required is false" comment; the table's own CheckConstraint
    (ck_screening_screenout_has_a_reason) covers only dpia_required vs.
    justification; it does NOT relate dpia_required to triggered_keys, so a
    hand-written INSERT could still violate that comment. This function is
    the one and only write path the application offers onto this table, and
    by never accepting dpia_required as input — only ever computing it from
    triggered_keys, right here — it is the thing that makes that comment
    true rather than aspirational.
    """
    # Dedup + sort up front: the stored array, and the verdict handed back,
    # both reflect ScreeningVerdict's "sorted" contract, and a duplicate
    # tick (e.g. the same trigger submitted twice by a sloppy form) collapses
    # to one entry rather than being treated as meaningfully different from
    # ticking it once.
    keys = sorted(set(triggered_keys))

    valid_keys = {row[0] for row in db.execute(_VALID_TRIGGER_KEYS_SQL).all()}
    unknown = sorted(k for k in keys if k not in valid_keys)
    if unknown:
        # Rejected by name, not silently dropped: a caller who believed
        # they ticked a trigger that turns out not to exist must be told
        # which one, not have it quietly vanish from the recorded decision.
        raise ValueError(
            f"unknown screening trigger key(s): {', '.join(unknown)}"
        )

    # THE derivation: dpia_required is a function of triggered_keys, full
    # stop. See this function's own docstring above for why nothing else
    # may set it.
    dpia_required = bool(keys)

    if dpia_required:
        if justification is not None:
            raise ValueError(
                "justification must not be given when a trigger requires a "
                "DPIA — the ticked triggers are the reason, and a "
                "screen-in does not need a second one"
            )
    else:
        if justification is None or not justification.strip():
            raise ValueError(
                "justification is required when no trigger is ticked — a "
                "screen-out needs a one-line reason, and this is the "
                "compliance artifact a regulator asks for"
            )

    exists = db.execute(_DECLARATION_EXISTS_SQL, {"id": declaration_id}).first()
    if exists is None:
        raise ValueError(f"no such declaration: {declaration_id!r}")

    result = db.execute(
        _INSERT_DECISION_SQL,
        {
            "id": str(uuid.uuid4()),
            "declaration_id": declaration_id,
            "dpia_required": dpia_required,
            "triggered_keys": keys,
            "justification": justification,
            "decided_by": decided_by,
        },
    ).first()

    return ScreeningVerdict(
        declaration_id=declaration_id,
        dpia_required=dpia_required,
        triggered_keys=keys,
        justification=justification,
        decided_by=decided_by,
        decided_at=result.decided_at,
    )


def decision_history(db: Session, declaration_id: str) -> list[ScreeningVerdict]:
    """Every screening decision ever recorded for this declaration, newest
    first (see _SELECT_DECISIONS_SQL's comment for the decided_at/id
    tie-break). Empty when the declaration has never been screened."""
    rows = db.execute(_SELECT_DECISIONS_SQL, {"declaration_id": declaration_id}).all()
    return [_to_verdict(row) for row in rows]


def current_verdict(db: Session, declaration_id: str) -> Optional[ScreeningVerdict]:
    """The newest screening decision for this declaration, or None when it
    has never been screened at all. Built on decision_history so the two
    can never disagree about which row is "current" or about the
    decided_at/id tie-break — there is exactly one ordering rule, defined
    once, in _SELECT_DECISIONS_SQL."""
    history = decision_history(db, declaration_id)
    return history[0] if history else None


def is_screened_out(db: Session, declaration_id: str) -> bool:
    """True only when the LATEST decision screened the activity out
    (dpia_required is False). False for a declaration that has never been
    screened at all — that is not an error and it is not "screened out":
    an unscreened activity has simply not been screened, and screening is
    opt-in, not assumed."""
    verdict = current_verdict(db, declaration_id)
    if verdict is None:
        return False
    return not verdict.dpia_required
