# The DPIA risk register itself: recording, listing, removing, and rolling
# up each identified risk against a Kenyan DPIA. All raw SQL, all bound
# parameters, never a commit — the caller's session boundary decides, same
# rule as dsr/register.py and taxonomy/loader.py.
#
# score and band are never read from or written to the database. Both are
# computed here, through banding.score()/band() (Task 1), from the
# likelihood and severity a row actually stores — see models.py's
# dpia_risk_table comment for why storing either would be a liability, not
# a convenience.
import uuid
from dataclasses import dataclass

import sqlalchemy
from sqlalchemy.orm import Session

from fides.api.privacycare.risk.banding import (
    band,
    overall_band,
    projected_risk_level,
    score,
)

# The seven risk categories are Carol's, sector-neutral, and carry across
# unchanged from the DPIA methodology. A category outside this set is
# rejected by name rather than silently accepted, the same discipline
# dsr/register.py applies to an unknown `right`.
_CATEGORIES = (
    "confidentiality",
    "integrity",
    "availability",
    "discrimination",
    "loss_of_autonomy",
    "financial_or_reputational_harm",
    "physical_harm",
)


@dataclass(frozen=True)
class RiskEntry:
    id: str
    assessment_id: str
    category: str
    description: str
    likelihood: int
    severity: int
    score: int
    band: str


_ASSESSMENT_EXISTS_SQL = sqlalchemy.text(
    "SELECT 1 FROM privacy_assessment WHERE id = :id"
)

_INSERT_RISK_SQL = sqlalchemy.text(
    "INSERT INTO privacycare_dpia_risk "
    "(id, assessment_id, category, description, likelihood, severity) "
    "VALUES (:id, :assessment_id, :category, :description, :likelihood, :severity)"
)

_RISK_COLUMNS = "id, assessment_id, category, description, likelihood, severity"

_LIST_RISKS_SQL = sqlalchemy.text(
    f"SELECT {_RISK_COLUMNS} FROM privacycare_dpia_risk WHERE assessment_id = :assessment_id"
)

_DELETE_RISK_SQL = sqlalchemy.text(
    "DELETE FROM privacycare_dpia_risk WHERE id = :id RETURNING assessment_id"
)

# The one sanctioned write to an Ethyca table in this plan. risk_level is an
# UPDATE of a single column Ethyca already treats as hand-editable — it is
# in their own _UPDATABLE_ASSESSMENT_FIELDS (privacycare/api/assessments.py)
# — so this is a data write, not a schema change, and needs no migration.
# Only this one column is ever named on the right-hand side; every other
# column on the row is left untouched.
#
# Deliberately does NOT bump updated_at, unlike Ethyca's own
# _update_assessment (privacycare/api/assessments.py) which does
# `, updated_at = now()` alongside its writes. That column means "a human
# last touched this row" to anyone reading the DPIA's history; a projection
# recomputed automatically every time the register changes is not a human
# edit, and bumping updated_at here would make an automatic recomputation
# look like someone edited the DPIA — corrupting the audit trail in a way
# that is worse than the timestamp being merely stale. Kept as-is on
# purpose, not an oversight.
_SYNC_PROJECTION_SQL = sqlalchemy.text(
    "UPDATE privacy_assessment SET risk_level = :risk_level WHERE id = :id"
)

# Same FOR UPDATE-on-the-parent-row discipline api/answers.py's
# _LOCK_ASSESSMENT_SQL and api/assessments.py's _LOCK_ASSESSMENT_ROW_SQL
# already use for every other write/recompute against privacy_assessment:
# take the lock BEFORE the read that decides what to write, not just before
# the write itself. sync_projection used to read the register (list_risks,
# inside assessment_band) with no lock at all, at READ COMMITTED. A plain
# UPDATE still takes an implicit row lock, but only at the UPDATE
# statement — by then the value being written was already computed from a
# snapshot that could predate a concurrent committer's insert. Two DPOs
# adding risks to the same DPIA at once could then lose the higher band:
# transaction A inserts a critical risk and updates risk_level to "high",
# holding the row locked (via its own UPDATE) until it commits; transaction
# B, whose list_risks snapshot ran before A's commit, computes "low" from
# its own smaller risk, blocks on A's implicit lock, and once A commits and
# releases it, B proceeds to write its stale "low" straight over A's
# "high" — durably wrong until the next mutation. Locking first forces B to
# block BEFORE it reads, so once unblocked it re-reads the post-commit
# state and recomputes the correct band.
_LOCK_ASSESSMENT_ROW_SQL = sqlalchemy.text(
    "SELECT id FROM privacy_assessment WHERE id = :id FOR UPDATE"
)


def _to_entry(row) -> RiskEntry:
    risk_score = score(row.likelihood, row.severity)
    return RiskEntry(
        id=row.id,
        assessment_id=row.assessment_id,
        category=row.category,
        description=row.description,
        likelihood=row.likelihood,
        severity=row.severity,
        score=risk_score,
        band=band(risk_score),
    )


def add_risk(
    db: Session,
    *,
    assessment_id: str,
    category: str,
    description: str,
    likelihood: int,
    severity: int,
) -> RiskEntry:
    """Validates before writing anything: an out-of-range likelihood or
    severity, or a category outside Carol's seven, must not leave a
    half-written risk. score(likelihood, severity) itself raises ValueError
    for either parameter outside 1..5, so that check is not duplicated here
    — this function neither re-implements nor re-validates the arithmetic
    banding.py already owns.

    assessment_id carries no ForeignKey to privacy_assessment (see
    models.py) — existence is checked here, once, before the insert."""
    # Raises ValueError for an out-of-range likelihood or severity before
    # anything is written. Computed once and reused below rather than
    # trusted blindly a second time.
    risk_score = score(likelihood, severity)

    if category not in _CATEGORIES:
        raise ValueError(
            f"{category!r} is not one of the seven risk categories: {_CATEGORIES}"
        )

    exists = db.execute(_ASSESSMENT_EXISTS_SQL, {"id": assessment_id}).first()
    if exists is None:
        raise ValueError(f"no such assessment: {assessment_id!r}")

    risk_id = str(uuid.uuid4())
    db.execute(
        _INSERT_RISK_SQL,
        {
            "id": risk_id,
            "assessment_id": assessment_id,
            "category": category,
            "description": description,
            "likelihood": likelihood,
            "severity": severity,
        },
    )
    # The register just changed, so the projection Ethyca's admin UI reads
    # is now stale — resync it after the mutation has taken effect, same as
    # remove_risk below. Nothing else calls sync_projection; a route that
    # wants risk_level current always goes through add_risk/remove_risk,
    # never by setting the column itself.
    sync_projection(db, assessment_id)
    return RiskEntry(
        id=risk_id,
        assessment_id=assessment_id,
        category=category,
        description=description,
        likelihood=likelihood,
        severity=severity,
        score=risk_score,
        band=band(risk_score),
    )


def list_risks(db: Session, assessment_id: str) -> list[RiskEntry]:
    """Ordered by score descending, then id — score is computed per row
    since it is never stored, so the sort happens here rather than in SQL."""
    rows = db.execute(_LIST_RISKS_SQL, {"assessment_id": assessment_id}).all()
    entries = [_to_entry(row) for row in rows]
    entries.sort(key=lambda entry: (-entry.score, entry.id))
    return entries


def remove_risk(db: Session, risk_id: str) -> bool:
    """True if a row was actually removed, False for an id that did not
    exist — the caller can tell a real deletion from a no-op.

    The DELETE returns the removed row's assessment_id (there is no other
    way to know which assessment to resync once the row is gone), and on an
    actual removal the projection is resynced against what remains."""
    result = db.execute(_DELETE_RISK_SQL, {"id": risk_id})
    row = result.first()
    if row is None:
        return False
    sync_projection(db, row.assessment_id)
    return True


def assessment_band(db: Session, assessment_id: str) -> str:
    """The band of the assessment's highest-scoring risk — LOW when the
    register holds nothing for this assessment yet (overall_band's own
    empty-input default, per spec D-W2-3)."""
    scores = [entry.score for entry in list_risks(db, assessment_id)]
    return overall_band(scores)


def sync_projection(db: Session, assessment_id: str) -> str:
    """Write projected_risk_level(assessment_band(...)) to
    privacy_assessment.risk_level, and return the OUR-side band — not the
    projection just written.

    This is the one sanctioned write to an Ethyca table anywhere in this
    plan. It is an UPDATE of a single column, risk_level, which Ethyca
    already treats as hand-editable (it is in their own
    _UPDATABLE_ASSESSMENT_FIELDS in api/assessments.py) — this is a data
    write, not a schema change, and needs no migration. No other column on
    the row is read or written.

    sync_projection is the ONLY writer of risk_level anywhere in this plan.
    Nothing of ours offers a setter for the band or for risk_level directly;
    add_risk and remove_risk call this at the end of every mutation, after
    it has taken effect, so the column never has a chance to go stale.
    Ethyca's own API can still accept risk_level directly
    (_UPDATABLE_ASSESSMENT_FIELDS does not know about us), but any value
    typed in by hand is overwritten the next time the register changes and
    this runs again.

    The return value is our four-value band (LOW/MEDIUM/HIGH/CRITICAL),
    never the three-value projection written to the column. A caller
    deciding the ODPC-escalation rule (Task 4) must be able to tell
    CRITICAL from HIGH; projected_risk_level collapses CRITICAL into
    "high", so returning the projection instead of the band would make
    that distinction unrecoverable from this function's result.

    Idempotent: calling it again with no change to the register writes the
    same value and leaves the row otherwise untouched.

    Takes a FOR UPDATE lock on the privacy_assessment row BEFORE reading the
    register (assessment_band -> list_risks), not just before the write —
    see _LOCK_ASSESSMENT_ROW_SQL's comment for the lost-update this closes.
    A row that does not exist locks nothing (the SELECT returns no rows)
    and the UPDATE below is then the same no-op it always was for an
    unknown assessment_id.
    """
    db.execute(_LOCK_ASSESSMENT_ROW_SQL, {"id": assessment_id})
    our_band = assessment_band(db, assessment_id)
    db.execute(
        _SYNC_PROJECTION_SQL,
        {"risk_level": projected_risk_level(our_band), "id": assessment_id},
    )
    return our_band
