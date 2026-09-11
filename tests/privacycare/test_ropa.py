# The ROPA is the whole point: for one business process, everything it
# processes, on what basis, in which system. Reads across PrivacyCare's
# process layer and Fides' declarations.
import pytest
import sqlalchemy
from sqlalchemy.orm import Session

from fides.api.privacycare.models import BusinessProcess, ProcessDeclaration
from fides.api.privacycare.ropa import ropa_for_process


@pytest.fixture
def db():
    engine = sqlalchemy.create_engine(
        "postgresql://postgres:fides@127.0.0.1:5442/fides"
    )
    with Session(engine) as session:
        yield session
        session.rollback()


def test_a_process_with_no_declarations_is_a_valid_empty_ropa(db):
    proc = BusinessProcess(name="Newly Identified Process")
    db.add(proc)
    db.flush()
    entry = ropa_for_process(db, proc.id)
    assert entry.process.name == "Newly Identified Process"
    assert entry.declarations == []
    assert entry.missing_declarations == []


def test_a_link_to_a_vanished_declaration_is_reported_not_crashed(db):
    proc = BusinessProcess(name="Process With Stale Link")
    db.add(proc)
    db.flush()
    db.add(
        ProcessDeclaration(
            business_process_id=proc.id,
            privacy_declaration_id="decl_that_does_not_exist",
        )
    )
    db.flush()
    entry = ropa_for_process(db, proc.id)
    assert entry.declarations == []
    assert entry.missing_declarations == ["decl_that_does_not_exist"], (
        "a link whose declaration is gone must surface, not vanish silently"
    )


def test_unknown_process_raises(db):
    with pytest.raises(LookupError):
        ropa_for_process(db, "no-such-process")
