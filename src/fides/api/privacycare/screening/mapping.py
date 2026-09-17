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

VOCABULARIES, NOT FREE TEXT. Every data subject, data category and lawful-
basis ground offered to her was already loaded by taxonomy/loader.py from
her own workbook (D-KT-1..8). "Her 33 data subjects" and "her 29 data
categories" are exactly `ctl_data_subjects`/`ctl_data_categories` WHERE
`is_default = false` — the rows created specifically for her, as opposed to
a handful of her terms that reuse a Fides-shipped default key (e.g.
"Customer/Client" -> the shipped `customer`), which are NOT offered here.
An unknown value in either list is rejected BY NAME (_unknown_values below)
rather than silently dropped, because a silently dropped value records a
mapping the user did not make and cannot see is missing.

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

SPECIAL-CATEGORY DATA IS DERIVED, NOT TICKED. `processes_special_category_
data` is computed from the chosen `data_categories` against the SAME
ancestor-prefix walk over `ctl_data_categories.tags` that
special_category.py's `derive_special_category` already uses for the read
path (SPECIAL_TAG = "dpa2019:special_category") — reused here rather than
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

WHAT HAPPENS WHEN THE PROCESS HAS NO SYSTEM. `privacydeclaration.system_id`
is NOT NULL, but a business process has no system of its own — processes.py
's own module docstring says so explicitly ("Fides' map is anchored on
systems, which can be scanned, while a process exists only once a
consultant has written it down"), and the measured live data bears it out:
84 of the 86 processes have never been linked to any system at all.
Refusing to save a mapping until someone first goes and creates a Fides
system by hand would defeat the entire point of this route — capturing the
mapping AT THE MOMENT someone decides the process matters — for all but the
one process that already happens to have one. So `_system_id_for_process`
first looks for a system already in use by an EXISTING (marker or not)
activity linked to this same process, and only if none exists does it
provision a minimal placeholder `ctl_systems` row scoped to this process
(fides_key `privacycare_process_<id>`, ON CONFLICT DO NOTHING so a raced or
repeated call never errors). This is a PrivacyCare convention, not
something Ethyca or the customer defined, and is named as such in this
task's report.

All raw SQL, all bound parameters, never a commit — the caller's session
boundary decides, same rule as gate.py, risk/register.py and grounds.py.
"""
import uuid
from dataclasses import dataclass
from typing import List, Optional

import sqlalchemy
from sqlalchemy.orm import Session

from fides.api.privacycare.taxonomy.kenyan import SPECIAL_TAG

# Tags every privacydeclaration row this module has ever created. The ONLY
# thing idempotency keys off of — see this module's own docstring for why
# the process id alone is not safe to key off of.
MAPPING_ROUTE_FEATURE_MARKER = "privacycare:mapping_route"

# Written to `data_use` when no `purpose` is supplied yet (a legitimately
# partial mapping — see DataMappingRequest). NOT a taxonomy key: data_use is
# a soft String reference with no FK into ctl_data_uses (context.py's
# build_context already documents that "a declaration may legitimately name
# a custom data use" and resolves an unmatched one to None name/description
# rather than raising), and PrivacyDeclaration.purpose's own hybrid property
# looks up MAPPED_PURPOSES_ONLY_BY_DATA_USE.get(self.data_use) and returns
# None for anything it does not recognise — this sentinel included. Neither
# read path breaks; both simply have nothing to say about it yet, which is
# the truth.
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


_BUSINESS_PROCESS_NAME_SQL = sqlalchemy.text(
    "SELECT name FROM privacycare_business_process WHERE id = :id"
)

_VALID_SUBJECTS_SQL = sqlalchemy.text(
    "SELECT fides_key FROM ctl_data_subjects "
    "WHERE is_default = false AND fides_key = ANY(:keys)"
)

_VALID_CATEGORIES_SQL = sqlalchemy.text(
    "SELECT fides_key FROM ctl_data_categories "
    "WHERE is_default = false AND fides_key = ANY(:keys)"
)

_GROUND_SQL = sqlalchemy.text(
    "SELECT ground, fides_legal_basis FROM privacycare_processing_ground "
    "WHERE ground = :ground"
)

# Mirrors special_category.py's _TRIGGERING_KEYS_SQL exactly (same ancestor-
# prefix walk, same tag), parameterised on the categories about to be
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

_INSERT_SYSTEM_SQL = sqlalchemy.text(
    "INSERT INTO ctl_systems (id, fides_key, name, description) "
    "VALUES (:id, :fides_key, :name, :description) "
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


def _system_id_for_process(db: Session, business_process_id: str, process_name: str) -> str:
    existing = db.execute(
        _ANY_SYSTEM_FOR_PROCESS_SQL, {"business_process_id": business_process_id}
    ).scalar()
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
        },
    )
    return db.execute(_SYSTEM_ID_BY_FIDES_KEY_SQL, {"fides_key": fides_key}).scalar()


def save_mapping(
    db: Session,
    *,
    business_process_id: str,
    name: str,
    data_categories: List[str],
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

    fides_legal_basis: Optional[str] = None
    if ground is not None:
        ground_row = db.execute(_GROUND_SQL, {"ground": ground}).mappings().first()
        if ground_row is None:
            raise ValueError(f"unknown lawful basis ground: {ground!r}")
        fides_legal_basis = ground_row["fides_legal_basis"]
        if fides_legal_basis is None:
            raise ValueError(
                f"no legal basis has been determined yet for ground {ground!r}"
            )

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

    # Canonical state, not an echo of this call's own request fields — see
    # _SELECT_DECLARATION_SQL's own comment for why that matters on update.
    row = db.execute(_SELECT_DECLARATION_SQL, {"id": declaration_id}).mappings().first()
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
        # earlier call already set the legal basis); fides_legal_basis below
        # always reflects the current persisted, derived value.
        ground=ground,
        fides_legal_basis=row["legal_basis_for_processing"],
        purpose=persisted_purpose,
        retention_period=row["retention_period"],
        third_parties=row["third_parties"],
        processes_special_category_data=row["processes_special_category_data"],
        created=created,
    )
