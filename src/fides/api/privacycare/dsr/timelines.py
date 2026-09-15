# Kenya's statutory clocks for the six DSR types, plus the arithmetic that
# turns "a request came in" into "here is the date it is due". Nothing here
# knows about HTTP or about Fides' privacyrequest — that's later tasks. This
# module answers one question only: how long does a controller have, per
# right, and what date does that number produce.
#
# Two of the six rights (restriction, objection) move no data at all, so
# Fides has no representation of them and no clock. PrivacyCare owns the
# clock for all six because the register is the point (spec D-DSR-1, Barbara
# 2026-09-15) — Fides is delegated only the data movement for the other four.
from datetime import datetime, timedelta
from typing import Optional

import sqlalchemy
from sqlalchemy.orm import Session

# Order matters nowhere downstream, but it is fixed here so callers (tests,
# seeders, future UI) never have to guess it — it mirrors the brief's own
# enumeration order (access, rectification, erasure, restriction,
# portability, objection).
KENYAN_RIGHTS: tuple[str, ...] = (
    "access",
    "rectification",
    "erasure",
    "restriction",
    "portability",
    "objection",
)

# right -> (days, source_note). objection's days is None on purpose: the
# source brief gives it no timeline, and inventing one would decide when a
# controller is in breach — that is the SME's call, not engineering's
# (OQ-PRIVACY-02, Carol). NULL is a reportable "unclocked" state, not a gap
# to fill in later.
_BRIEF_CITATION = (
    "01_brief_for_dpia.md § 'Statutory timelines as stated in the source'"
)
_TIMELINES: tuple[tuple[str, Optional[int], str], ...] = (
    ("access", 7, _BRIEF_CITATION),
    ("rectification", 14, _BRIEF_CITATION),
    ("erasure", 14, _BRIEF_CITATION),
    ("restriction", 14, _BRIEF_CITATION),
    ("portability", 30, _BRIEF_CITATION),
    (
        "objection",
        None,
        "not stated in the source; not inferred (OQ-PRIVACY-02, pending Carol)",
    ),
)

_SEED_SQL = sqlalchemy.text(
    'INSERT INTO privacycare_dsr_timeline ("right", days, source_note) '
    'VALUES (:right, :days, :source_note) '
    'ON CONFLICT ("right") DO NOTHING'
)

_SELECT_DAYS_SQL = sqlalchemy.text(
    'SELECT days FROM privacycare_dsr_timeline WHERE "right" = :right'
)


def seed_timelines(db: Session) -> int:
    """Idempotent: ON CONFLICT DO NOTHING means a rerun writes nothing and
    reports 0. Never commits — same rule as taxonomy/loader.py, the caller's
    session boundary decides when this becomes durable."""
    written = 0
    for right, days, source_note in _TIMELINES:
        result = db.execute(
            _SEED_SQL, {"right": right, "days": days, "source_note": source_note}
        )
        written += result.rowcount
    return written


def timeline_days(db: Session, right: str) -> Optional[int]:
    """None means "unclocked" (objection today), not "unknown". An
    unrecognised right is a caller bug, not a data state, so it raises rather
    than returning None indistinguishably from objection's real NULL."""
    if right not in KENYAN_RIGHTS:
        raise ValueError(
            f"{right!r} is not one of Kenya's six DSR rights: {KENYAN_RIGHTS}"
        )
    return db.execute(_SELECT_DAYS_SQL, {"right": right}).scalar()


def deadline_for(received_at: datetime, days: Optional[int]) -> Optional[datetime]:
    """The whole of the clock arithmetic: a due date is receipt plus the
    statutory count, or nothing at all when the right carries no count."""
    if days is None:
        return None
    return received_at + timedelta(days=days)
