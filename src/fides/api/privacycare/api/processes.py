"""The business-process ROPA surface.

Carol's method, the SOW's "Business Process Assessment and Data Mapping", and
the CVEQ Privacy Resilience Framework's layer 1 all start from a business
process. Fides does not have one — its map is anchored on systems, because
systems can be scanned, while a process exists only once a consultant has
written it down. privacycare/models.py supplies the entity and the link;
ropa.py assembles the entry; until now nothing exposed any of it, so the layer
was real code with no door on it.

NAMESPACE. These routes live under /api/v1/privacycare, NOT under
/api/v1/plus. The assessment routes squat Ethyca's `plus` namespace because
the shipped admin UI calls those exact paths and the UI's path is the
requirement. Nothing in the shipped UI calls THESE, so taking a path in Plus's
namespace would only risk colliding with a real Plus endpoint later. We choose
our own.

RELATIONSHIP TO FIDES' `data-purpose`. Fides ships a DataPurpose entity and a
whole admin-UI section for it (clients/admin-ui/src/pages/data-purposes/),
including the ROPA CSV export, all served by ~14 `plus/data-purpose/*`
endpoints that OSS does not implement. A DataPurpose is a reusable refinement
of a privacy declaration — data_use, data_subject, categories, legal basis,
retention — and is still purpose-centric. It is NOT a business process, and
implementing that family is a separate, much larger piece of work against a
different model. The two are complementary: a process is what the organisation
does; a purpose is why a particular set of data is processed.
"""
from typing import List, Optional

import sqlalchemy
from fastapi import Depends, HTTPException, Security
from fastapi import status as status_codes
from fastapi_pagination import Page, Params, paginate
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from fides.api.deps import get_db
from fides.api.models.client import ClientDetail
from fides.api.oauth.utils import verify_oauth_client
from fides.api.privacycare.api.identity import _created_by_from_client
from fides.api.privacycare.api.router import privacycare_processes_router
from fides.api.privacycare.ropa import ropa_for_process
from fides.common.scope_registry import SYSTEM_READ


class BusinessProcessCreate(BaseModel):
    # No TypeScript counterpart: nothing in the shipped admin UI creates a
    # business process, because Fides has no such concept. Recorded in
    # test_response_model_ts_parity.py's ALLOWLIST with that reason rather
    # than left to look like an oversight.
    name: str = Field(min_length=1)
    description: Optional[str] = None
    business_cycle: Optional[str] = None
    owner_name: Optional[str] = None
    owner_email: Optional[str] = None
    is_critical: bool = False
    criticality_note: Optional[str] = None
    external_ref: Optional[str] = None


class BusinessProcessResponse(BaseModel):
    id: str
    name: str
    description: Optional[str]
    business_cycle: Optional[str]
    owner_name: Optional[str]
    owner_email: Optional[str]
    is_critical: bool
    criticality_note: Optional[str]
    external_ref: Optional[str]
    last_attested_at: Optional[str]
    created_at: Optional[str]
    updated_at: Optional[str]


class LinkDeclarationsRequest(BaseModel):
    # The declarations this process processes data under. Replaces the set
    # rather than appending: a ROPA entry is a statement of what a process
    # does NOW, and an append-only link table would make removing a
    # decommissioned activity impossible through the API.
    privacy_declaration_ids: List[str]


class RopaDeclarationResponse(BaseModel):
    id: str
    name: Optional[str]
    data_use: str
    data_categories: List[str]
    data_subjects: List[str]
    legal_basis: Optional[str]
    retention_period: Optional[str]
    system_id: Optional[str]
    system_name: Optional[str]


class RopaEntryResponse(BaseModel):
    process: BusinessProcessResponse
    declarations: List[RopaDeclarationResponse]
    # Links whose declaration cannot be resolved right now. Surfaced rather
    # than dropped: a dangling link in a record of processing is a finding a
    # DPO has to act on, not a non-event. See ropa.py for why this does not
    # distinguish "deleted since" from "never valid".
    missing_declarations: List[str]


_INSERT_PROCESS_SQL = sqlalchemy.text(
    "INSERT INTO privacycare_business_process "
    "(id, name, description, business_cycle, owner_name, owner_email, "
    " is_critical, criticality_note, external_ref) "
    "VALUES (:id, :name, :description, :business_cycle, :owner_name, "
    " :owner_email, :is_critical, :criticality_note, :external_ref)"
)

_SELECT_PROCESSES_SQL = sqlalchemy.text(
    "SELECT id, name, description, business_cycle, owner_name, owner_email, "
    "       is_critical, criticality_note, external_ref, last_attested_at, "
    "       created_at, updated_at "
    "FROM privacycare_business_process WHERE deleted_at IS NULL "
    "ORDER BY is_critical DESC, name, id"
)

_PROCESS_EXISTS_SQL = sqlalchemy.text(
    "SELECT id FROM privacycare_business_process "
    "WHERE id = :id AND deleted_at IS NULL FOR UPDATE"
)

_DELETE_LINKS_SQL = sqlalchemy.text(
    "DELETE FROM privacycare_process_declaration WHERE business_process_id = :id"
)

_INSERT_LINK_SQL = sqlalchemy.text(
    "INSERT INTO privacycare_process_declaration "
    "(id, business_process_id, privacy_declaration_id) "
    "VALUES (:id, :process_id, :declaration_id)"
)


def _as_str(value) -> Optional[str]:
    return value.isoformat() if value is not None else None


def _process_response(row) -> BusinessProcessResponse:
    return BusinessProcessResponse(
        id=row["id"],
        name=row["name"],
        description=row["description"],
        business_cycle=row["business_cycle"],
        owner_name=row["owner_name"],
        owner_email=row["owner_email"],
        is_critical=row["is_critical"],
        criticality_note=row["criticality_note"],
        external_ref=row["external_ref"],
        last_attested_at=_as_str(row["last_attested_at"]),
        created_at=_as_str(row["created_at"]),
        updated_at=_as_str(row["updated_at"]),
    )


def _create_process(db: Session, request: BusinessProcessCreate) -> str:
    import uuid

    process_id = f"bp_{uuid.uuid4().hex[:12]}"
    db.execute(
        _INSERT_PROCESS_SQL,
        {"id": process_id, **request.model_dump()},
    )
    return process_id


def _list_processes(db: Session) -> List[BusinessProcessResponse]:
    """Critical processes first, then by name.

    The SOW asks the consultant to "identify all the relevant business
    processes ... and prioritize each", up to 15 critical ones, so the
    prioritisation is the point of the list rather than a display preference.
    `id` breaks the tie so two processes with the same name cannot swap order
    between calls.
    """
    rows = db.execute(_SELECT_PROCESSES_SQL).mappings().all()
    return [_process_response(row) for row in rows]


def _link_declarations(
    db: Session, process_id: str, declaration_ids: List[str]
) -> int:
    """Replace this process's declaration links. Returns how many now exist.

    The process row is locked FOR UPDATE first, for the same reason
    write_answer locks its assessment: delete-then-insert is a read-modify-
    write, and two concurrent link calls could otherwise interleave into a set
    that matches neither request.

    Duplicate ids in one request collapse — the link table has a unique
    constraint on (business_process_id, privacy_declaration_id), and asking
    twice for the same link is the same statement, not an error.
    """
    import uuid

    if db.execute(_PROCESS_EXISTS_SQL, {"id": process_id}).first() is None:
        raise LookupError(f"No business process with id {process_id}")

    db.execute(_DELETE_LINKS_SQL, {"id": process_id})
    unique_ids = list(dict.fromkeys(declaration_ids))
    for declaration_id in unique_ids:
        db.execute(
            _INSERT_LINK_SQL,
            {
                "id": f"pd_{uuid.uuid4().hex[:12]}",
                "process_id": process_id,
                "declaration_id": declaration_id,
            },
        )
    return len(unique_ids)


@privacycare_processes_router.post(
    "",
    dependencies=[Security(verify_oauth_client, scopes=[SYSTEM_READ])],
    response_model=BusinessProcessResponse,
    status_code=status_codes.HTTP_201_CREATED,
)
def create_business_process(
    request: BusinessProcessCreate,
    *,
    db: Session = Depends(get_db),
    client: ClientDetail = Security(verify_oauth_client, scopes=[SYSTEM_READ]),
) -> BusinessProcessResponse:
    """Record a business process the customer runs.

    Authorship is resolved from the authenticated principal even though this
    table has no created_by column — the resolution is what the audit log line
    below names, and doing it here keeps every write route in this module
    identical in shape.
    """
    from loguru import logger

    created_by = _created_by_from_client(client)
    process_id = _create_process(db, request)
    db.commit()
    logger.info(
        "PrivacyCare business process {} created by {}: {!r}",
        process_id,
        created_by,
        request.name,
    )
    row = db.execute(
        sqlalchemy.text(
            "SELECT id, name, description, business_cycle, owner_name, "
            "       owner_email, is_critical, criticality_note, external_ref, "
            "       last_attested_at, created_at, updated_at "
            "FROM privacycare_business_process WHERE id = :id"
        ),
        {"id": process_id},
    ).mappings().first()
    return _process_response(row)


@privacycare_processes_router.get(
    "",
    dependencies=[Security(verify_oauth_client, scopes=[SYSTEM_READ])],
    response_model=Page[BusinessProcessResponse],
)
def list_business_processes(
    *, db: Session = Depends(get_db), params: Params = Depends()
) -> Page[BusinessProcessResponse]:
    return paginate(_list_processes(db), params)


@privacycare_processes_router.put(
    "/{process_id}/declarations",
    dependencies=[Security(verify_oauth_client, scopes=[SYSTEM_READ])],
    response_model=RopaEntryResponse,
)
def link_process_declarations(
    process_id: str,
    request: LinkDeclarationsRequest,
    *,
    db: Session = Depends(get_db),
    client: ClientDetail = Security(verify_oauth_client, scopes=[SYSTEM_READ]),
) -> RopaEntryResponse:
    """Set which declarations this process processes data under.

    Returns the resulting ROPA entry rather than an acknowledgement, so a
    caller linking a declaration id that does not resolve sees it immediately
    in `missing_declarations` instead of discovering it on a later read.
    """
    from loguru import logger

    linked_by = _created_by_from_client(client)
    try:
        count = _link_declarations(db, process_id, request.privacy_declaration_ids)
    except LookupError:
        raise HTTPException(
            status_code=status_codes.HTTP_404_NOT_FOUND,
            detail=f"No business process with id {process_id}",
        )
    db.commit()
    logger.info(
        "PrivacyCare process {} linked to {} declaration(s) by {}",
        process_id,
        count,
        linked_by,
    )
    return _ropa_response(db, process_id)


@privacycare_processes_router.get(
    "/{process_id}/ropa",
    dependencies=[Security(verify_oauth_client, scopes=[SYSTEM_READ])],
    response_model=RopaEntryResponse,
)
def get_process_ropa(
    process_id: str, *, db: Session = Depends(get_db)
) -> RopaEntryResponse:
    try:
        return _ropa_response(db, process_id)
    except LookupError:
        raise HTTPException(
            status_code=status_codes.HTTP_404_NOT_FOUND,
            detail=f"No business process with id {process_id}",
        )


def _ropa_response(db: Session, process_id: str) -> RopaEntryResponse:
    entry = ropa_for_process(db, process_id)
    process = entry.process
    return RopaEntryResponse(
        process=BusinessProcessResponse(
            id=process.id,
            name=process.name,
            description=process.description,
            business_cycle=process.business_cycle,
            owner_name=process.owner_name,
            owner_email=process.owner_email,
            is_critical=process.is_critical,
            criticality_note=process.criticality_note,
            external_ref=process.external_ref,
            last_attested_at=_as_str(process.last_attested_at),
            created_at=_as_str(process.created_at),
            updated_at=_as_str(process.updated_at),
        ),
        declarations=[
            RopaDeclarationResponse(
                id=d.id,
                name=d.name,
                data_use=d.data_use,
                data_categories=list(d.data_categories or []),
                data_subjects=list(d.data_subjects or []),
                legal_basis=d.legal_basis,
                retention_period=d.retention_period,
                system_id=d.system_id,
                system_name=d.system_name,
            )
            for d in entry.declarations
        ],
        missing_declarations=list(entry.missing_declarations),
    )
