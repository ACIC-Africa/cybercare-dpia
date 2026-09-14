"""D-KT-5: which Kenyan ground produced a declaration's Article 6 value.

privacycare_processing_ground (loaded by taxonomy/loader.py's
load_kenyan_taxonomy) holds 23 grounds sourced from the customer's own
lawful-basis register, of which 12 have no `fides_legal_basis` yet — D-KT-4
says the form must never offer one of those 12 until Carol rules on it. This
module is the door onto that table plus privacycare_declaration_ground, the
join that records which ground a specific declaration relies on.

D-KT-4's other half is the guarantee this module enforces on write: the form
writes the enum straight to Fides' own privacydeclaration.legal_basis_for_processing,
and separately tells us which Kenyan ground it picked. Those two facts must
agree — a declaration claiming "Consent" while pointing at a ground whose
class is "Legitimate interests" is not a data-entry error, it is Carol's
ruling and the form's tick disagreeing about what actually justifies the
processing. set_declaration_ground is the single place that agreement is
checked, so nothing downstream (the graph projection, an export, a DPIA
context source) can ever see one recorded without the other.

Authorisation (I7): recording a ground is a write about a system's
processing, so it is authorised exactly as Fides authorises a write to that
system — global SYSTEM_UPDATE (role or scope) OR system-manager rights on
the system the declaration belongs to, through Fides' own
has_system_permissions. Reading stays plain SYSTEM_READ, as everywhere else
on this surface.
"""
import uuid
from typing import List, Optional

import sqlalchemy
from fastapi import Depends, HTTPException, Security
from fastapi import status as status_codes
from fastapi.security import SecurityScopes
from loguru import logger
from pydantic import BaseModel
from sqlalchemy.orm import Session

from fides.api.deps import get_db
from fides.api.models.client import ClientDetail
from fides.api.models.sql_models import System  # type: ignore[attr-defined]
from fides.api.oauth.system_manager_oauth_util import (
    SystemAuthContainer,
    has_system_permissions,
)
from fides.api.oauth.utils import (
    PermissionCheckerCallback,
    _resolve_depends,
    extract_token_and_load_client,
    get_permission_checker,
    oauth2_scheme,
    verify_oauth_client,
)
from fides.api.privacycare.api.identity import _created_by_from_client
from fides.api.privacycare.api.router import privacycare_grounds_router
from fides.common.scope_registry import SYSTEM_READ, SYSTEM_UPDATE


class ProcessingGroundResponse(BaseModel):
    # TS counterpart: `export interface ProcessingGroundResponse` in
    # clients/admin-ui/src/features/privacycare/processing-grounds.slice.ts.
    # Not allowlisted: test_response_model_ts_parity.py requires the
    # same-named interface to exist and test_api_schemas.py compares the
    # fields and their optionality, so adding a field here without adding it
    # there fails the suite.
    id: str
    ground: str
    fides_legal_basis: str


class ProcessingGroundListResponse(BaseModel):
    # Same TS counterpart file as ProcessingGroundResponse above.
    grounds: List[ProcessingGroundResponse]
    # D-KT-4: how many of the 23 loaded grounds still have no class — a
    # count the consultant screen needs so "why isn't ground X offered" has
    # an answer without a second query.
    unmapped_count: int


class SetGroundRequest(BaseModel):
    # A request body, not a response_model, so the parity walk never reaches
    # it; the slice's SetDeclarationGroundRequest is its TS counterpart.
    processing_ground_id: str


class DeclarationGroundResponse(BaseModel):
    # Same TS counterpart file as ProcessingGroundResponse above.
    privacy_declaration_id: str
    processing_ground_id: str
    fides_legal_basis: str


_SELECT_MAPPED_GROUNDS_SQL = sqlalchemy.text(
    "SELECT id, ground, fides_legal_basis FROM privacycare_processing_ground "
    "WHERE fides_legal_basis IS NOT NULL ORDER BY ground"
)

_COUNT_UNMAPPED_GROUNDS_SQL = sqlalchemy.text(
    "SELECT count(*) FROM privacycare_processing_ground WHERE fides_legal_basis IS NULL"
)

_SELECT_GROUND_SQL = sqlalchemy.text(
    "SELECT id, fides_legal_basis FROM privacycare_processing_ground WHERE id = :id"
)

_SELECT_DECLARATION_LEGAL_BASIS_SQL = sqlalchemy.text(
    "SELECT legal_basis_for_processing FROM privacydeclaration WHERE id = :id"
)

_UPSERT_DECLARATION_GROUND_SQL = sqlalchemy.text(
    "INSERT INTO privacycare_declaration_ground "
    "(id, privacy_declaration_id, processing_ground_id, recorded_by) "
    "VALUES (:id, :declaration_id, :processing_ground_id, :recorded_by) "
    "ON CONFLICT (privacy_declaration_id) DO UPDATE SET "
    "processing_ground_id = EXCLUDED.processing_ground_id, "
    "recorded_by = EXCLUDED.recorded_by, "
    "updated_at = now()"
)

_SELECT_DECLARATION_GROUND_SQL = sqlalchemy.text(
    # The join to privacydeclaration is load-bearing, not decoration.
    # privacycare_declaration_ground deliberately carries no FK to
    # privacydeclaration (models.py), because Fides matches declarations on
    # the logical id `data_use:name` (db/system.py) and DELETEs every row
    # that no longer matches — an ordinary edit to a declaration's data_use
    # re-creates it under a new id and strands the ground row behind it.
    # Without this join such a row still reads back 200, so the screen would
    # show a ground for a declaration that no longer exists. With it, a
    # stranded row reads 404: "no ground recorded", which is the truth.
    "SELECT dg.privacy_declaration_id, dg.processing_ground_id, "
    "       pg.fides_legal_basis "
    "FROM privacycare_declaration_ground dg "
    "JOIN privacycare_processing_ground pg ON pg.id = dg.processing_ground_id "
    "JOIN privacydeclaration pd ON pd.id = dg.privacy_declaration_id "
    "WHERE dg.privacy_declaration_id = :declaration_id"
)


def _list_mapped_grounds(db: Session) -> ProcessingGroundListResponse:
    rows = db.execute(_SELECT_MAPPED_GROUNDS_SQL).mappings().all()
    unmapped_count = db.execute(_COUNT_UNMAPPED_GROUNDS_SQL).scalar()
    return ProcessingGroundListResponse(
        grounds=[
            ProcessingGroundResponse(
                id=row["id"],
                ground=row["ground"],
                fides_legal_basis=row["fides_legal_basis"],
            )
            for row in rows
        ],
        unmapped_count=unmapped_count,
    )


def _declaration_ground_row(db: Session, declaration_id: str) -> Optional[dict]:
    row = db.execute(
        _SELECT_DECLARATION_GROUND_SQL, {"declaration_id": declaration_id}
    ).mappings().first()
    return dict(row) if row is not None else None


def _record_declaration_ground(
    db: Session, declaration_id: str, ground_id: str, recorded_by: str
) -> dict:
    """D-KT-4/D-KT-5: record which Kenyan ground this declaration relies on.

    Raises LookupError(declaration|ground) for an unknown id, ValueError for
    a ground with no class yet (nothing to agree with, and nothing Carol has
    ruled on), and PermissionError when the declaration's own stored
    legal_basis_for_processing disagrees with the ground's class — the two
    are supposed to be the same fact recorded twice, and this is the one
    place that is checked.
    """
    declaration_row = db.execute(
        _SELECT_DECLARATION_LEGAL_BASIS_SQL, {"id": declaration_id}
    ).mappings().first()
    if declaration_row is None:
        raise LookupError("declaration")

    ground_row = db.execute(_SELECT_GROUND_SQL, {"id": ground_id}).mappings().first()
    if ground_row is None:
        raise LookupError("ground")

    ground_class = ground_row["fides_legal_basis"]
    if ground_class is None:
        raise ValueError("ground")

    declaration_class = declaration_row["legal_basis_for_processing"]
    if declaration_class != ground_class:
        raise PermissionError(declaration_class)

    db.execute(
        _UPSERT_DECLARATION_GROUND_SQL,
        {
            "id": f"dg_{uuid.uuid4().hex[:12]}",
            "declaration_id": declaration_id,
            "processing_ground_id": ground_id,
            "recorded_by": recorded_by,
        },
    )
    return {
        "privacy_declaration_id": declaration_id,
        "processing_ground_id": ground_id,
        "fides_legal_basis": ground_class,
    }


@privacycare_grounds_router.get(
    "/processing-grounds",
    dependencies=[Security(verify_oauth_client, scopes=[SYSTEM_READ])],
    response_model=ProcessingGroundListResponse,
)
def list_processing_grounds(
    *,
    db: Session = Depends(get_db),
    client: ClientDetail = Security(verify_oauth_client, scopes=[SYSTEM_READ]),
) -> ProcessingGroundListResponse:
    """Only grounds Carol has already ruled a class for (D-KT-4) — the form
    must never offer one of the still-NULL 12 as a choice."""
    return _list_mapped_grounds(db)


_SELECT_DECLARATION_SYSTEM_KEY_SQL = sqlalchemy.text(
    "SELECT s.fides_key FROM privacydeclaration pd "
    "JOIN ctl_systems s ON s.id = pd.system_id "
    "WHERE pd.id = :declaration_id"
)


def _system_for_declaration(
    declaration_id: str, db: Session = Depends(get_db)
) -> SystemAuthContainer:
    """The system this declaration belongs to, in the container Fides' own
    system-manager authorisation expects.

    Fides grants SYSTEM_UPDATE two ways — globally (a role or scope), or per
    system, to a system manager — and its own PUT /system resolves the
    second through _get_system_from_request_body. Our route has no system in
    its body: it is addressed by declaration id, so the system is resolved
    here instead. A declaration nobody can find yields system=None, which
    _has_scope_as_system_manager reads as "not a manager of it" — so an
    unknown id is a 403 for a caller who only has system-manager rights, and
    a 404 (from _record_declaration_ground) for one with global
    SYSTEM_UPDATE, which is the same order Fides' own endpoints answer in."""
    fides_key = db.execute(
        _SELECT_DECLARATION_SYSTEM_KEY_SQL, {"declaration_id": declaration_id}
    ).scalar()
    system = (
        db.query(System).filter(System.fides_key == fides_key).first()
        if fides_key is not None
        else None
    )
    return SystemAuthContainer(original_data=declaration_id, system=system)


async def verify_oauth_client_for_declaration_system(
    security_scopes: SecurityScopes,
    authorization: str = Security(oauth2_scheme),
    db: Session = Depends(get_db),
    system_auth_data: SystemAuthContainer = Depends(_system_for_declaration),
    permission_checker: PermissionCheckerCallback = Depends(get_permission_checker),
) -> ClientDetail:
    """I7: authorise this PUT the way Fides authorises its own system writes.

    Plain verify_oauth_client honours only global SYSTEM_UPDATE, which made
    this route stricter than the Ethyca endpoint it shadows: a consultant who
    is a *system manager* of her systems — the natural PrivacyCare persona —
    could save the declaration through Fides' own PUT /system and then be
    403'd recording which Kenyan ground justified it, on every save.
    has_system_permissions is Fides' own helper and applies both tests
    (model-level scopes OR system-manager scopes on THIS system); reusing it
    rather than restating its rules means a change upstream reaches this
    route too.

    It returns the container's original_data rather than the client, and the
    handler needs the client for recorded_by (D-KT-5: who recorded it), so
    the token is read a second time here. That is one more decode plus a
    client lookup on the same session — the price of not re-implementing the
    authorisation rules beside them."""
    # Resolve Depends if called directly (not via FastAPI DI) — the same line
    # Fides' own verify_oauth_client carries, for the same reason.
    permission_checker = _resolve_depends(permission_checker, get_permission_checker)
    has_system_permissions(
        system_auth_data=system_auth_data,
        authorization=authorization,
        security_scopes=security_scopes,
        db=db,
        permission_checker=permission_checker,
    )
    _, client = extract_token_and_load_client(authorization, db)
    return client


@privacycare_grounds_router.put(
    "/declarations/{declaration_id}/ground",
    dependencies=[
        Security(verify_oauth_client_for_declaration_system, scopes=[SYSTEM_UPDATE])
    ],
    response_model=DeclarationGroundResponse,
)
def set_declaration_ground(
    declaration_id: str,
    request: SetGroundRequest,
    *,
    db: Session = Depends(get_db),
    client: ClientDetail = Security(
        verify_oauth_client_for_declaration_system, scopes=[SYSTEM_UPDATE]
    ),
) -> DeclarationGroundResponse:
    recorded_by = _created_by_from_client(client)
    try:
        result = _record_declaration_ground(
            db, declaration_id, request.processing_ground_id, recorded_by
        )
    except LookupError as exc:
        if exc.args[0] == "declaration":
            detail = f"No declaration with id {declaration_id}"
        else:
            detail = f"No ground with id {request.processing_ground_id}"
        raise HTTPException(status_code=status_codes.HTTP_404_NOT_FOUND, detail=detail)
    except ValueError:
        raise HTTPException(
            status_code=status_codes.HTTP_409_CONFLICT,
            detail="This ground has no legal basis class yet; it cannot be recorded "
            "against a declaration until Carol rules on it.",
        )
    except PermissionError as exc:
        declaration_class = exc.args[0]
        raise HTTPException(
            status_code=status_codes.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "The declaration's own legal_basis_for_processing "
                f"({declaration_class!r}) does not agree with this ground's class; "
                "the two must name the same lawful basis before it can be recorded."
            ),
        )
    db.commit()
    logger.info(
        "PrivacyCare declaration {} recorded against ground {} by {}",
        declaration_id,
        request.processing_ground_id,
        recorded_by,
    )
    return DeclarationGroundResponse(**result)


@privacycare_grounds_router.get(
    "/declarations/{declaration_id}/ground",
    dependencies=[Security(verify_oauth_client, scopes=[SYSTEM_READ])],
    response_model=DeclarationGroundResponse,
)
def get_declaration_ground(
    declaration_id: str,
    *,
    db: Session = Depends(get_db),
    client: ClientDetail = Security(verify_oauth_client, scopes=[SYSTEM_READ]),
) -> DeclarationGroundResponse:
    row = _declaration_ground_row(db, declaration_id)
    if row is None:
        raise HTTPException(
            status_code=status_codes.HTTP_404_NOT_FOUND,
            detail=f"No recorded ground for declaration {declaration_id}",
        )
    return DeclarationGroundResponse(**row)
