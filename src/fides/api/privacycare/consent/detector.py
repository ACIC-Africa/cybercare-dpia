"""Find data subjects whose consent has gone stale against a privacy notice
that has since gained a processing purpose.

Spec D-CON-2 / D-CON-4. Fides records which `privacynoticehistory` version a
subject consented against, but never compares it to anything. This module
is that comparison.

**This module is READ-ONLY against Ethyca's consent tables.** It issues
`SELECT` only — no `INSERT`, no `UPDATE`, no `DELETE` — against
`privacynotice`, `noticetranslation`, `privacynoticehistory` or
`privacypreferencehistory`, and it writes nothing to a preference row.
Staleness is computed fresh on every call from whatever those tables hold
right now; it is never cached or written back onto a preference. A
regulator's copy of what a subject did must not change because our
materiality rule changed later (spec D-CON-3) — so this module has no
write path to change it with.

**The join.** `privacypreferencehistory.privacy_notice_history_id` points
at the `privacynoticehistory` row a subject's preference was recorded
against. That row carries `.version` and `.data_uses` for the moment it was
created, plus `.translation_id`, which is how it reaches the notice it
belongs to: `translation_id -> noticetranslation.id -> privacy_notice_id ->
privacynotice.id`.

**`privacynotice` has no `version` column of its own.** A notice's LIVE
version is the highest `version` among the `privacynoticehistory` rows
reachable through its translations — not the most recently created row.
History rows are versioned by an application-level counter
(`existing_version + 1.0`, see `create_historical_record_for_notice_and_
translation` in `fides.api.models.privacy_notice`), and nothing stops one
from landing in the table after a higher-numbered one (a slow write racing
an earlier one, or a backfill). `MAX(version)` is the only version-numbering
fact that's actually true regardless of insertion order; `MAX(created_at)`
or "last row inserted" is not.
"""
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

import sqlalchemy
from sqlalchemy.orm import Session

from fides.api.privacycare.consent.materiality import active_rule, added_uses, is_materially_different

# Only an affirmative opt-in can be stale. `opt_out` means the subject
# declined the processing outright — there is nothing to re-consent to, and
# reporting them would ask them to re-agree to something they already
# turned down. `acknowledge` belongs to `notice_only` notices, where the
# mechanism never asked for a yes/no in the first place — it records that
# the notice was shown, not that the subject agreed to anything — so there
# is no consent there to invalidate either. Only `opt_in` names an
# affirmative agreement that a later, broader notice can outrun.
_STALE_ELIGIBLE_PREFERENCE = "opt_in"

# `privacynoticehistory.notice_key` is a denormalised copy of the owning
# notice's key at the time of that edit, but the query below deliberately
# does NOT group by it. The plan's structure section defines "live version"
# as the highest version reachable through a notice's translations — i.e.
# grouped by `noticetranslation.privacy_notice_id` — and that is what this
# reads.
_QUERY = sqlalchemy.text(
    """
    WITH live_version AS (
        SELECT DISTINCT ON (nt.privacy_notice_id)
            nt.privacy_notice_id AS notice_id,
            pnh.version           AS live_version,
            pnh.data_uses         AS live_data_uses,
            pnh.name              AS live_name
        FROM privacynoticehistory pnh
        JOIN noticetranslation nt ON nt.id = pnh.translation_id
        ORDER BY nt.privacy_notice_id, pnh.version DESC
    )
    SELECT
        pph.email             AS email,
        pph.fides_user_device AS fides_user_device,
        pph.external_id       AS external_id,
        pph.preference        AS preference,
        pph.received_at       AS received_at,
        cv.notice_key         AS notice_key,
        cv.version            AS consented_version,
        cv.data_uses          AS consented_data_uses,
        lv.live_version       AS live_version,
        lv.live_data_uses     AS live_data_uses,
        lv.live_name          AS live_name
    FROM privacypreferencehistory pph
    JOIN privacynoticehistory cv ON cv.id = pph.privacy_notice_history_id
    JOIN noticetranslation nt    ON nt.id = cv.translation_id
    JOIN live_version lv         ON lv.notice_id = nt.privacy_notice_id
    WHERE pph.preference = :stale_eligible
      AND (:notice_key IS NULL OR cv.notice_key = :notice_key)
    """
)


@dataclass(frozen=True)
class StaleConsent:
    subject: str  # email, or the device id when there is no email
    subject_kind: str  # "email" | "fides_user_device" | "external_id"
    notice_key: str
    notice_name: str
    consented_version: float
    live_version: float
    added_uses: list[str]
    preference: str
    received_at: datetime


def _subject(row: Any) -> tuple[str, str]:
    """Which identity a preference row carries, in the order Fides itself
    prefers one identity type over another for consent reporting: email,
    then the device id, then an external id. Exactly one of the three is
    expected to be set on any given row — `privacypreferencehistory` allows
    all three to be NULL at the schema level, but a preference recorded
    with no identity at all is a Fides data-integrity problem this
    detector cannot paper over, so that case raises rather than returning
    a description of a subject nobody can act on."""
    if row.email is not None:
        return row.email, "email"
    if row.fides_user_device is not None:
        return row.fides_user_device, "fides_user_device"
    if row.external_id is not None:
        return row.external_id, "external_id"
    raise ValueError(
        "privacypreferencehistory row carries no identity — email, "
        "fides_user_device and external_id are all NULL"
    )


def find_stale_consents(db: Session, *, notice_key: Optional[str] = None) -> list[StaleConsent]:
    """Every affirmative consent recorded against a notice version that has
    since gained a data use, per the currently active materiality rule
    (Task 1's `active_rule` — raises if nobody has configured one yet).

    Pass `notice_key` to narrow the report to one notice; omit it for
    every notice at once. Read-only: see the module docstring.
    """
    rule = active_rule(db)
    rows = db.execute(
        _QUERY, {"stale_eligible": _STALE_ELIGIBLE_PREFERENCE, "notice_key": notice_key}
    ).fetchall()

    stale: list[StaleConsent] = []
    for row in rows:
        if not is_materially_different(row.consented_data_uses, row.live_data_uses, rule=rule):
            continue
        subject, subject_kind = _subject(row)
        stale.append(
            StaleConsent(
                subject=subject,
                subject_kind=subject_kind,
                notice_key=row.notice_key,
                notice_name=row.live_name,
                consented_version=row.consented_version,
                live_version=row.live_version,
                added_uses=added_uses(row.consented_data_uses, row.live_data_uses),
                preference=row.preference,
                received_at=row.received_at,
            )
        )
    return stale
