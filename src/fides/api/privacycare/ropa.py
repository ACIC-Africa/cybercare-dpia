# The ROPA read path: one business process, and everything it processes.
#
# PrivacyCare owns the process and the link. Fides owns the declaration and
# the system. This module joins them without copying either.
from dataclasses import dataclass, field

import sqlalchemy
from sqlalchemy.orm import Session

from fides.api.privacycare.models import BusinessProcess, ProcessDeclaration

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
    declarations = [
        RopaDeclaration(
            id=r["id"],
            name=r["name"],
            data_use=r["data_use"],
            data_categories=list(r["data_categories"] or []),
            data_subjects=list(r["data_subjects"] or []),
            legal_basis=r["legal_basis_for_processing"],
            retention_period=r["retention_period"],
            system_id=r["system_id"],
            system_name=r["system_name"],
        )
        for r in rows
    ]
    return RopaEntry(
        process=process,
        declarations=declarations,
        missing_declarations=sorted(set(linked_ids) - found),
    )
