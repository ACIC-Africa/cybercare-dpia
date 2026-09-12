# The business-process ROPA surface: the layer that had no door on it.
#
# models.py has supplied BusinessProcess and ProcessDeclaration, and ropa.py
# has assembled the entry, since an earlier plan — but no route exposed any of
# it, so the code Carol's whole method starts from was unreachable from the
# product. These tests drive the routes that open it.
import uuid

import pytest
import sqlalchemy
from fastapi import HTTPException
from fastapi_pagination import Params, paginate
from sqlalchemy.orm import Session

from fides.api.privacycare.api.processes import (
    BusinessProcessCreate,
    LinkDeclarationsRequest,
    _list_processes,
    create_business_process,
    get_process_ropa,
    link_process_declarations,
)
from tests.privacycare.test_api_assessments import _fake_client
from tests.privacycare.test_context import _seed_declaration, _seed_system

DB_URL = "postgresql://postgres:fides@127.0.0.1:5442/fides"


@pytest.fixture
def db():
    engine = sqlalchemy.create_engine(DB_URL)
    with Session(engine) as session:
        yield session
        session.rollback()


def _create(db, monkeypatch, **kwargs):
    monkeypatch.setattr(db, "commit", lambda: None)
    payload = {"name": f"Process {uuid.uuid4().hex[:6]}", **kwargs}
    return create_business_process(
        BusinessProcessCreate(**payload),
        db=db,
        client=_fake_client("carol@example.com"),
    )


def test_a_business_process_can_be_recorded_and_read_back(db, monkeypatch):
    created = _create(
        db,
        monkeypatch,
        name="Fuel card application processing",
        business_cycle="Customer onboarding",
        owner_name="Josephine",
        owner_email="josephine@example.com",
        is_critical=True,
        criticality_note="Handles KYC for every retail fuel-card customer.",
    )

    assert created.id.startswith("bp_")
    assert created.name == "Fuel card application processing"
    assert created.is_critical is True
    assert created.owner_email == "josephine@example.com"
    assert created.created_at is not None


def test_the_list_puts_critical_processes_first(db, monkeypatch):
    # The SOW asks the consultant to identify the relevant processes "and
    # prioritize each", up to 15 critical ones — so the prioritisation is the
    # point of this list, not a display preference.
    ordinary = _create(db, monkeypatch, name="ZZZ Ordinary", is_critical=False)
    critical = _create(db, monkeypatch, name="AAA Critical", is_critical=True)
    db.flush()

    ids = [p.id for p in _list_processes(db)]

    assert ids.index(critical.id) < ids.index(ordinary.id)


def test_linking_declarations_returns_the_ropa_entry(db, monkeypatch):
    key = f"sys-{uuid.uuid4().hex[:6]}"
    system_id = _seed_system(db, key, name="Fuel Card CRM")
    first = _seed_declaration(db, system_id, "essential.service.payment_processing")
    second = _seed_declaration(db, system_id, "marketing.advertising")
    process = _create(db, monkeypatch, name="Fuel card administration")
    db.flush()

    entry = link_process_declarations(
        process.id,
        LinkDeclarationsRequest(privacy_declaration_ids=[first, second]),
        db=db,
        client=_fake_client("carol@example.com"),
    )

    assert entry.process.id == process.id
    assert {d.id for d in entry.declarations} == {first, second}
    assert {d.system_name for d in entry.declarations} == {"Fuel Card CRM"}
    assert entry.missing_declarations == []


def test_linking_replaces_rather_than_appends(db, monkeypatch):
    # A ROPA entry states what a process does NOW. If links only ever
    # accumulated, a decommissioned activity could never be removed through
    # the API and the record would overstate the processing.
    key = f"sys-{uuid.uuid4().hex[:6]}"
    system_id = _seed_system(db, key)
    first = _seed_declaration(db, system_id, "essential.service.payment_processing")
    second = _seed_declaration(db, system_id, "marketing.advertising")
    process = _create(db, monkeypatch, name="Replaced Links")
    db.flush()

    link_process_declarations(
        process.id,
        LinkDeclarationsRequest(privacy_declaration_ids=[first, second]),
        db=db,
        client=_fake_client("carol@example.com"),
    )
    entry = link_process_declarations(
        process.id,
        LinkDeclarationsRequest(privacy_declaration_ids=[second]),
        db=db,
        client=_fake_client("carol@example.com"),
    )

    assert [d.id for d in entry.declarations] == [second]


def test_a_duplicate_id_in_one_request_collapses(db, monkeypatch):
    # The link table is unique on (process, declaration); asking twice for the
    # same link is the same statement, not an error.
    key = f"sys-{uuid.uuid4().hex[:6]}"
    decl = _seed_declaration(db, _seed_system(db, key), "marketing.advertising")
    process = _create(db, monkeypatch, name="Duplicate Links")
    db.flush()

    entry = link_process_declarations(
        process.id,
        LinkDeclarationsRequest(privacy_declaration_ids=[decl, decl]),
        db=db,
        client=_fake_client("carol@example.com"),
    )

    assert [d.id for d in entry.declarations] == [decl]


def test_a_dangling_link_is_reported_not_dropped(db, monkeypatch):
    # A link to a declaration that no longer resolves is a finding a DPO has
    # to act on — the record of processing claims something the systems map
    # cannot corroborate. Dropping it silently would hide that.
    key = f"sys-{uuid.uuid4().hex[:6]}"
    real = _seed_declaration(db, _seed_system(db, key), "marketing.advertising")
    process = _create(db, monkeypatch, name="Dangling Link")
    db.flush()

    entry = link_process_declarations(
        process.id,
        LinkDeclarationsRequest(privacy_declaration_ids=[real, "decl_does_not_exist"]),
        db=db,
        client=_fake_client("carol@example.com"),
    )

    assert [d.id for d in entry.declarations] == [real]
    assert entry.missing_declarations == ["decl_does_not_exist"]


def test_an_empty_list_clears_every_link(db, monkeypatch):
    key = f"sys-{uuid.uuid4().hex[:6]}"
    decl = _seed_declaration(db, _seed_system(db, key), "marketing.advertising")
    process = _create(db, monkeypatch, name="Cleared Links")
    db.flush()
    link_process_declarations(
        process.id,
        LinkDeclarationsRequest(privacy_declaration_ids=[decl]),
        db=db,
        client=_fake_client("carol@example.com"),
    )

    entry = link_process_declarations(
        process.id,
        LinkDeclarationsRequest(privacy_declaration_ids=[]),
        db=db,
        client=_fake_client("carol@example.com"),
    )

    assert entry.declarations == []
    assert entry.missing_declarations == []


def test_linking_against_an_unknown_process_is_a_404(db):
    with pytest.raises(HTTPException) as exc_info:
        link_process_declarations(
            "bp_does_not_exist",
            LinkDeclarationsRequest(privacy_declaration_ids=[]),
            db=db,
            client=_fake_client("carol@example.com"),
        )
    assert exc_info.value.status_code == 404


def test_reading_the_ropa_of_an_unknown_process_is_a_404(db):
    with pytest.raises(HTTPException) as exc_info:
        get_process_ropa("bp_does_not_exist", db=db)
    assert exc_info.value.status_code == 404


def test_an_unnamed_process_is_rejected_by_the_schema():
    with pytest.raises(ValueError):
        BusinessProcessCreate(name="")


def test_the_list_returns_an_items_envelope(db, monkeypatch):
    _create(db, monkeypatch, name="Envelope Check")
    db.flush()

    page = paginate(_list_processes(db), Params(page=1, size=50))

    assert hasattr(page, "items")
    assert page.total >= 1
