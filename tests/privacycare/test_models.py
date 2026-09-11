# The business process is the entity Fides does not have and Serianu's
# method requires. These tests pin its shape and the soft-delete behaviour
# that keeps retired processes visible in historical ROPAs.
import datetime

import pytest
import sqlalchemy
from sqlalchemy.orm import Session

from fides.api.privacycare.models import BusinessProcess


@pytest.fixture
def db():
    engine = sqlalchemy.create_engine(
        "postgresql://postgres:fides@127.0.0.1:5442/fides"
    )
    with Session(engine) as session:
        yield session
        session.rollback()


def test_business_process_persists_with_an_owner(db):
    proc = BusinessProcess(
        name="Fuel Dealer Onboarding",
        description="Vetting and contracting a new retail dealer",
        business_cycle="Relationship Management",
        owner_name="A. Mwangi",
        owner_email="a.mwangi@example.co.ke",
        is_critical=True,
        criticality_note="Handles national ID and KRA PIN of dealer principals",
    )
    db.add(proc)
    db.flush()
    assert proc.id, "id should be generated"
    assert proc.created_at is not None
    assert proc.last_attested_at is None, "a new process has never been attested"
    assert proc.deleted_at is None


def test_name_is_required(db):
    db.add(BusinessProcess(description="no name"))
    with pytest.raises(sqlalchemy.exc.IntegrityError):
        db.flush()


def test_soft_delete_keeps_the_row(db):
    proc = BusinessProcess(name="Retired Process")
    db.add(proc)
    db.flush()
    proc.deleted_at = datetime.datetime.now(datetime.timezone.utc)
    db.flush()
    found = db.get(BusinessProcess, proc.id)
    assert found is not None, "a soft-deleted process must remain readable"
    assert found.deleted_at is not None
