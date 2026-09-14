# Loads the Kenyan taxonomy (taxonomy/kenyan.py) into Fides' own tables plus
# our privacycare_taxonomy_mapping / privacycare_processing_ground audit
# trail. Every statement here is raw parameterised SQL via sqlalchemy.text()
# — never Fides' Base.create_or_update / create / update / delete, which
# commit internally (D-KT rule: nothing in this module may commit; the
# caller's session boundary decides that). Idempotent: reload changes
# nothing (ON CONFLICT DO NOTHING / merge-only updates).
import json
import uuid
from dataclasses import dataclass

import sqlalchemy
from fideslang.models import DataCategory, DataSubject
from sqlalchemy.orm import Session

from fides.api.privacycare.taxonomy import kenyan

# The 9 reused subjects' original Fides names, so revert can put them back.
ORIGINAL_NAMES = {
    "customer": "Customer",
    "employee": "Employee",
    "consultant": "Consultant",
    "prospect": "Prospect",
    "shareholder": "Shareholder",
    "supplier_vendor": "Supplier/Vendor",
    "visitor": "Visitor",
    "next_of_kin": "Next of Kin",
    "job_applicant": "Job Applicant",
}


def _uuid() -> str:
    return str(uuid.uuid4())


@dataclass(frozen=True)
class LoadSummary:
    subjects_reused: int
    subjects_created: int
    subjects_skipped: int
    categories_created: int
    categories_tagged: int  # distinct rows, not terms
    categories_skipped: int
    grounds_loaded: int
    grounds_unmapped: int
    mapping_rows: int
    deferred: list[tuple[str, str, str]]  # (taxonomy, term, owner)


def _validate_creates() -> None:
    """D-KT-2: fideslang rejects a mismatched parent and a parallel root.
    Prove every row we intend to create is one fideslang would accept BEFORE
    any database is touched — a ValidationError here aborts the whole load.

    F9/F11: this gate previously constructed a lookalike row (no
    version_added, no tags, no active, no organization_fides_key) rather
    than the row the INSERT actually writes — which is exactly how two
    defects got past it: the INSERT set version_added='3.1.3' on an
    is_default=False row (fideslang's has_versioning_if_default rejects
    that), and it left organization_fides_key NULL when every row
    fideslang serializes must carry a valid organization_fides_key string
    (F11 — 'default_organization' is the only row in ctl_organizations and
    what all 15/85 Fides default rows carry). _validate_creates() never
    noticed either because it never passed these fields at all. Every
    field below is copied from the corresponding INSERT's VALUES so the
    gate validates the row we actually write, not a stand-in for it."""
    for c in kenyan.CATEGORIES:
        if c.action == "create":
            tags = ["privacycare:kenyan"] + ([kenyan.SPECIAL_TAG] if c.special else [])
            DataCategory(
                fides_key=c.fides_key,
                parent_key=c.parent_key,
                name=c.term,
                description=c.reason,
                is_default=False,
                active=True,
                tags=tags,
                version_added=None,
                organization_fides_key="default_organization",
            )
    for s in kenyan.SUBJECTS:
        if s.action == "create":
            DataSubject(
                fides_key=s.fides_key,
                name=s.term,
                description=s.reason,
                is_default=False,
                rights=kenyan.SIX_RIGHTS,
                active=True,
                tags=["privacycare:kenyan"],
                version_added=None,
                organization_fides_key="default_organization",
            )


def _load_subjects(db: Session) -> None:
    rights_json = json.dumps(kenyan.SIX_RIGHTS)
    for s in kenyan.SUBJECTS:
        if s.action == "reuse":
            db.execute(
                sqlalchemy.text(
                    "UPDATE ctl_data_subjects SET name = :term, "
                    "rights = CAST(:rights AS json) "
                    "WHERE fides_key = :key AND is_default = true"
                ),
                {"term": s.term, "rights": rights_json, "key": s.fides_key},
            )
        elif s.action == "create":
            db.execute(
                sqlalchemy.text(
                    # F9: version_added stays NULL here — fideslang's own
                    # has_versioning_if_default (validation.py) rejects a
                    # non-default (is_default=false) row that carries any
                    # version information, so a created row must not set
                    # one; the column has no NOT NULL constraint.
                    # F11: organization_fides_key must be 'default_organization'
                    # — it's the only row in ctl_organizations, and every
                    # Fides default row carries it; fideslang's response
                    # model requires it be a non-null string.
                    "INSERT INTO ctl_data_subjects "
                    "(id, fides_key, name, description, rights, is_default, active, tags, "
                    "organization_fides_key) "
                    "VALUES (:id, :key, :term, :reason, CAST(:rights AS json), false, true, "
                    "ARRAY['privacycare:kenyan'], 'default_organization') "
                    "ON CONFLICT (fides_key) DO NOTHING"
                ),
                {
                    "id": _uuid(),
                    "key": s.fides_key,
                    "term": s.term,
                    "reason": s.reason,
                    "rights": rights_json,
                },
            )
        # dropped: mapping row only, handled below.

        db.execute(
            sqlalchemy.text(
                "INSERT INTO privacycare_taxonomy_mapping "
                "(id, taxonomy, customer_term, fides_key, action, reason, source_document, "
                "names_person_directly, natural_person_role, appears_via, deferred_to) "
                "VALUES (:id, 'data_subject', :term, :key, :action, :reason, :source, "
                ":names_person_directly, :role, NULL, NULL) "
                "ON CONFLICT (taxonomy, customer_term) DO UPDATE SET "
                "fides_key = EXCLUDED.fides_key, action = EXCLUDED.action, reason = EXCLUDED.reason, "
                "names_person_directly = EXCLUDED.names_person_directly, "
                "natural_person_role = EXCLUDED.natural_person_role, "
                "deferred_to = EXCLUDED.deferred_to"
            ),
            {
                "id": _uuid(),
                "term": s.term,
                "key": s.fides_key,
                "action": s.action,
                "reason": s.reason,
                "source": kenyan.SOURCE,
                "names_person_directly": s.names_person_directly,
                "role": s.natural_person_role,
            },
        )


def _load_categories(db: Session) -> None:
    for c in kenyan.CATEGORIES:
        if c.action == "tag":
            db.execute(
                sqlalchemy.text(
                    "UPDATE ctl_data_categories SET tags = array_append(coalesce(tags, '{}'), :tag) "
                    "WHERE fides_key = :key AND NOT (:tag = ANY(coalesce(tags, '{}')))"
                ),
                {"tag": kenyan.SPECIAL_TAG, "key": c.fides_key},
            )
        elif c.action == "create":
            tags = ["privacycare:kenyan"] + ([kenyan.SPECIAL_TAG] if c.special else [])
            db.execute(
                sqlalchemy.text(
                    # F9: version_added stays NULL for created rows — see
                    # the identical note in _load_subjects above.
                    # F11: organization_fides_key = 'default_organization' —
                    # see the identical note in _load_subjects above.
                    "INSERT INTO ctl_data_categories "
                    "(id, fides_key, name, description, parent_key, is_default, active, tags, "
                    "organization_fides_key) "
                    "VALUES (:id, :key, :term, :reason, :parent_key, false, true, :tags, "
                    "'default_organization') "
                    "ON CONFLICT (fides_key) DO NOTHING"
                ),
                {
                    "id": _uuid(),
                    "key": c.fides_key,
                    "term": c.term,
                    "reason": c.reason,
                    "parent_key": c.parent_key,
                    "tags": tags,
                },
            )
        # reuse / not_personal_data / deferred: mapping row only.

        db.execute(
            sqlalchemy.text(
                "INSERT INTO privacycare_taxonomy_mapping "
                "(id, taxonomy, customer_term, fides_key, action, reason, source_document, "
                "names_person_directly, natural_person_role, appears_via, deferred_to) "
                "VALUES (:id, 'data_category', :term, :key, :action, :reason, :source, "
                "NULL, NULL, NULL, :deferred_to) "
                "ON CONFLICT (taxonomy, customer_term) DO UPDATE SET "
                "fides_key = EXCLUDED.fides_key, action = EXCLUDED.action, reason = EXCLUDED.reason, "
                "names_person_directly = EXCLUDED.names_person_directly, "
                "natural_person_role = EXCLUDED.natural_person_role, "
                "deferred_to = EXCLUDED.deferred_to"
            ),
            {
                "id": _uuid(),
                "term": c.term,
                "key": c.fides_key,
                "action": c.action,
                "reason": c.reason,
                "source": kenyan.SOURCE,
                "deferred_to": c.deferred_to,
            },
        )


def _load_grounds(db: Session) -> None:
    for g in kenyan.GROUNDS:
        db.execute(
            sqlalchemy.text(
                "INSERT INTO privacycare_processing_ground "
                "(id, ground, source_class, fides_legal_basis, suggested_fides_legal_basis, provenance) "
                "VALUES (:id, :ground, :source_class, :fides_legal_basis, :suggested, :source) "
                "ON CONFLICT (ground) DO UPDATE SET "
                "source_class = EXCLUDED.source_class, "
                "suggested_fides_legal_basis = EXCLUDED.suggested_fides_legal_basis, "
                "fides_legal_basis = COALESCE(privacycare_processing_ground.fides_legal_basis, EXCLUDED.fides_legal_basis)"
            ),
            {
                "id": _uuid(),
                "ground": g.ground,
                "source_class": g.source_class,
                "fides_legal_basis": g.fides_legal_basis,
                "suggested": g.suggested,
                "source": kenyan.SOURCE,
            },
        )


_SELECT_CREATED_CATEGORIES_SQL = sqlalchemy.text(
    "SELECT fides_key, name, description, parent_key FROM ctl_data_categories "
    "WHERE fides_key = ANY(:keys)"
)

_SELECT_CREATED_SUBJECTS_SQL = sqlalchemy.text(
    "SELECT fides_key, name, description FROM ctl_data_subjects "
    "WHERE fides_key = ANY(:keys)"
)


def _verify_created_rows(db: Session) -> None:
    """I4: the INSERTs above are ON CONFLICT DO NOTHING, so editing a created
    row's name/description/parent_key in kenyan.py and re-running the loader
    leaves the database on the OLD content while
    privacycare_taxonomy_mapping (an upsert) records the new decision — the
    two halves of D-KT-7's audit trail diverge, silently, and LoadSummary
    cannot see it because it is computed from the module rather than from
    rowcounts (Ruling F1).

    So read every created key back and compare it to what kenyan.py says it
    should be. A mismatch is not a load that half-worked; it is a database
    the module no longer describes, and the remedy (documented in
    scripts/privacycare/load_taxonomy.py) is `--revert --commit` then
    `--commit`, subject to the revert guard below. A missing key counts as
    drift too: it means the INSERT did not take and nothing else would say
    so."""
    drifted: list[str] = []

    wanted_categories = {
        c.fides_key: (c.term, c.reason, c.parent_key)
        for c in kenyan.CATEGORIES
        if c.action == "create"
    }
    live_categories = {
        row["fides_key"]: (row["name"], row["description"], row["parent_key"])
        for row in db.execute(
            _SELECT_CREATED_CATEGORIES_SQL, {"keys": list(wanted_categories)}
        ).mappings()
    }
    for key, wanted in wanted_categories.items():
        if live_categories.get(key) != wanted:
            drifted.append(f"data_category {key}")

    wanted_subjects = {
        s.fides_key: (s.term, s.reason)
        for s in kenyan.SUBJECTS
        if s.action == "create"
    }
    live_subjects = {
        row["fides_key"]: (row["name"], row["description"])
        for row in db.execute(
            _SELECT_CREATED_SUBJECTS_SQL, {"keys": list(wanted_subjects)}
        ).mappings()
    }
    for key, wanted_subject in wanted_subjects.items():
        if live_subjects.get(key) != wanted_subject:
            drifted.append(f"data_subject {key}")

    if drifted:
        raise ValueError(
            "kenyan.py and the database disagree about "
            f"{len(drifted)} created row(s): {', '.join(sorted(drifted))}. "
            "The taxonomy INSERTs are ON CONFLICT DO NOTHING, so a content "
            "change to an already-loaded key never reaches the database: "
            "revert the load (--revert --commit) and load it again."
        )


def load_kenyan_taxonomy(db: Session) -> LoadSummary:
    """Idempotent; never commits — the caller's session boundary decides
    that. Returns a LoadSummary computed from kenyan.SUBJECTS/CATEGORIES/
    GROUNDS (what the loader guarantees exists after it runs), not from
    statement rowcounts — those are 0 on a reload because of ON CONFLICT DO
    NOTHING, and the summary must stay meaningful either way."""
    _validate_creates()

    _load_subjects(db)
    _load_categories(db)
    _load_grounds(db)
    _verify_created_rows(db)

    subjects_reused = sum(s.action == "reuse" for s in kenyan.SUBJECTS)
    subjects_created = sum(s.action == "create" for s in kenyan.SUBJECTS)
    subjects_skipped = sum(s.action == "dropped" for s in kenyan.SUBJECTS)

    categories_created = sum(c.action == "create" for c in kenyan.CATEGORIES)
    categories_tagged = len({c.fides_key for c in kenyan.CATEGORIES if c.action == "tag"})
    categories_skipped = sum(
        c.action in ("reuse", "not_personal_data", "deferred") for c in kenyan.CATEGORIES
    )

    grounds_loaded = len(kenyan.GROUNDS)
    grounds_unmapped = sum(g.fides_legal_basis is None for g in kenyan.GROUNDS)

    mapping_rows = len(kenyan.SUBJECTS) + len(kenyan.CATEGORIES)

    # Subject.action is never "deferred" (see kenyan.py); only categories
    # carry a deferred_to owner. Category.deferred_to is typed Optional[str]
    # (it's None for every non-deferred row), so a deferred row with no owner
    # would be a data defect in kenyan.py, not something to pass through as a
    # silent None — narrow explicitly and raise rather than let mypy (or a
    # future silent None) paper over it.
    deferred: list[tuple[str, str, str]] = []
    for c in kenyan.CATEGORIES:
        if c.action != "deferred":
            continue
        if c.deferred_to is None:
            raise ValueError(
                f"category {c.fides_key!r} (term {c.term!r}) is deferred but has no deferred_to owner"
            )
        deferred.append(("data_category", c.term, c.deferred_to))

    return LoadSummary(
        subjects_reused=subjects_reused,
        subjects_created=subjects_created,
        subjects_skipped=subjects_skipped,
        categories_created=categories_created,
        categories_tagged=categories_tagged,
        categories_skipped=categories_skipped,
        grounds_loaded=grounds_loaded,
        grounds_unmapped=grounds_unmapped,
        mapping_rows=mapping_rows,
        deferred=deferred,
    )


_COUNT_DECLARATIONS_USING_CREATED_KEYS_SQL = sqlalchemy.text(
    "SELECT count(*) FROM privacydeclaration d WHERE EXISTS ("
    "  SELECT 1 FROM unnest(d.data_categories) k "
    "  JOIN ctl_data_categories c ON c.fides_key = k "
    "  WHERE c.is_default = false AND 'privacycare:kenyan' = ANY(c.tags)"
    ") OR EXISTS ("
    "  SELECT 1 FROM unnest(d.data_subjects) k "
    "  JOIN ctl_data_subjects s ON s.fides_key = k "
    "  WHERE s.is_default = false AND 'privacycare:kenyan' = ANY(s.tags)"
    ")"
)


def count_declarations_referencing_created_keys(db: Session) -> int:
    """How many privacydeclaration rows name a category or subject key this
    loader created. privacydeclaration.data_categories / .data_subjects are
    text arrays with no foreign key, so deleting a taxonomy row a
    declaration names always succeeds and leaves the declaration naming a
    key nobody can resolve — at which point Fides' own
    validate_privacy_declarations (db/system.py) rejects the next save of
    that system and the system becomes un-editable in the admin UI. Read-only;
    the number is the whole point of the guard in revert_kenyan_taxonomy."""
    return db.execute(_COUNT_DECLARATIONS_USING_CREATED_KEYS_SQL).scalar() or 0


def revert_kenyan_taxonomy(db: Session, *, force: bool = False) -> None:
    """Deletes the is_default=false rows we created, strips SPECIAL_TAG from
    is_default=true rows, restores the 9 reused names + nulls their rights,
    and deletes our mapping rows. Leaves privacycare_processing_ground alone
    — declarations may reference it.

    Refuses to delete anything (ValueError) while a privacydeclaration names
    one of the created keys, unless `force=True` says the caller accepts
    leaving those declarations pointing at keys that no longer exist. The
    check runs BEFORE the first DELETE, so a refusal leaves the taxonomy
    exactly as it was."""
    if not force:
        in_use = count_declarations_referencing_created_keys(db)
        if in_use:
            raise ValueError(
                f"{in_use} privacy declaration(s) still name a data category "
                "or data subject this loader created; reverting would leave "
                "them pointing at keys that no longer exist and Fides would "
                "reject the next save of those systems. Re-point or delete "
                "those declarations first, or pass force=True (--force) to "
                "revert anyway."
            )
    db.execute(
        sqlalchemy.text(
            "DELETE FROM ctl_data_categories WHERE 'privacycare:kenyan' = ANY(tags) AND is_default = false"
        )
    )
    db.execute(
        sqlalchemy.text(
            "DELETE FROM ctl_data_subjects WHERE 'privacycare:kenyan' = ANY(tags) AND is_default = false"
        )
    )
    db.execute(
        sqlalchemy.text(
            "UPDATE ctl_data_categories SET tags = array_remove(tags, :tag) WHERE is_default = true"
        ),
        {"tag": kenyan.SPECIAL_TAG},
    )
    for key, name in ORIGINAL_NAMES.items():
        db.execute(
            sqlalchemy.text(
                "UPDATE ctl_data_subjects SET name = :name, rights = 'null'::json "
                "WHERE fides_key = :key AND is_default = true"
            ),
            {"name": name, "key": key},
        )
    db.execute(
        sqlalchemy.text(
            "DELETE FROM privacycare_taxonomy_mapping WHERE source_document = :source"
        ),
        {"source": kenyan.SOURCE},
    )
