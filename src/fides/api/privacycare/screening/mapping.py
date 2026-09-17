"""Capturing the data mapping behind a business process (plan 20, Task 4).

Of the customer's 86 business processes, exactly ONE has any data mapping —
who the data subjects are, what categories of data, on what lawful basis.
Without a mapping there is nothing to assess, so 85 of her processes could
never produce a DPIA. This module is the write path that closes that gap:
`save_mapping` creates a `privacydeclaration` (Ethyca's own "processing
activity" table) from her own loaded vocabularies and links it to the
business process through `privacycare_process_declaration` — the same link
table tasks.py's `_is_activity_screened_out` already reads to resolve an
activity back to its process's screening verdict.

VOCABULARIES, NOT FREE TEXT (fix round 1, item 1; extended to `purpose` in
the final fix wave, item C-2). Every data subject and data category offered
to her lives in `ctl_data_subjects`/`ctl_data_categories` — her 33/29
loaded-for-her rows (`is_default = false`) SIDE BY SIDE with fideslang's own
shipped defaults (15/85 more), never instead of them (taxonomy/loader.py
loaded her additions ALONGSIDE fideslang, per plan 09). The first cut of
this module validated against `is_default = false` only, which rejected the
ONLY data mapping the customer actually has today: both real live
activities on `bp_94d5439ced86` use exclusively default-valued rows
(`data_subjects = {customer}`, categories `user.contact.email`/
`user.contact.phone_number`/`user.financial`/`user.behavior.purchase_history`
— all `is_default = true`). Validation is now against the FULL active
taxonomy — existence in `ctl_data_subjects`/`ctl_data_categories` at all,
defaults included; no `is_default` filter, and no `active` filter either,
because no other PrivacyCare code filters on that column and every row in
both tables is currently `active = true` anyway (measured: 48/48, 114/114)
— inventing a filter nothing else uses would be exactly the kind of
ungrounded rule this task's brief warns against. An unknown value — one
absent from the taxonomy altogether — is still rejected BY NAME
(_unknown_values below) rather than silently dropped, because a silently
dropped value records a mapping the user did not make and cannot see is
missing.

`purpose` IS ALSO A TAXONOMY KEY, NOT PROSE (final fix wave, item C-2 —
this is the one field the first cut of this task got wrong). `purpose` is
written to `privacydeclaration.data_use`, and `data_use` is a `FidesKey`:
alphanumerics and `. _ < > -` only. An earlier round accepted free text
here on the theory that `PrivacyDeclaration.purpose`'s own hybrid property
(sql_models.py) "equates the two" — that hybrid returns an `Optional[int]`
TCF Purpose id **looked up by `data_use`**; reading it actually proves
`data_use` is a taxonomy key, the opposite of what was concluded. The two
route-created activities in the live database before this fix carried
`data_use` values of raw prose ("Verify applicant identity and issue a fuel
card...") — invalid `FidesKey`s that happened not to be validated by
anything downstream yet, not evidence that the shape was fine. `purpose` is
now validated against `ctl_data_uses` (56 rows, all fideslang defaults —
the Kenyan taxonomy load never added any of her own) by the SAME
`_unknown_values` helper the other three vocabularies use, and rejected BY
NAME when absent, exactly like a data subject or category. The
`UNSPECIFIED_DATA_USE` sentinel below is the one deliberate exception: it
is used only when `purpose` is not given at all (a legitimately partial
mapping), is already a valid `FidesKey`, and is never itself checked
against `ctl_data_uses` — see its own comment for why that is fine.

THE LAWFUL BASIS IS DERIVED, NEVER ACCEPTED. The client sends the *ground*
— her business situation ("KYC Requirements", "Enrolment of an Applicant")
— as free text matching `privacycare_processing_ground.ground` verbatim.
This module reads that row's `fides_legal_basis` (D-KT-4: 12 of the 23
loaded grounds still have none, and are rejected here exactly as
grounds.py's own `_record_declaration_ground` rejects them for the same
reason — nothing to derive, nothing Carol has ruled on) and writes it to
`legal_basis_for_processing`. There is no request field the client could
use to set that column directly — see DataMappingRequest in
screening_schemas.py, which carries `ground` and nothing else.

THE PROVENANCE IS WRITTEN, NOT JUST THE DERIVED VALUE (fix round 1, item
2). `legal_basis_for_processing = 'Consent'` on its own does not say WHICH
of the (possibly several) "Consent"-class grounds produced it — for a
compliance product, that link IS the audit trail a regulator would ask
about. Once this module has written the activity's own
`legal_basis_for_processing` (so it already agrees with the ground's
class), it calls grounds.py's OWN `_record_declaration_ground` — the exact
function `POST .../declaration/{id}/ground` already uses — to upsert the
`privacycare_declaration_ground` row, rather than inventing a second way to
write that table. Reused, not duplicated: `_record_declaration_ground`'s
own consistency check (declaration's stored legal basis must equal the
ground's class) passes trivially here because this module just wrote both
values from the SAME ground, in the SAME transaction. `_UPSERT_
DECLARATION_GROUND_SQL` (grounds.py) is itself an upsert keyed on the
UNIQUE `privacy_declaration_id` column, so re-submitting a mapping with a
DIFFERENT ground updates that one row rather than creating a second —
provenance for an activity is always exactly one ground, matching
`declaration_ground_table`'s own schema. Only called when `ground` is
given this call (`None` means "not answered this call", same rule as every
other optional field below) — an earlier call's provenance is left alone
otherwise.

SPECIAL-CATEGORY DATA IS DERIVED, NOT TICKED. `processes_special_category_
data` is computed from the chosen `data_categories` against the SAME
ancestor-prefix walk over `ctl_data_categories.tags` that the TOP-LEVEL
`privacycare/special_category.py` module's `derive_special_category`
already uses for the read path (SPECIAL_TAG = "dpa2019:special_category") —
that module lives directly under `privacycare/`, a sibling of this
`screening/` package, not inside it — reused here rather than
re-implemented, parameterised on the categories the caller is about to
write rather than on an already-persisted declaration's row, because the
flag has to be set atomically as part of the same INSERT/UPDATE.

A PARTIAL MAPPING IS SAVABLE. Only `name` and at least one `data_category`
are required to produce a valid activity (see DataMappingRequest). Every
other field — data subjects, ground, purpose, retention, third parties — is
Optional[...] = None, and None means "not answered THIS call", not "clear
it": an UPDATE only overwrites a column whose request field is not None
(COALESCE in the UPDATE statement itself), so a privacy officer who fills in
three fields today and two more next week never has today's answers erased
by tomorrow's partial form. `name` and `data_categories` are the two
exceptions — always required, always fully replace whatever was there,
because a caller who resubmits without a name or with zero categories is not
"still deciding", they are asking for an invalid activity, which is exactly
the case Step 1 rejects.

IDEMPOTENCY IS KEYED TO THE ACTIVITY THIS ROUTE CREATED, NEVER TO THE
PROCESS. A live measurement during this plan found a real business process
(bp_94d5439ced86) already carrying TWO real, unrelated activities — "Fuel
card marketing campaigns" and "Fuel card account administration" — so a
process can legitimately host more than one. Keying idempotency to the
PROCESS (the plan's original wording: "mapping a process twice updates
rather than creating a second activity") would make a second mapping of
that process silently overwrite whichever of those two activities the query
happened to find first — an unrelated marketing activity clobbered by a
KYC-mapping resubmit. Instead, this module tags every activity IT creates
with a marker string in `privacydeclaration.features` (a plain
ARRAY(String) column Ethyca already ships for exactly this kind of
lightweight, migration-free tagging — see sql_models.py's own "keep as JSON
blobs" comment on the sibling egress/ingress columns) and, on every call,
looks ONLY for an activity carrying that marker on THIS process
(_existing_route_activity below). An activity without the marker — every
pre-existing real one — is never read for the purpose of deciding whether
to update it, and never written to, by this function, under any
circumstances.

WHAT HAPPENS WHEN THE PROCESS HAS NO SYSTEM (fix round 1, item 3: kept,
constrained). `privacydeclaration.system_id` is NOT NULL, but a business
process has no system of its own — processes.py's own module docstring
says so explicitly ("Fides' map is anchored on systems, which can be
scanned, while a process exists only once a consultant has written it
down"), and the measured live data bears it out: 84 of the 86 processes
have never been linked to any system at all. Refusing to save a mapping
until someone first goes and creates a Fides system by hand would defeat
the entire point of this route — capturing the mapping AT THE MOMENT
someone decides the process matters — for all but the one process that
already happens to have one, and one shared "Unassigned" bucket holding 84
activities would be unreadable in Fides' own system-organised screens,
where process-named systems keep her estate legible.

So `_system_id_for_process` first looks for a system already in use by an
EXISTING (marker or not) activity linked to this same process, and only if
none exists does it provision a minimal placeholder `ctl_systems` row
scoped to THIS ONE process (fides_key `privacycare_process_<id>`, ON
CONFLICT DO NOTHING so a raced or repeated call never errors). This is
called ONLY from inside `save_mapping`'s create branch — lazily, once per
process, exactly when that process is first mapped through this route.
Nothing in this module, or anywhere else in this task, provisions a system
eagerly for all 86 processes up front; there is no code path that runs
`_system_id_for_process` other than a `save_mapping` call naming that
specific `business_process_id`.

Every system this module provisions is tagged `MAPPING_ROUTE_FEATURE_
MARKER` in its own `tags` column (an `ARRAY(String)` Ethyca already ships
on `ctl_systems`, same shape and same reasoning as `privacydeclaration.
features` above) — identifiable, and so removable later, the same way a
route-created ACTIVITY is. A system this module only REUSED (the two real
activities' own `ctl_ef9cadb3-...` system, or an earlier mapping's
provisioned one) is never tagged by this code path — only the INSERT that
creates a brand new one sets `tags`, so an already-real customer system is
never relabelled as ours. This is a PrivacyCare convention, not something
Ethyca or the customer defined, and is named as such in this task's report
for Product/Carol to review — there is exactly one `ctl_systems` row today,
so the shape of what 85 more provisioned rows looks like is not yet
visible in her demo.

A PROVISIONED SYSTEM MUST BE A VALID `System` (final fix wave, item C-1 —
the other defect the first cut of this task got wrong). `_INSERT_SYSTEM_SQL`
used to write only `(id, fides_key, name, description, tags)`, leaving
`organization_fides_key` and `system_type` NULL. Both are required,
non-Optional `str` on fideslang's own `System` model — which
`BasicSystemResponse` inherits, and which `GET /api/v1/system` declares as
its `response_model` — so FastAPI validates the WHOLE list on every call,
and a single bad row 500s the entire System Inventory screen, taking the
customer's own genuinely-valid systems down with it (measured live: it did,
for `fuel_card_crm`). This repo had already learned this exact lesson once,
from plan 09's raw-SQL taxonomy inserts (`test_created_rows_carry_default_
organization`, `test_created_rows_pass_fideslang_response_validation` —
tests/privacycare/test_taxonomy_loader.py) and the mapping route did not
inherit the convention. `_INSERT_SYSTEM_SQL` now sets both columns:
`organization_fides_key` to `'default_organization'` — the only row in
`ctl_organizations`, and what every Fides default row and the customer's
own `fuel_card_crm` already carry — and `system_type` to
`MAPPING_ROUTE_SYSTEM_TYPE` (`'Application'`, matching `fuel_card_crm`'s own
value; the field's own description names "Service, Application, Third
Party" as its examples, and there is no enum to pick from, so matching the
customer's one existing precedent is the least surprising choice for a
process-anchored placeholder with no scanned infrastructure of its own).

THE MAPPING CAN BE READ BACK (final fix wave, item I-2). The screen design
promises a partial mapping can be "saved and returned to", which is not
possible without a read side — and an officer who cannot see what they
already saved would silently create a THIRD activity on their next visit
(see "IDEMPOTENCY IS KEYED TO THE ACTIVITY" above). `get_mapping` answers
that: it returns the SAME row `save_mapping`'s own update branch would find
(`_EXISTING_ROUTE_ACTIVITY_SQL`, reused verbatim, taken with no lock — a
GET must never block a concurrent write), built through the SAME
`_result_from_row` construction `save_mapping` itself uses, so the two can
never disagree about what a mapping looks like. `None` for a process with
no route-owned activity — including one that carries only activities this
route does not own, which is a real and different state from "never
mapped" and must not be confused with it (see api/screening.py's own
`_response_from_mapping`-adjacent handling for how the route surfaces that
distinction as 200-with-null rather than 404).

All raw SQL, all bound parameters, never a commit — the caller's session
boundary decides, same rule as gate.py, risk/register.py and grounds.py.
"""
import uuid
from dataclasses import dataclass
from typing import List, Optional

import sqlalchemy
from sqlalchemy.orm import Session

# Reused, not duplicated (fix round 1, item 2) — the exact function
# grounds.py's own POST .../ground route already calls to write
# privacycare_declaration_ground. grounds.py decorates its OWN router
# (privacycare_grounds_router, a distinct prefix from screening's), so
# importing its business logic here carries none of the tasks-vs-
# assessments same-router registration-order hazard api/router.py's
# register() documents at length — see this module's own docstring.
from fides.api.privacycare.api.grounds import _record_declaration_ground
from fides.api.privacycare.taxonomy.kenyan import SPECIAL_TAG

# Tags every privacydeclaration row this module has ever created. The ONLY
# thing idempotency keys off of — see this module's own docstring for why
# the process id alone is not safe to key off of.
MAPPING_ROUTE_FEATURE_MARKER = "privacycare:mapping_route"

# The organization every route-provisioned system belongs to, and the type
# it is tagged with (fix wave, item C-1 — see this module's own docstring).
# 'default_organization' is the only row in ctl_organizations and what every
# Fides default row, and the customer's own real fuel_card_crm, already
# carry. 'Application' matches fuel_card_crm's own system_type — there is no
# enum to choose from (fideslang's System.system_type is a free str whose
# field description names "Service, Application, Third Party, etc." as
# examples only), so matching her one existing precedent is the least
# surprising choice for a process-anchored placeholder with no scanned
# infrastructure of its own.
MAPPING_ROUTE_ORGANIZATION_FIDES_KEY = "default_organization"
MAPPING_ROUTE_SYSTEM_TYPE = "Application"

# Written to `data_use` when no `purpose` is supplied yet (a legitimately
# partial mapping — see DataMappingRequest). Deliberately NOT one of the 56
# rows in ctl_data_uses (a real `purpose`, once given, must be — see this
# module's own docstring, "`purpose` IS ALSO A TAXONOMY KEY, NOT PROSE"):
# this sentinel is a synthetic placeholder for "nothing recorded yet", never
# checked against _VALID_DATA_USES_SQL itself (the None-vs-sentinel branch
# below chooses it directly, bypassing validation, precisely because "not
# answered" is not a value to validate). Both of `data_use`'s own read paths
# already tolerate an unmatched value gracefully rather than raising —
# context.py's build_context documents that "a declaration may legitimately
# name a custom data use" and resolves an unmatched one to None name/
# description, and PrivacyDeclaration.purpose's own hybrid property looks up
# MAPPED_PURPOSES_ONLY_BY_DATA_USE.get(self.data_use) and returns None for
# anything it does not recognise — this sentinel included, same as before.
UNSPECIFIED_DATA_USE = "privacycare.purpose_not_yet_recorded"


@dataclass(frozen=True)
class MappingResult:
    business_process_id: str
    privacy_declaration_id: str
    system_id: str
    name: str
    data_subjects: List[str]
    data_categories: List[str]
    ground: Optional[str]
    fides_legal_basis: Optional[str]
    purpose: Optional[str]
    retention_period: Optional[str]
    third_parties: Optional[str]
    processes_special_category_data: bool
    created: bool  # True the first time this route maps this process


# FOR UPDATE (fix round 2, item I-2): the same lock-the-parent-row-before-
# the-decision-that-follows-it discipline risk/register.py's sync_projection
# already uses (api/answers.py's _LOCK_ASSESSMENT_SQL, api/assessments.py's
# _LOCK_ASSESSMENT_ROW_SQL) — take the lock BEFORE the read that decides
# create-vs-update, not just before a write. Without it, two concurrent
# calls for the SAME business_process_id (a double-click, a UI retry) both
# run _EXISTING_ROUTE_ACTIVITY_SQL under READ COMMITTED, both find no
# marked activity, and both take the create branch: the process ends up
# with two marker-carrying activities, and every later resubmit updates
# only the earliest (_EXISTING_ROUTE_ACTIVITY_SQL's own created_at/id
# tie-break), leaving the second permanently stranded — still a real
# generation target, still capable of producing a spurious DPIA. This
# SELECT is the first statement save_mapping runs (it also IS the existence
# check), so the lock is held for the rest of the transaction: a second
# transaction's own SELECT ... FOR UPDATE against the same row blocks here
# until the first commits or rolls back, and once unblocked it re-reads
# (via its own _EXISTING_ROUTE_ACTIVITY_SQL, run only after this lock is
# acquired) the post-commit state — including the first transaction's own
# newly-marked activity — rather than a stale pre-commit snapshot. No
# migration: this locks an existing row, it does not touch schema.
_BUSINESS_PROCESS_NAME_SQL = sqlalchemy.text(
    "SELECT name FROM privacycare_business_process WHERE id = :id FOR UPDATE"
)

# A local copy of the same existence check, WITHOUT the write path's own
# FOR UPDATE lock — used only by get_mapping (fix wave, item I-2), which is
# a read and must never block behind, or itself hold, a lock a concurrent
# save_mapping call is waiting on. Same convention api/screening.py's own
# module docstring documents for why each module keeps its own copy of this
# query rather than importing another module's private one.
_BUSINESS_PROCESS_EXISTS_SQL = sqlalchemy.text(
    "SELECT 1 FROM privacycare_business_process WHERE id = :id"
)

_VALID_SUBJECTS_SQL = sqlalchemy.text(
    "SELECT fides_key FROM ctl_data_subjects WHERE fides_key = ANY(:keys)"
)

_VALID_CATEGORIES_SQL = sqlalchemy.text(
    "SELECT fides_key FROM ctl_data_categories WHERE fides_key = ANY(:keys)"
)

# fix wave, item C-2: purpose is a taxonomy key like subjects and
# categories, not free text — see this module's own docstring.
_VALID_DATA_USES_SQL = sqlalchemy.text(
    "SELECT fides_key FROM ctl_data_uses WHERE fides_key = ANY(:keys)"
)

_GROUND_SQL = sqlalchemy.text(
    "SELECT id, ground, fides_legal_basis FROM privacycare_processing_ground "
    "WHERE ground = :ground"
)

# Mirrors the top-level privacycare/special_category.py module's
# _TRIGGERING_KEYS_SQL exactly (same ancestor-prefix walk, same tag),
# parameterised on the categories about to be
# WRITTEN rather than on an already-persisted declaration's own
# data_categories column, because this runs BEFORE the row exists (on
# create) or as part of the same statement's value computation (on update)
# — there is no declaration id to read back yet on the create path.
_SPECIAL_CATEGORY_MATCH_SQL = sqlalchemy.text(
    """
    WITH declared_keys AS (
        SELECT unnest(CAST(:categories AS varchar[])) AS declared_key
    ),
    prefixes AS (
        SELECT dk.declared_key,
               array_to_string(
                   (string_to_array(dk.declared_key, '.'))[1:n], '.'
               ) AS ancestor_key
        FROM declared_keys dk,
             generate_series(
                 1, array_length(string_to_array(dk.declared_key, '.'), 1)
             ) AS n
    )
    SELECT DISTINCT p.declared_key
    FROM prefixes p
    JOIN ctl_data_categories cc ON cc.fides_key = p.ancestor_key
    WHERE :tag = ANY(cc.tags)
    """
)

# The activity, if any, that THIS ROUTE previously created for this
# process — see this module's own docstring for why this is the only thing
# idempotency may key off of. created_at/id tie-break so a defensive
# duplicate could never make this pick a different row from one call to the
# next.
_EXISTING_ROUTE_ACTIVITY_SQL = sqlalchemy.text(
    "SELECT pd.id AS declaration_id, pd.system_id AS system_id "
    "FROM privacycare_process_declaration link "
    "JOIN privacydeclaration pd ON pd.id = link.privacy_declaration_id "
    "WHERE link.business_process_id = :business_process_id "
    "AND :marker = ANY(pd.features) "
    "ORDER BY pd.created_at, pd.id LIMIT 1"
)

# ANY existing activity's system, marker or not — used only to decide which
# system a BRAND NEW activity for this process should join, so this route's
# own second activity on a process lands on the same system as the first
# rather than spawning a needless second placeholder system.
_ANY_SYSTEM_FOR_PROCESS_SQL = sqlalchemy.text(
    "SELECT pd.system_id "
    "FROM privacycare_process_declaration link "
    "JOIN privacydeclaration pd ON pd.id = link.privacy_declaration_id "
    "WHERE link.business_process_id = :business_process_id "
    "ORDER BY pd.created_at, pd.id LIMIT 1"
)

# fix wave, item C-1: organization_fides_key and system_type are both
# required, non-Optional str on fideslang's System model — omitting them
# (the pre-fix shape) produced a row GET /api/v1/system's own
# BasicSystemResponse cannot validate, 500ing the WHOLE list. See this
# module's own docstring for the measured live evidence.
_INSERT_SYSTEM_SQL = sqlalchemy.text(
    "INSERT INTO ctl_systems "
    "(id, fides_key, name, description, tags, organization_fides_key, system_type) "
    "VALUES (:id, :fides_key, :name, :description, :tags, "
    " :organization_fides_key, :system_type) "
    "ON CONFLICT (fides_key) DO NOTHING"
)

_SYSTEM_ID_BY_FIDES_KEY_SQL = sqlalchemy.text(
    "SELECT id FROM ctl_systems WHERE fides_key = :fides_key"
)

_INSERT_DECLARATION_SQL = sqlalchemy.text(
    "INSERT INTO privacydeclaration "
    "(id, name, data_use, data_categories, data_subjects, "
    " legal_basis_for_processing, retention_period, third_parties, "
    " data_shared_with_third_parties, system_id, features, "
    " processes_special_category_data) "
    "VALUES (:id, :name, :data_use, :data_categories, :data_subjects, "
    " :legal_basis, :retention_period, :third_parties, "
    " :data_shared_with_third_parties, :system_id, :features, "
    " :processes_special_category_data)"
)

# COALESCE(:field, column) leaves a column exactly as it was when the
# matching request field is None ("not answered this call") — see this
# module's own docstring for why that is the rule for every field except
# name/data_categories/processes_special_category_data, which always fully
# replace because they are always supplied (schema-required).
_UPDATE_DECLARATION_SQL = sqlalchemy.text(
    """
    UPDATE privacydeclaration SET
        name = :name,
        data_categories = :data_categories,
        processes_special_category_data = :processes_special_category_data,
        data_subjects = COALESCE(:data_subjects, data_subjects),
        legal_basis_for_processing = COALESCE(:legal_basis, legal_basis_for_processing),
        data_use = COALESCE(:data_use, data_use),
        retention_period = COALESCE(:retention_period, retention_period),
        third_parties = COALESCE(:third_parties, third_parties),
        data_shared_with_third_parties = CASE
            WHEN :third_parties IS NOT NULL THEN :data_shared_with_third_parties
            ELSE data_shared_with_third_parties
        END,
        updated_at = now()
    WHERE id = :id
    """
)

_INSERT_LINK_SQL = sqlalchemy.text(
    "INSERT INTO privacycare_process_declaration "
    "(id, business_process_id, privacy_declaration_id) "
    "VALUES (:id, :business_process_id, :privacy_declaration_id)"
)

# Read back after every write so the response reflects the CANONICAL
# persisted state rather than an echo of this call's own request fields —
# an UPDATE's COALESCE means a field this call left as None may already
# carry a value a previous call set, and the response must show that value,
# not None.
_SELECT_DECLARATION_SQL = sqlalchemy.text(
    "SELECT name, data_use, data_categories, data_subjects, "
    "       legal_basis_for_processing, retention_period, third_parties, "
    "       processes_special_category_data, system_id "
    "FROM privacydeclaration WHERE id = :id"
)


def _unknown_values(db: Session, sql, keys: List[str]) -> List[str]:
    valid = {row[0] for row in db.execute(sql, {"keys": keys}).all()}
    return sorted(k for k in keys if k not in valid)


def _is_special_category(db: Session, data_categories: List[str]) -> bool:
    if not data_categories:
        return False
    row = db.execute(
        _SPECIAL_CATEGORY_MATCH_SQL,
        {"categories": data_categories, "tag": SPECIAL_TAG},
    ).first()
    return row is not None


def existing_system_id_for_process(db: Session, business_process_id: str) -> Optional[str]:
    """The system id ANY existing activity linked to this process already
    uses, or None if the process has none yet. PUBLIC (no leading
    underscore) — api/screening.py's own system-write authorisation
    dependency (fix round 2, item I-3) calls this to resolve which
    ctl_systems row a caller would need SYSTEM_UPDATE rights on, using the
    EXACT SAME query _system_id_for_process itself reads from, so the two
    can never disagree about which system a mapping call is about to touch."""
    return db.execute(
        _ANY_SYSTEM_FOR_PROCESS_SQL, {"business_process_id": business_process_id}
    ).scalar()


def _system_id_for_process(db: Session, business_process_id: str, process_name: str) -> str:
    existing = existing_system_id_for_process(db, business_process_id)
    if existing is not None:
        return existing

    fides_key = f"privacycare_process_{business_process_id}"
    db.execute(
        _INSERT_SYSTEM_SQL,
        {
            "id": f"sys_{uuid.uuid4().hex[:12]}",
            "fides_key": fides_key,
            "name": process_name,
            "description": (
                f"Auto-created by the PrivacyCare data-mapping route for "
                f"business process {business_process_id!r} — no scanned "
                f"Fides system exists for it yet."
            ),
            # Identifiable and removable later — same reasoning as the
            # activity marker. Only ever set on the row THIS INSERT creates;
            # a reused, real customer system (e.g. the two live activities'
            # own ctl_ef9cadb3-... system) is never tagged by this code path.
            "tags": [MAPPING_ROUTE_FEATURE_MARKER],
            # fix wave, item C-1: both required, non-Optional str on
            # fideslang's System model — see _INSERT_SYSTEM_SQL's own
            # comment and this module's docstring.
            "organization_fides_key": MAPPING_ROUTE_ORGANIZATION_FIDES_KEY,
            "system_type": MAPPING_ROUTE_SYSTEM_TYPE,
        },
    )
    return db.execute(_SYSTEM_ID_BY_FIDES_KEY_SQL, {"fides_key": fides_key}).scalar()


def save_mapping(
    db: Session,
    *,
    business_process_id: str,
    name: str,
    data_categories: List[str],
    recorded_by: str,
    data_subjects: Optional[List[str]] = None,
    ground: Optional[str] = None,
    purpose: Optional[str] = None,
    retention_period: Optional[str] = None,
    third_parties: Optional[str] = None,
) -> MappingResult:
    """Creates or updates the ONE activity this route owns for this business
    process, from her own vocabularies. Raises ValueError, naming the
    offending id/value, for: an unknown business_process_id ("no such
    business process: ..." — the same prefix screening.py's existing
    ValueError-to-404 mapping already matches on, reused here rather than
    duplicated); an unknown data subject or data category; an unknown
    ground; or a real ground with no fides_legal_basis determined yet.

    `recorded_by` names who is recording the ground's provenance — passed
    straight through to grounds.py's own `_record_declaration_ground` (see
    this module's own docstring, "the provenance is written, not just the
    derived value"). Not persisted anywhere on `privacydeclaration` itself
    (that table has no created_by column); only on `privacycare_
    declaration_ground`, and only when `ground` is given this call.
    """
    process_name = db.execute(
        _BUSINESS_PROCESS_NAME_SQL, {"id": business_process_id}
    ).scalar()
    if process_name is None:
        raise ValueError(f"no such business process: {business_process_id!r}")

    if not name or not name.strip():
        raise ValueError("a name is required to save a mapping")

    categories = sorted(set(data_categories))
    if not categories:
        raise ValueError("at least one data category is required to save a mapping")
    unknown_categories = _unknown_values(db, _VALID_CATEGORIES_SQL, categories)
    if unknown_categories:
        raise ValueError(
            f"unknown data category value(s): {', '.join(unknown_categories)}"
        )

    subjects: Optional[List[str]] = None
    if data_subjects is not None:
        subjects = sorted(set(data_subjects))
        unknown_subjects = _unknown_values(db, _VALID_SUBJECTS_SQL, subjects)
        if unknown_subjects:
            raise ValueError(
                f"unknown data subject(s): {', '.join(unknown_subjects)}"
            )

    # fix wave, item C-2: purpose is a taxonomy key (ctl_data_uses), not
    # free text — see this module's own docstring, "`purpose` IS ALSO A
    # TAXONOMY KEY, NOT PROSE". Only checked when given this call; None
    # ("not answered") is not a value to validate, and takes the
    # UNSPECIFIED_DATA_USE sentinel branch further down instead.
    if purpose is not None:
        unknown_purpose = _unknown_values(db, _VALID_DATA_USES_SQL, [purpose])
        if unknown_purpose:
            raise ValueError(f"unknown purpose value: {purpose!r}")

    fides_legal_basis: Optional[str] = None
    ground_id: Optional[str] = None
    if ground is not None:
        ground_row = db.execute(_GROUND_SQL, {"ground": ground}).mappings().first()
        if ground_row is None:
            raise ValueError(f"unknown lawful basis ground: {ground!r}")
        fides_legal_basis = ground_row["fides_legal_basis"]
        if fides_legal_basis is None:
            raise ValueError(
                f"no legal basis has been determined yet for ground {ground!r}"
            )
        ground_id = ground_row["id"]

    processes_special_category_data = _is_special_category(db, categories)

    existing = db.execute(
        _EXISTING_ROUTE_ACTIVITY_SQL,
        {"business_process_id": business_process_id, "marker": MAPPING_ROUTE_FEATURE_MARKER},
    ).mappings().first()

    if existing is not None:
        declaration_id = existing["declaration_id"]
        system_id = existing["system_id"]
        db.execute(
            _UPDATE_DECLARATION_SQL,
            {
                "id": declaration_id,
                "name": name,
                "data_categories": categories,
                "processes_special_category_data": processes_special_category_data,
                "data_subjects": subjects,
                "legal_basis": fides_legal_basis,
                "data_use": purpose,
                "retention_period": retention_period,
                "third_parties": third_parties,
                "data_shared_with_third_parties": bool(third_parties) if third_parties is not None else None,
            },
        )
        created = False
    else:
        system_id = _system_id_for_process(db, business_process_id, process_name)
        declaration_id = f"pri_{uuid.uuid4()}"
        db.execute(
            _INSERT_DECLARATION_SQL,
            {
                "id": declaration_id,
                "name": name,
                "data_use": purpose if purpose is not None else UNSPECIFIED_DATA_USE,
                "data_categories": categories,
                "data_subjects": subjects or [],
                "legal_basis": fides_legal_basis,
                "retention_period": retention_period,
                "third_parties": third_parties,
                "data_shared_with_third_parties": bool(third_parties),
                "system_id": system_id,
                "features": [MAPPING_ROUTE_FEATURE_MARKER],
                "processes_special_category_data": processes_special_category_data,
            },
        )
        db.execute(
            _INSERT_LINK_SQL,
            {
                "id": f"pd_{uuid.uuid4().hex[:12]}",
                "business_process_id": business_process_id,
                "privacy_declaration_id": declaration_id,
            },
        )
        created = True

    if ground_id is not None:
        # Reused verbatim from grounds.py — see this module's own docstring,
        # "the provenance is written, not just the derived value". Safe to
        # call here: the activity's own legal_basis_for_processing was just
        # written, above, from this SAME ground, in this SAME transaction,
        # so _record_declaration_ground's own consistency check (the
        # declaration's stored class must equal the ground's) passes by
        # construction rather than by luck.
        _record_declaration_ground(
            db, declaration_id=declaration_id, ground_id=ground_id, recorded_by=recorded_by
        )

    # Canonical state, not an echo of this call's own request fields — see
    # _SELECT_DECLARATION_SQL's own comment for why that matters on update.
    row = db.execute(_SELECT_DECLARATION_SQL, {"id": declaration_id}).mappings().first()
    return _result_from_row(
        business_process_id, declaration_id, row, ground=ground, created=created
    )


def _result_from_row(
    business_process_id: str,
    declaration_id: str,
    row,
    *,
    ground: Optional[str],
    created: bool,
) -> MappingResult:
    """Builds a MappingResult from a _SELECT_DECLARATION_SQL row — the ONE
    place that shape is assembled, used by both save_mapping (fresh off its
    own write, in the same transaction) and get_mapping (fix wave, item
    I-2, a pure read with no write of its own) so the two can never
    disagree about what a mapping looks like."""
    persisted_purpose = row["data_use"]
    if persisted_purpose == UNSPECIFIED_DATA_USE:
        persisted_purpose = None

    return MappingResult(
        business_process_id=business_process_id,
        privacy_declaration_id=declaration_id,
        system_id=row["system_id"],
        name=row["name"],
        data_subjects=list(row["data_subjects"] or []),
        data_categories=list(row["data_categories"] or []),
        # The ground's own TEXT is not stored anywhere on privacydeclaration
        # — only its derived fides_legal_basis is. This echoes what THIS
        # call was given (None when this call did not name one, even if an
        # earlier call already set the legal basis, or — on the get_mapping
        # read path — always, since a read never "names" a ground at all);
        # fides_legal_basis below always reflects the current persisted,
        # derived value.
        ground=ground,
        fides_legal_basis=row["legal_basis_for_processing"],
        purpose=persisted_purpose,
        retention_period=row["retention_period"],
        third_parties=row["third_parties"],
        processes_special_category_data=row["processes_special_category_data"],
        created=created,
    )


def get_mapping(db: Session, business_process_id: str) -> Optional[MappingResult]:
    """Reads back the ONE activity THIS ROUTE owns for this business
    process (fix wave, item I-2) — or None when it has none. Without this,
    the screen design's promise that a partial mapping can be "saved and
    returned to" is impossible to keep, and a privacy officer who re-types
    a mapping they cannot see would silently create a THIRD activity (see
    this module's own docstring, "IDEMPOTENCY IS KEYED TO THE ACTIVITY").

    Raises ValueError("no such business process: ...") for an unknown
    business_process_id — the same message and prefix save_mapping and
    gate.record_decision already use, so api/screening.py's existing
    ValueError-to-404 mapping handles this with no new branch.

    Returns None, not an error, in TWO different real situations a caller
    must be able to tell apart from a 404: a business process that has
    simply never been mapped at all, and one that carries only activities
    this route does not own (a pre-existing real one with no marker) —
    api/screening.py's own route surfaces both as a 200 with an explicit
    null, same "unmapped is not an error" contract gate.py's own
    current_verdict already keeps for screening decisions.

    Uses _EXISTING_ROUTE_ACTIVITY_SQL with NO lock — a read must never
    block behind, or itself hold, the FOR UPDATE lock save_mapping takes on
    the business process row (fix round 2, item I-2); the two mustn't be
    able to deadlock against each other, and a GET is not the caller who
    gets to decide create-vs-update, so it has nothing to protect by
    locking.
    """
    exists = db.execute(
        _BUSINESS_PROCESS_EXISTS_SQL, {"id": business_process_id}
    ).first()
    if exists is None:
        raise ValueError(f"no such business process: {business_process_id!r}")

    existing = db.execute(
        _EXISTING_ROUTE_ACTIVITY_SQL,
        {"business_process_id": business_process_id, "marker": MAPPING_ROUTE_FEATURE_MARKER},
    ).mappings().first()
    if existing is None:
        return None

    declaration_id = existing["declaration_id"]
    row = db.execute(_SELECT_DECLARATION_SQL, {"id": declaration_id}).mappings().first()
    # ground=None: a read never "names" a ground this call — see
    # _result_from_row's own comment on this field. created=False: a read
    # never creates or updates anything.
    return _result_from_row(business_process_id, declaration_id, row, ground=None, created=False)
