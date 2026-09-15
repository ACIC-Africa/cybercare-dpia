"""The reconciler: diff a scan's FoundResource list against what is already
staged, and write only the difference.

Spec D-EX-5 (a removal is a diff status, never a DELETE) and D-EX-6 (a
re-scan that finds the same resources again touches nothing) are the two
decisions this module exists to enforce. The cross-monitor scoping test
below is the one most likely to be got wrong, and the most damaging if it
is: a "what has gone" query that forgets to filter by monitor marks every
OTHER monitor's resources removed on the first run of a new one.

No connector, no scanning: every FoundResource here is constructed by hand,
exactly the shape Task 1's walk_catalogue would have produced.
"""
import pytest
import sqlalchemy
from sqlalchemy.orm import Session

from fides.api.privacycare.discovery.reconcile import ReconcileSummary, reconcile
from fides.api.privacycare.discovery.walk import FoundResource

DB_URL = "postgresql://postgres:fides@127.0.0.1:5442/fides"


@pytest.fixture
def db(monkeypatch):
    engine = sqlalchemy.create_engine(DB_URL)
    with Session(engine) as session:
        # Same fixture pattern as test_discovery_walk.py / test_api_monitors.py:
        # commit -> flush keeps every row visible to later statements in this
        # SAME transaction (persist_obj's refresh() needs that), while
        # rollback() at teardown discards the transaction wholesale, so
        # nothing written here ever reaches a separate connection.
        monkeypatch.setattr(session, "commit", session.flush)
        yield session
        session.rollback()


def _stage_one(db: Session, *, monitor_config_id: str, monitor_key: str, urn: str) -> str:
    """Insert a single stagedresource row directly (bypassing reconcile()
    entirely) so a test can set up "what was already staged" before calling
    reconcile() to see what it does with that state. Fills every NOT NULL
    column reconcile() itself fills.

    monitor_key is accepted only so call sites read the same way a
    reconcile() call does -- there is no stagedresource.monitor_key column
    (see reconcile.py's own module docstring: the monitor_key is already
    implicit in the urn's first segment), so it is not written anywhere.
    """
    del monitor_key
    db.execute(
        sqlalchemy.text(
            """
            INSERT INTO stagedresource (
                id, urn, name, resource_type, monitor_config_id, diff_status,
                classifications, user_assigned_data_categories, children, meta
            ) VALUES (
                'sta_' || gen_random_uuid(), :urn, :urn, 'Table',
                :monitor_config_id, 'addition', '{}', '{}', '{}', '{}'::jsonb
            )
            """
        ),
        {"urn": urn, "monitor_config_id": monitor_config_id},
    )
    return urn


def _diff_status(db: Session, urn: str) -> str:
    return db.execute(
        sqlalchemy.text("SELECT diff_status FROM stagedresource WHERE urn = :urn"),
        {"urn": urn},
    ).scalar()


def _row_count(db: Session) -> int:
    return db.execute(
        sqlalchemy.text("SELECT count(*) FROM stagedresource")
    ).scalar()


def test_a_first_scan_marks_everything_addition(db):
    found = [
        FoundResource(urn="m1.db.public", name="db", resource_type="Database",
                      parent_urn=None, field_type=None),
        FoundResource(urn="m1.db.public.orders", name="orders", resource_type="Table",
                      parent_urn="m1.db.public", field_type=None),
    ]

    summary = reconcile(db, monitor_config_id="mon1", monitor_key="m1", found=found)

    assert summary == ReconcileSummary(added=2, removed=0, unchanged=0)
    for resource in found:
        assert _diff_status(db, resource.urn) == "addition"


def test_a_second_identical_scan_changes_nothing(db):
    # D-EX-6. summary.added == 0, summary.unchanged == the full count, and
    # the rows' diff_status is untouched. A consultant will re-run this.
    found = [
        FoundResource(urn="m1.db.public", name="db", resource_type="Database",
                      parent_urn=None, field_type=None),
        FoundResource(urn="m1.db.public.orders", name="orders", resource_type="Table",
                      parent_urn="m1.db.public", field_type=None),
    ]
    reconcile(db, monitor_config_id="mon1", monitor_key="m1", found=found)

    summary = reconcile(db, monitor_config_id="mon1", monitor_key="m1", found=found)

    assert summary == ReconcileSummary(added=0, removed=0, unchanged=2)
    for resource in found:
        assert _diff_status(db, resource.urn) == "addition"


def test_a_resource_that_has_gone_is_marked_removal_and_still_exists(db):
    # D-EX-5. The ROPA must be able to show that a table which held personal
    # data last quarter is gone this quarter. Assert BOTH the status and
    # that the row is still there.
    found = [
        FoundResource(urn="m1.db.public", name="db", resource_type="Database",
                      parent_urn=None, field_type=None),
        FoundResource(urn="m1.db.public.orders", name="orders", resource_type="Table",
                      parent_urn="m1.db.public", field_type=None),
    ]
    reconcile(db, monitor_config_id="mon1", monitor_key="m1", found=found)

    # orders is gone this time; only the database is found.
    summary = reconcile(
        db, monitor_config_id="mon1", monitor_key="m1",
        found=[found[0]],
    )

    assert summary == ReconcileSummary(added=0, removed=1, unchanged=1)
    assert _diff_status(db, "m1.db.public.orders") == "removal"
    assert _row_count(db) == 2  # still there, not deleted


def test_nothing_ever_deletes_a_staged_resource(db):
    # Count rows before and after a reconcile whose `found` list is empty.
    _stage_one(db, monitor_config_id="mon1", monitor_key="m1", urn="m1.db.public.orders")
    before = _row_count(db)

    summary = reconcile(db, monitor_config_id="mon1", monitor_key="m1", found=[])

    assert _row_count(db) == before
    assert summary == ReconcileSummary(added=0, removed=1, unchanged=0)
    assert _diff_status(db, "m1.db.public.orders") == "removal"


def test_ancestry_is_written_with_correct_distances(db):
    # A field's ancestors: table 1, schema 2, database 3.
    found = [
        FoundResource(urn="m1.db", name="db", resource_type="Database",
                      parent_urn=None, field_type=None),
        FoundResource(urn="m1.db.public", name="public", resource_type="Schema",
                      parent_urn="m1.db", field_type=None),
        FoundResource(urn="m1.db.public.orders", name="orders", resource_type="Table",
                      parent_urn="m1.db.public", field_type=None),
        FoundResource(urn="m1.db.public.orders.id", name="id", resource_type="Field",
                      parent_urn="m1.db.public.orders", field_type="INTEGER"),
    ]

    reconcile(db, monitor_config_id="mon1", monitor_key="m1", found=found)

    rows = db.execute(
        sqlalchemy.text(
            "SELECT ancestor_urn, distance FROM stagedresourceancestor"
            " WHERE descendant_urn = :urn"
        ),
        {"urn": "m1.db.public.orders.id"},
    ).all()
    distances = {ancestor_urn: distance for ancestor_urn, distance in rows}
    assert distances == {
        "m1.db.public.orders": 1,
        "m1.db.public": 2,
        "m1.db": 3,
    }


def test_reconciling_twice_does_not_duplicate_ancestry(db):
    found = [
        FoundResource(urn="m1.db", name="db", resource_type="Database",
                      parent_urn=None, field_type=None),
        FoundResource(urn="m1.db.public", name="public", resource_type="Schema",
                      parent_urn="m1.db", field_type=None),
        FoundResource(urn="m1.db.public.orders", name="orders", resource_type="Table",
                      parent_urn="m1.db.public", field_type=None),
    ]

    reconcile(db, monitor_config_id="mon1", monitor_key="m1", found=found)
    reconcile(db, monitor_config_id="mon1", monitor_key="m1", found=found)

    count = db.execute(
        sqlalchemy.text(
            "SELECT count(*) FROM stagedresourceancestor WHERE descendant_urn = :urn"
        ),
        {"urn": "m1.db.public.orders"},
    ).scalar()
    assert count == 2  # orders -> public (1), orders -> db (2); not doubled


def test_rows_from_another_monitor_are_untouched(db):
    # The scoping trap. `reconcile` decides what has GONE by comparing against
    # what is stored -- so a query that forgets to filter by monitor would mark
    # every other monitor's resources `removal` on the first run of a new one.
    # On a real estate that is a fleet of false "this table disappeared"
    # findings, from a monitor that never looked at those tables.
    other = _stage_one(db, monitor_config_id="mon_other", monitor_key="other",
                       urn="other.db.public.customers")

    reconcile(db, monitor_config_id="mon_mine", monitor_key="mine", found=[
        FoundResource(urn="mine.db.public.orders", name="orders",
                      resource_type="Table", parent_urn="mine.db.public",
                      field_type=None),
    ])

    still_there = db.execute(
        sqlalchemy.text(
            "SELECT diff_status FROM stagedresource WHERE urn = :urn"
        ),
        {"urn": other},
    ).scalar()
    assert still_there != "removal", (
        "reconciling one monitor marked another monitor's resources removed"
    )
