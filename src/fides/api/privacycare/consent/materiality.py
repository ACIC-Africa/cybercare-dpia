# When does a notice change invalidate consent already given? Spec D-CON-2.
#
# Gaining a data use asks the subject to agree to something they never saw,
# so consent given before it is no longer informed. Losing a use narrows
# processing and cannot invalidate agreement already given. Wording changes
# are not material on their own — otherwise every typo fix would re-consent
# an entire customer base, and a rule that cries wolf gets switched off,
# which is worse than not having it.
#
# is_materially_different is deliberately PURE: no database, no clock. The
# database enters only through the rule NAME that gets asked for — the
# predicate's own logic is provisional and owned by Carol (OQ-CON-01), which
# is why the active rule lives in a configuration row (privacycare_consent_
# rule, models.py) rather than as a constant baked into this module, the
# same shape and for the same reason privacycare_dsr_timeline is a table.
from typing import Iterable

import sqlalchemy
from sqlalchemy.orm import Session

# The one rule this module knows how to evaluate today. A notice change is
# material exactly when it adds a data use the subject never saw.
RULE_GAINED_USE = "gained_data_use"

# The configuration table holds at most one row: which rule is active. A
# fixed row id (rather than models.py's usual random _uuid()) is what makes
# seed_consent_rule's ON CONFLICT DO NOTHING idempotent — a second seed call
# collides with this exact id instead of inserting a second, competing row.
_SEED_ROW_ID = "consent-materiality-rule"

_SOURCE_NOTE = (
    "Provisional default pending Carol's ruling on OQ-CON-01 (spec D-CON-2: "
    "what counts as a material notice change). Gaining a data use is treated "
    "as material; losing one, or a wording-only change, is not."
)

_SEED_SQL = sqlalchemy.text(
    "INSERT INTO privacycare_consent_rule (id, rule, source_note) "
    "VALUES (:id, :rule, :source_note) "
    "ON CONFLICT (id) DO NOTHING"
)

_SELECT_ACTIVE_RULE_SQL = sqlalchemy.text("SELECT rule FROM privacycare_consent_rule")


def added_uses(earlier: list[str], later: list[str]) -> list[str]:
    """What `later` has that `earlier` did not. data_uses is a set in
    meaning even though it is stored as an array — order and duplicates in
    either input carry no significance. Sorted so callers (tests, D-CON-4's
    report) never have to guess an order of their own."""
    return sorted(set(later) - set(earlier))


def is_materially_different(
    earlier: Iterable[str], later: Iterable[str], *, rule: str = RULE_GAINED_USE
) -> bool:
    """Pure predicate: no database, no clock. `rule` names which materiality
    rule to apply — today there is exactly one (RULE_GAINED_USE), and an
    unrecognised name is a caller bug, not a data state, so it raises rather
    than silently falling back to some default behaviour."""
    if rule != RULE_GAINED_USE:
        raise ValueError(f"unknown consent materiality rule: {rule!r}")
    return bool(added_uses(list(earlier), list(later)))


def seed_consent_rule(db: Session) -> None:
    """Idempotent: ON CONFLICT DO NOTHING means a rerun writes nothing.
    Never commits — same rule as timelines.seed_timelines, the caller's
    session boundary decides when this becomes durable."""
    db.execute(
        _SEED_SQL,
        {"id": _SEED_ROW_ID, "rule": RULE_GAINED_USE, "source_note": _SOURCE_NOTE},
    )


def active_rule(db: Session) -> str:
    """The rule name Carol's row currently holds. Exactly one row is the
    only state this table is allowed to be in — seed_consent_rule's fixed
    id is what enforces that for its own writes, but the table carries no
    uniqueness constraint of its own, so any other writer (a hand-written
    insert, an ORM call, an admin tool) can land a second row with a fresh
    random id and slip past that conflict target entirely.

    Both zero rows and more than one row are the same underlying failure —
    a config value nobody deliberately set (or nobody deliberately kept
    singular) being read as if it were meaningful — so both raise by table
    name instead of guessing. Zero means nobody has run the seed CLI (Task
    4) yet, the same trap the DSR register hit. More than one means the
    table's only sanctioned invariant (exactly one row) has already been
    violated, and picking one of the rows via ORDER BY / LIMIT would hide
    that violation behind whichever row happened to be touched most
    recently — silently, which is worse than the empty-table case, since
    the loud failure the empty check performs would not fire at all."""
    rows = db.execute(_SELECT_ACTIVE_RULE_SQL).fetchall()
    if len(rows) == 0:
        raise ValueError(
            "privacycare_consent_rule is empty — run the seed CLI "
            "(scripts/privacycare/seed_consent.py, Task 4) before reading "
            "the active materiality rule"
        )
    if len(rows) > 1:
        raise ValueError(
            f"privacycare_consent_rule holds {len(rows)} rows — exactly "
            "one is expected. A second writer bypassed seed_consent_rule's "
            "fixed id; resolve the ambiguity in the table before reading "
            "the active materiality rule"
        )
    return rows[0][0]
