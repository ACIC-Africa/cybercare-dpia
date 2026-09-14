# The ROPA read path: one business process, and everything it processes.
#
# PrivacyCare owns the process and the link. Fides owns the declaration and
# the system. This module joins them without copying either.
from dataclasses import dataclass, field

import sqlalchemy
from sqlalchemy.orm import Session

from fides.api.privacycare.models import BusinessProcess, ProcessDeclaration
from fides.api.privacycare.special_category import derive_special_category

_DECLARATION_SQL = sqlalchemy.text(
    """
    SELECT pd.id, pd.name, pd.data_use, pd.data_categories, pd.data_subjects,
           pd.legal_basis_for_processing, pd.retention_period, pd.system_id,
           s.name AS system_name
    FROM privacydeclaration pd
    LEFT JOIN ctl_systems s ON s.id = pd.system_id
    WHERE pd.id = ANY(:ids)
    ORDER BY pd.id
    """
)

# The literal placeholder for a data subject fides_key with no
# privacycare_taxonomy_mapping row, or a row whose natural_person_role was
# never recorded (D-KT-8: NULL until Carol rules) — never fabricate a role.
NATURAL_PERSON_ROLE_NOT_RECORDED = "natural-person role not yet recorded"

_NATURAL_PERSON_ROLES_SQL = sqlalchemy.text(
    "SELECT fides_key, natural_person_role FROM privacycare_taxonomy_mapping "
    "WHERE taxonomy = 'data_subject' AND fides_key = ANY(:fides_keys)"
)


@dataclass
class RopaDeclaration:
    id: str
    name: str | None
    data_use: str
    data_categories: list[str]
    data_subjects: list[str]
    legal_basis: str | None
    retention_period: str | None
    system_id: str | None
    system_name: str | None
    # Defaulted (not required-positional) so existing call sites that build
    # a RopaDeclaration directly — test_graph_projection.py's fixtures, in
    # particular — keep working unchanged; ropa_for_process itself always
    # passes all three explicitly.
    special_category_derived: bool = False
    special_category_triggering_keys: list[str] = field(default_factory=list)
    special_category_mismatch: bool = False
    # Parallel to data_subjects: natural_person_roles[i] is the role for
    # data_subjects[i], or NATURAL_PERSON_ROLE_NOT_RECORDED when no mapping
    # row exists or its natural_person_role is NULL.
    natural_person_roles: list[str] = field(default_factory=list)


@dataclass
class RopaEntry:
    process: BusinessProcess
    declarations: list[RopaDeclaration] = field(default_factory=list)
    # Ids linked to this process that could not be resolved to a
    # privacydeclaration row right now. This deliberately does not
    # distinguish a declaration that existed and was later deleted from an
    # id that was never valid in the first place — telling those apart
    # would require a write-time existence check on the link, which is out
    # of scope here.
    missing_declarations: list[str] = field(default_factory=list)


def ropa_for_process(db: Session, business_process_id: str) -> RopaEntry:
    # Assemble the ROPA entry for one business process.
    #
    # Raises LookupError if the process does not exist. Links whose id does
    # not resolve to a privacydeclaration row right now are reported in
    # `missing_declarations` rather than dropped — a dangling link is a
    # finding, not a non-event. This does not distinguish a declaration that
    # was deleted after the link was created from an id that was never
    # valid; separating those would require a write-time existence check,
    # which is out of scope here.
    process = db.get(BusinessProcess, business_process_id)
    if process is None:
        raise LookupError(f"no business process with id {business_process_id!r}")

    linked_ids = [
        row.privacy_declaration_id
        for row in db.query(ProcessDeclaration)
        .filter(ProcessDeclaration.business_process_id == business_process_id)
        .all()
    ]
    if not linked_ids:
        return RopaEntry(process=process)

    rows = db.execute(_DECLARATION_SQL, {"ids": linked_ids}).mappings().all()
    found = {r["id"] for r in rows}

    # One query for every data subject key across all of this entry's
    # declarations, rather than one per declaration — natural_person_role is
    # a small lookup table and the keys repeat heavily (e.g. "customer"),
    # unlike special_category below which genuinely needs the declaration's
    # own row.
    all_subject_keys = sorted(
        {key for r in rows for key in (r["data_subjects"] or [])}
    )
    roles_by_key: dict[str, str | None] = {}
    if all_subject_keys:
        role_rows = db.execute(
            _NATURAL_PERSON_ROLES_SQL, {"fides_keys": all_subject_keys}
        ).mappings().all()
        roles_by_key = {rr["fides_key"]: rr["natural_person_role"] for rr in role_rows}

    declarations = []
    for r in rows:
        # N+1 by design here: derive_special_category re-reads the
        # declaration row this loop already has in hand, one extra query per
        # declaration, to keep the ancestor-prefix derivation in its own
        # module rather than duplicating that SQL inline. Acceptable for a
        # ROPA read (bounded by a business process's own declaration count),
        # flagged here rather than left silent.
        special_category = derive_special_category(db, r["id"])
        data_subjects = list(r["data_subjects"] or [])
        declarations.append(
            RopaDeclaration(
                id=r["id"],
                name=r["name"],
                data_use=r["data_use"],
                data_categories=list(r["data_categories"] or []),
                data_subjects=data_subjects,
                legal_basis=r["legal_basis_for_processing"],
                retention_period=r["retention_period"],
                system_id=r["system_id"],
                system_name=r["system_name"],
                special_category_derived=special_category.derived,
                special_category_triggering_keys=list(special_category.triggering_keys),
                special_category_mismatch=special_category.mismatch,
                natural_person_roles=[
                    roles_by_key.get(subject_key) or NATURAL_PERSON_ROLE_NOT_RECORDED
                    for subject_key in data_subjects
                ],
            )
        )
    return RopaEntry(
        process=process,
        declarations=declarations,
        missing_declarations=sorted(set(linked_ids) - found),
    )
