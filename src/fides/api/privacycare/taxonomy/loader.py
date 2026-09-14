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
    any database is touched — a ValidationError here aborts the whole load."""
    for c in kenyan.CATEGORIES:
        if c.action == "create":
            DataCategory(
                fides_key=c.fides_key,
                parent_key=c.parent_key,
                name=c.term,
                description=c.reason,
                is_default=False,
            )
    for s in kenyan.SUBJECTS:
        if s.action == "create":
            DataSubject(
                fides_key=s.fides_key,
                name=s.term,
                description=s.reason,
                is_default=False,
                rights=kenyan.SIX_RIGHTS,
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
                    "INSERT INTO ctl_data_subjects "
                    "(id, fides_key, name, description, rights, is_default, active, tags, version_added) "
                    "VALUES (:id, :key, :term, :reason, CAST(:rights AS json), false, true, "
                    "ARRAY['privacycare:kenyan'], '3.1.3') "
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
                    "INSERT INTO ctl_data_categories "
                    "(id, fides_key, name, description, parent_key, is_default, active, tags, version_added) "
                    "VALUES (:id, :key, :term, :reason, :parent_key, false, true, :tags, '3.1.3') "
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
    # carry a deferred_to owner.
    deferred: list[tuple[str, str, str]] = [
        ("data_category", c.term, c.deferred_to)
        for c in kenyan.CATEGORIES
        if c.action == "deferred"
    ]

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


def revert_kenyan_taxonomy(db: Session) -> None:
    """Deletes the is_default=false rows we created, strips SPECIAL_TAG from
    is_default=true rows, restores the 9 reused names + nulls their rights,
    and deletes our mapping rows. Leaves privacycare_processing_ground alone
    — declarations may reference it."""
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
