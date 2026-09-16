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

**A SUBJECT, NOT A ROW (final review, Finding 1).** `privacy
preferencehistory` is append-only evidence: every time a subject answers,
Fides writes another row and leaves the old ones standing. An earlier
version of this module reported one finding per stale ROW, which made the
report both wrong and unusable:

* Alice opts in at v1, is reported, Josephine contacts her, Alice
  re-consents at v2. Both rows live in the table forever, so Alice was
  reported forever. The list never shrank and Josephine could not tell
  who she had already handled.
* Bob opts in at v1 and later opts OUT at v2. He was reported as needing
  re-consent — the exact thing `_AFFIRMATIVE_PREFERENCE`'s comment below
  forbids. Re-soliciting somebody who withdrew is a data-protection
  problem, not merely wasted effort.

So this module now reduces each subject to their CURRENT POSITION before
judging staleness: every preference row in scope is grouped by
`(subject, subject_kind, notice)`, only the subject's latest row in each
group survives, and the affirmative-consent filter and the materiality
rule are applied to THAT row alone. A latest row of `opt_out` or
`acknowledge` yields nothing; a latest row of `opt_in` against a
materially older version is the finding.

"Latest" is `received_at` descending with NULLs LAST, then `created_at`
descending, then the row id for a total, deterministic order. `received_at`
is the authoritative "when the subject actually answered" and is nullable
with no default, so a row that carries one outranks a row that does not;
`created_at` (defaulted `now()`) breaks the tie when neither does.

**Reduction happens in Python, not in SQL**, because the grouping key is
the subject's identity and that identity is encrypted at rest — see
"Subject identity" below. The identities therefore have to be resolved for
every CANDIDATE row, before the reduction, not only for the matched subset.

`subject_kind == "none"` rows (no identity at all — a Fides data-integrity
problem, see `_subject`) cannot be grouped with anything, because nothing
about them says whether two such rows are the same person. Each one stays
its own group and is still reported individually; that behaviour is
deliberate and survives the reduction.

**Reaching the notice (final review, Finding 3).** A `privacynoticehistory`
row carries `.version` and `.data_uses` for the moment it was created,
plus `.translation_id`, which points at the `noticetranslation` it was
written for. This module used to reach the owning notice ONLY through that
pointer, with three INNER JOINs — and that silently discarded findings.
`privacynoticehistory.translation_id` is `ForeignKey(..., ondelete="SET
NULL")`, and Ethyca's own comment on it (`models/privacy_notice.py`) says
why: "Set to null if the translation is deleted, but we retain this record
for consent reporting." `PrivacyNotice.update` calls
`delete_notice_translations`, which deletes every translation NOT supplied
in the update request. So a notice that gains a purpose and drops its
Swahili translation in the same save loses every Swahili-recorded
preference from the report; a save supplying `translations: []` drops the
notice from the report entirely and returns "nothing is stale" for a
notice that just gained a processing purpose. Ethyca deliberately
preserves those rows; this query was the one consumer throwing them away.

So a history row now resolves to its notice through, in order:

1. `translation_id -> noticetranslation.privacy_notice_id` (the normal path),
2. failing that, `privacynoticehistory.notice_key -> privacynotice.notice_key`
   — `notice_key` is denormalised onto the history row, is `NOT NULL` in
   the live schema (verified against `fides-db`), and is translation
   independent, so a `translation_id`-NULL row still finds its notice,
3. failing even that (the notice row itself is gone, or its key was
   changed after this history row was written), the literal
   `'notice_key:' || notice_key`, so the row is grouped with its own kind
   rather than dropped.

`privacynotice.notice_key` carries NO uniqueness constraint in the live
schema, so step 2 is a deterministic scalar subquery (`ORDER BY ... LIMIT
1`) rather than a join — a join would fan out and duplicate preference
rows if a key were ever doubled.

**`privacynotice` has no `version` column of its own.** A notice's LIVE
version is the highest `version` among the `privacynoticehistory` rows that
resolve to it — not the most recently created row. History rows are
versioned by an application-level counter (`existing_version + 1.0`, see
`create_historical_record_for_notice_and_translation` in
`fides.api.models.privacy_notice`), and nothing stops one from landing in
the table after a higher-numbered one (a slow write racing an earlier one,
or a backfill). `MAX(version)` is the only version-numbering fact that's
actually true regardless of insertion order; `MAX(created_at)` or "last row
inserted" is not.

**`notice_key` and `notice_name` are both LIVE (final review, minor).**
The reported `notice_key` — and the `notice_key` filter argument — used to
come from the version the subject consented AGAINST while `notice_name`
came from the live one. If a notice's key is ever changed, filtering by the
key Josephine sees today would then return nothing for exactly the people
who need re-consent. Both now name the live version.

**Subject identity is resolved through the ORM, not through `_QUERY`.**
`privacypreferencehistory.email` / `.phone_number` / `.fides_user_device` /
`.external_id` are `StringEncryptedType` columns (AES-GCM; see
`ConsentIdentitiesMixin` in `fides.api.models.privacy_preference`) — the
encryption is a `TypeDecorator` that only fires when SQLAlchemy knows the
column's type, which a raw `sqlalchemy.text()` SELECT never does. Reading
those columns via raw SQL returns ciphertext, not a usable identity, for
any preference actually recorded through Fides' own consent flow (the ORM
`create`/`persist_obj` path). So `_QUERY` finds the version facts and the
`privacypreferencehistory.id`s, and `find_stale_consents` then loads those
rows through the `PrivacyPreferenceHistory` model — still read-only, a
`SELECT` via `db.query(...)`, no `add`/`commit`/`delete` — so the ORM's
decrypting type decorator applies and `.email` etc. come back as plaintext.

That ORM load is narrowed with `load_only` to the id and the four identity
columns and NOTHING else. `privacypreferencehistory` also holds
`secondary_user_ids` (an encrypted blob of identities shared with third
parties), `user_agent`, `url_recorded` and `anonymized_ip_address`; none of
them appear in the report, so none of them are read or decrypted here.
PrivacyCare reads only what the job requires.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

import sqlalchemy
from sqlalchemy.orm import Session, load_only

from fides.api.models.privacy_preference import PrivacyPreferenceHistory
from fides.api.privacycare.consent.materiality import active_rule, added_uses, is_materially_different

# Only an affirmative opt-in can be stale, and it is the subject's LATEST
# row that has to carry it. `opt_out` means the subject declined the
# processing outright — there is nothing to re-consent to, and reporting
# them would ask them to re-agree to something they already turned down.
# `acknowledge` belongs to `notice_only` notices, where the mechanism never
# asked for a yes/no in the first place — it records that the notice was
# shown, not that the subject agreed to anything — so there is no consent
# there to invalidate either. Only `opt_in` names an affirmative agreement
# that a later, broader notice can outrun.
#
# This is checked AFTER the per-subject reduction, never before it. Checking
# it in the SQL (as this module used to) cannot see a withdrawal at all: the
# `opt_out` row is filtered away before anything has a chance to notice that
# it supersedes the subject's earlier `opt_in`.
_AFFIRMATIVE_PREFERENCE = "opt_in"

# `_ORDER_FLOOR` only ever stands in for a NULL timestamp inside `_position`
# below, and only ever gets compared against another NULL's floor, because
# the "is it set at all" flag sorts first. Timezone-aware because Postgres
# hands back `timestamp with time zone`, and a naive/aware comparison
# raises.
_ORDER_FLOOR = datetime.min.replace(tzinfo=timezone.utc)

_QUERY = sqlalchemy.text(
    """
    WITH history AS (
        SELECT
            pnh.id            AS id,
            pnh.version       AS version,
            pnh.data_uses     AS data_uses,
            pnh.name          AS name,
            COALESCE(
                nt.privacy_notice_id,
                (
                    SELECT pn.id
                    FROM privacynotice pn
                    WHERE pn.notice_key = pnh.notice_key
                    ORDER BY pn.created_at, pn.id
                    LIMIT 1
                ),
                'notice_key:' || pnh.notice_key
            )                 AS notice_ref,
            pnh.notice_key    AS notice_key
        FROM privacynoticehistory pnh
        LEFT JOIN noticetranslation nt ON nt.id = pnh.translation_id
    ),
    live_version AS (
        SELECT DISTINCT ON (h.notice_ref)
            h.notice_ref   AS notice_ref,
            h.version      AS live_version,
            h.data_uses    AS live_data_uses,
            h.name         AS live_name,
            h.notice_key   AS live_notice_key
        FROM history h
        ORDER BY h.notice_ref, h.version DESC, h.id
    )
    SELECT
        pph.id                AS pref_id,
        pph.preference        AS preference,
        pph.received_at       AS received_at,
        pph.created_at        AS created_at,
        lv.notice_ref         AS notice_ref,
        lv.live_notice_key    AS notice_key,
        cv.version            AS consented_version,
        cv.data_uses          AS consented_data_uses,
        lv.live_version       AS live_version,
        lv.live_data_uses     AS live_data_uses,
        lv.live_name          AS live_name
    FROM privacypreferencehistory pph
    JOIN history cv      ON cv.id = pph.privacy_notice_history_id
    JOIN live_version lv ON lv.notice_ref = cv.notice_ref
    WHERE (:notice_key IS NULL OR lv.live_notice_key = :notice_key)
    """
)


@dataclass(frozen=True)
class StaleConsent:
    subject: str  # email, phone number, device id, external id, or a "privacypreferencehistory:<id>" trace-back when the row has none of those
    subject_kind: str  # "email" | "phone_number" | "fides_user_device" | "external_id" | "none"
    notice_key: str
    notice_name: str
    consented_version: float
    live_version: float
    added_uses: list[str]
    preference: str
    # Coordinator ruling (Task 3, fix round 1): Optional, not `datetime`.
    # `privacypreferencehistory.received_at` is nullable with no default —
    # the only NOT NULL columns without a default on that table are `id`
    # and `preference` — so a genuinely-collected preference row can carry
    # a NULL `received_at` today. A report that says "we do not know when
    # this consent was given" is strictly better than one that crashes:
    # the subject's identity and the version gap are what make this report
    # actionable under spec D-CON-4, and the timestamp is supporting
    # detail, not load-bearing for that judgement.
    received_at: Optional[datetime]


def _subject(row: Any) -> tuple[str, str]:
    """Which identity a preference row carries.

    All FOUR of Fides' consent identities are resolved here — `email`,
    `phone_number`, `fides_user_device`, `external_id` — the four
    `ConsentIdentitiesMixin` declares and the four
    `CurrentPrivacyPreferenceV2` carries uniqueness constraints over.
    `phone_number` was missing until the final review's Finding 2, which
    mattered for exactly this customer: if consent is captured in a fuel
    card or loyalty base, the identifier is an MSISDN, as it would be for
    essentially any Kenyan retailer. Every such subject came back as
    `subject_kind: "none"` with nothing but a row id — uncontactable, and
    worse, indistinguishable from the corrupt-data case `"none"` exists to
    flag, so a perfectly well-formed phone-identified record read as
    something to go chase.

    Directly contactable identities come first (email, then phone number:
    Josephine has to be able to reach the person), then the opaque
    technical identifiers (device id, then external id). Exactly one is
    expected to be set on any given row — `privacypreferencehistory` allows
    all four to be NULL at the schema level.

    Coordinator ruling (Task 3, fix round 2): a preference recorded with no
    identity at all IS a Fides data-integrity problem, but it used to be
    treated as a reason to raise ValueError here — which api/consent.py's
    route caught in the SAME except block as three unrelated configuration
    failures (see materiality.validate_active_rule), turning one bad row
    into an identical 503 and silently discarding every OTHER row's
    legitimate finding in the same call (find_stale_consents builds its
    whole list before returning any of it, so one raise mid-loop loses the
    rest). The notice, both versions and the added uses are all still true
    and actionable even when nobody can be named, so this reports instead
    of raising: `"none"` — never a real identity kind — as a first-class
    `subject_kind`, the same "none is a value, not an exception" pattern
    this codebase already uses elsewhere, paired with a subject string
    that names the offending `privacypreferencehistory` row by id so an
    operator can go trace and fix the underlying data rather than losing
    the finding entirely."""
    if row.email is not None:
        return row.email, "email"
    if row.phone_number is not None:
        return row.phone_number, "phone_number"
    if row.fides_user_device is not None:
        return row.fides_user_device, "fides_user_device"
    if row.external_id is not None:
        return row.external_id, "external_id"
    return f"privacypreferencehistory:{row.id}", "none"


def _position(row: Any) -> tuple:
    """Where a preference row sits in its subject's timeline. Bigger is
    later; the maximum is the subject's current position.

    `received_at` first, NULLs LAST — the leading boolean is what puts a
    row that records when the subject actually answered ahead of one that
    does not. `created_at` (defaulted `now()`) breaks the tie when neither
    carries a `received_at`, under the same NULLs-last treatment, and the
    row id makes the order total so the reduction is deterministic
    regardless of the order Postgres happens to return rows in."""
    return (
        row.received_at is not None,
        row.received_at or _ORDER_FLOOR,
        row.created_at is not None,
        row.created_at or _ORDER_FLOOR,
        row.pref_id,
    )


def _identities_by_id(db: Session, pref_ids: list[str]) -> dict:
    """The four identity columns for `pref_ids`, decrypted, keyed by row id.

    Read-only: a plain `SELECT ... WHERE id IN (...)` through the ORM, which
    is the only way the encrypted columns come back as plaintext (see the
    module docstring). `load_only` narrows it to the id and the four
    identity columns so nothing else on the row — `secondary_user_ids`,
    `user_agent`, `url_recorded`, `anonymized_ip_address` — is read or
    decrypted; the report uses none of them."""
    if not pref_ids:
        return {}
    return {
        preference.id: preference
        for preference in db.query(PrivacyPreferenceHistory)
        .options(
            load_only(
                PrivacyPreferenceHistory.id,
                PrivacyPreferenceHistory.email,
                PrivacyPreferenceHistory.phone_number,
                PrivacyPreferenceHistory.fides_user_device,
                PrivacyPreferenceHistory.external_id,
            )
        )
        .filter(PrivacyPreferenceHistory.id.in_(pref_ids))
        .all()
    }


def find_stale_consents(db: Session, *, notice_key: Optional[str] = None) -> list[StaleConsent]:
    """Every data subject whose CURRENT consent position is an affirmative
    opt-in against a notice version that has since gained a data use, per
    the currently active materiality rule (Task 1's `active_rule` — raises
    if nobody has configured one yet).

    One finding per subject per notice, never one per row: a subject who
    has since re-consented against the live version, or who has since
    opted out, is not reported at all. See the module docstring for the
    reduction and its ordering rule.

    Pass `notice_key` to narrow the report to one notice — matched against
    the notice's LIVE key, the one Josephine sees today. Omit it for every
    notice at once. Read-only: see the module docstring.
    """
    rule = active_rule(db)
    # Every preference row for the notices in scope, at every preference
    # value — NOT just `opt_in`. The withdrawals have to come back or the
    # reduction below cannot see that one supersedes an earlier opt-in.
    rows = db.execute(_QUERY, {"notice_key": notice_key}).fetchall()
    if not rows:
        return []

    identities = _identities_by_id(db, [row.pref_id for row in rows])

    # Reduce each subject to their current position. The grouping key is
    # the subject plus `notice_ref` — the notice itself, which the live
    # `notice_key` names; consent is per notice, so the same person can be
    # current on one notice and stale on another.
    latest: dict[Any, tuple[Any, str, str]] = {}
    for row in rows:
        subject, subject_kind = _subject(identities[row.pref_id])
        if subject_kind == "none":
            # No identity means nothing says whether two such rows are the
            # same person, so each is its own group and is still reported
            # individually — deliberate, see `_subject`.
            group: Any = ("none", row.pref_id)
        else:
            group = (subject_kind, subject, row.notice_ref)
        held = latest.get(group)
        if held is None or _position(row) > _position(held[0]):
            latest[group] = (row, subject, subject_kind)

    stale: list[StaleConsent] = []
    for row, subject, subject_kind in latest.values():
        if row.preference != _AFFIRMATIVE_PREFERENCE:
            continue
        if not is_materially_different(
            row.consented_data_uses, row.live_data_uses, rule=rule
        ):
            continue
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
