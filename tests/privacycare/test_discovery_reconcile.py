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
from typing import Optional

import pytest
import sqlalchemy
from sqlalchemy.orm import Session

from fides.api.privacycare.discovery.reconcile import (
    _INSERT_SQL,
    ReconcileSummary,
    reconcile,
)
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


def _stage_one(
    db: Session,
    *,
    monitor_config_id: str,
    monitor_key: str,
    urn: str,
    diff_status: str = "addition",
    resource_type: str = "Table",
) -> str:
    """Insert a single stagedresource row directly (bypassing reconcile()
    entirely) so a test can set up "what was already staged" before calling
    reconcile() to see what it does with that state. Fills every NOT NULL
    column reconcile() itself fills.

    monitor_key is accepted only so call sites read the same way a
    reconcile() call does -- there is no stagedresource.monitor_key column
    (see reconcile.py's own module docstring: the monitor_key is already
    implicit in the urn's first segment), so it is not written anywhere.

    `diff_status` defaults to 'addition' but a caller can stage a resource
    already sitting on a DiffStatus a real scan never produces on its own
    (e.g. 'monitored' -- F5's round trip needs a resource a user has already
    promoted, not one fresh out of a first scan).
    """
    del monitor_key
    db.execute(
        sqlalchemy.text(
            """
            INSERT INTO stagedresource (
                id, urn, name, resource_type, monitor_config_id, diff_status,
                classifications, user_assigned_data_categories, children, meta
            ) VALUES (
                'sta_' || gen_random_uuid(), :urn, :urn, :resource_type,
                :monitor_config_id, :diff_status, '{}', '{}', '{}', '{}'::jsonb
            )
            """
        ),
        {
            "urn": urn,
            "monitor_config_id": monitor_config_id,
            "diff_status": diff_status,
            "resource_type": resource_type,
        },
    )
    return urn


def _diff_status(db: Session, urn: str) -> str:
    return db.execute(
        sqlalchemy.text("SELECT diff_status FROM stagedresource WHERE urn = :urn"),
        {"urn": urn},
    ).scalar()


def _meta(db: Session, urn: str) -> dict:
    return db.execute(
        sqlalchemy.text("SELECT meta FROM stagedresource WHERE urn = :urn"),
        {"urn": urn},
    ).scalar()


def _is_leaf(db: Session, urn: str) -> Optional[bool]:
    return db.execute(
        sqlalchemy.text("SELECT is_leaf FROM stagedresource WHERE urn = :urn"),
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
    #
    # `_row_count(db)` counts the WHOLE stagedresource table, which a real
    # discovery scan (2026-09-18) permanently populated with 1881 rows of
    # its own — an absolute "== 2" here broke the day that scan ran. Measure
    # this test's own baseline before touching anything, then assert the
    # DELTA the two rows this test adds still holds, whatever else the
    # table already contains (same baseline-relative discipline
    # test_dsr_alert_job.py's own fix already applies).
    before = _row_count(db)
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
    assert _row_count(db) == before + 2  # still there, not deleted


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


def test_a_resurrected_resource_is_marked_addition_not_unchanged(db):
    # Found -> gone -> found again. A urn that comes back after being marked
    # `removal` must become `addition` again and be counted in `added` --
    # NOT silently folded into `unchanged`, which would assert (falsely, in
    # a record of processing) that nothing changed about a resource that in
    # fact vanished and reappeared between two scans.
    found = [
        FoundResource(urn="m1.db.public", name="db", resource_type="Database",
                      parent_urn=None, field_type=None),
        FoundResource(urn="m1.db.public.orders", name="orders", resource_type="Table",
                      parent_urn="m1.db.public", field_type=None),
    ]
    reconcile(db, monitor_config_id="mon1", monitor_key="m1", found=found)

    # orders goes away.
    reconcile(db, monitor_config_id="mon1", monitor_key="m1", found=[found[0]])
    assert _diff_status(db, "m1.db.public.orders") == "removal"

    # orders is back.
    summary = reconcile(db, monitor_config_id="mon1", monitor_key="m1", found=found)

    assert _diff_status(db, "m1.db.public.orders") == "addition"
    assert summary.added == 1  # orders, resurrected
    assert summary.unchanged == 1  # just the database


def test_removal_stashes_the_prior_status_and_resurrection_restores_it(db):
    # F5 deliberate-break proof (see fix-wave-report.md). Before this fix,
    # `_RESURRECT_SQL` unconditionally reset diff_status to 'addition' --
    # a promoted ('monitored') or muted ('muted') resource that missed one
    # scan would resurrect as brand-new work, its status silently gone, no
    # audit trail of what it had been. Round trip BOTH the plain 'addition'
    # case and the 'monitored' case, since a bug here would most plausibly
    # be "restores addition fine, but forgets anything else was possible".
    found = [
        FoundResource(urn="m1.db.public", name="db", resource_type="Database",
                      parent_urn=None, field_type=None),
        FoundResource(urn="m1.db.public.orders", name="orders", resource_type="Table",
                      parent_urn="m1.db.public", field_type=None),
        FoundResource(urn="m1.db.public.customers", name="customers", resource_type="Table",
                      parent_urn="m1.db.public", field_type=None),
    ]
    reconcile(db, monitor_config_id="mon1", monitor_key="m1", found=found)

    # "orders" stays a plain 'addition'. "customers" gets promoted the way
    # plan 12 will (simulated directly -- reconcile() itself never writes
    # 'monitored', nothing does yet).
    db.execute(
        sqlalchemy.text(
            "UPDATE stagedresource SET diff_status = 'monitored' WHERE urn = :urn"
        ),
        {"urn": "m1.db.public.customers"},
    )

    # Both go missing from the next scan.
    reconcile(db, monitor_config_id="mon1", monitor_key="m1", found=[found[0]])

    assert _diff_status(db, "m1.db.public.orders") == "removal"
    assert _meta(db, "m1.db.public.orders")["pre_removal_diff_status"] == "addition"
    assert _diff_status(db, "m1.db.public.customers") == "removal"
    assert _meta(db, "m1.db.public.customers")["pre_removal_diff_status"] == "monitored"

    # Both come back.
    reconcile(db, monitor_config_id="mon1", monitor_key="m1", found=found)

    assert _diff_status(db, "m1.db.public.orders") == "addition", (
        "a plain 'addition' resource did not round-trip back to 'addition'"
    )
    assert _diff_status(db, "m1.db.public.customers") == "monitored", (
        "F5's whole point: a promoted resource resurrected as 'addition' "
        "instead of restoring 'monitored' -- the user's decision was lost"
    )
    # The stashed key is cleaned up on restore, not left behind for a
    # second removal/resurrection cycle to find stale.
    assert "pre_removal_diff_status" not in _meta(db, "m1.db.public.orders")
    assert "pre_removal_diff_status" not in _meta(db, "m1.db.public.customers")


def test_resurrection_defaults_to_addition_when_nothing_was_stashed(db):
    # F5: a row marked 'removal' by code that predates this fix (or any
    # other gap) has no stashed status to restore. COALESCE must fall back
    # to 'addition' rather than resurrecting as NULL or raising.
    _stage_one(db, monitor_config_id="mon1", monitor_key="m1",
               urn="m1.db.public.legacy", diff_status="removal")

    found = [
        FoundResource(urn="m1.db.public.legacy", name="legacy", resource_type="Table",
                      parent_urn=None, field_type=None),
    ]
    reconcile(db, monitor_config_id="mon1", monitor_key="m1", found=found)

    assert _diff_status(db, "m1.db.public.legacy") == "addition"


def test_a_new_field_writes_its_sql_type_into_meta(db):
    # F7: Ethyca's own convention is meta->>'data_type'; classification
    # (plan 13) is the direct consumer and would otherwise have to re-scan
    # every field for a value this scan already held.
    found = [
        FoundResource(urn="m1.db.public.orders", name="orders", resource_type="Table",
                      parent_urn=None, field_type=None),
        FoundResource(urn="m1.db.public.orders.id", name="id", resource_type="Field",
                      parent_urn="m1.db.public.orders", field_type="INTEGER"),
    ]
    reconcile(db, monitor_config_id="mon1", monitor_key="m1", found=found)

    assert _meta(db, "m1.db.public.orders.id")["data_type"] == "INTEGER"
    # A Table's meta carries no data_type -- that key is Field-only.
    assert "data_type" not in _meta(db, "m1.db.public.orders")


def test_a_view_writes_its_table_type_into_meta_and_an_ordinary_table_does_not(db):
    # F8: StagedResourceType has no View member, so a view is staged as a
    # Table tagged via meta.table_type (walk.py's FoundResource.table_type).
    # An ordinary table (table_type=None) leaves meta.table_type unset.
    found = [
        FoundResource(urn="m1.db.public.orders", name="orders", resource_type="Table",
                      parent_urn=None, field_type=None, table_type=None),
        FoundResource(urn="m1.db.public.v_orders", name="v_orders", resource_type="Table",
                      parent_urn=None, field_type=None, table_type="view"),
        FoundResource(urn="m1.db.public.mv_orders", name="mv_orders", resource_type="Table",
                      parent_urn=None, field_type=None, table_type="materialized_view"),
    ]
    reconcile(db, monitor_config_id="mon1", monitor_key="m1", found=found)

    assert "table_type" not in _meta(db, "m1.db.public.orders")
    assert _meta(db, "m1.db.public.v_orders")["table_type"] == "view"
    assert _meta(db, "m1.db.public.mv_orders")["table_type"] == "materialized_view"


def test_a_new_field_is_leaf_and_a_new_table_is_not(db):
    # F6: is_leaf is documented as "None = not applicable (non-datastore
    # monitors)" -- our rows ARE datastore resources and were all landing
    # as NULL, which is in neither of the two partial indexes plan 12's
    # results queries rely on (WHERE is_leaf IS NOT NULL / IS TRUE).
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

    assert _is_leaf(db, "m1.db.public.orders.id") is True
    assert _is_leaf(db, "m1.db") is False
    assert _is_leaf(db, "m1.db.public") is False
    assert _is_leaf(db, "m1.db.public.orders") is False


def test_the_insert_is_idempotent_under_a_racing_duplicate_urn(db):
    # F3: two overlapping runs (a double-clicked Scan, or a retry racing a
    # still-running execution) can independently decide the same urn is
    # "new" -- both ran their own _FIND_EXISTING_SQL before either's INSERT
    # landed -- and both attempt to INSERT it. `ix_stagedresource_urn` is a
    # UNIQUE index, so without ON CONFLICT DO NOTHING the loser's INSERT
    # would raise IntegrityError and abort its entire transaction, not just
    # that one row.
    #
    # Driven directly against `_INSERT_SQL` (the exact statement reconcile()
    # uses) rather than through two reconcile() calls: reconcile()'s own
    # existence check (_FIND_EXISTING_SQL) would see the first call's row
    # once it exists and correctly treat the urn as already-staged, never
    # re-attempting the INSERT at all -- masking the very race this test
    # exists to prove is now harmless. This is exactly the situation two
    # genuinely concurrent callers would produce: each decided "new"
    # independently, and only one of their INSERTs can win.
    # Same baseline-relative fix as the test above: this table already
    # carries 1881 real rows from a 2026-09-18 discovery scan, so an
    # absolute "== 1" is exactly the defect that broke this test then —
    # measure this test's own baseline first, assert the delta.
    before = _row_count(db)
    params = {
        "urn": "m1.db.public.orders", "name": "orders", "resource_type": "Table",
        "parent": "m1.db.public", "monitor_config_id": "mon1",
        "diff_status": "addition", "meta": "{}", "is_leaf": False,
    }

    winner = db.execute(_INSERT_SQL, [params])
    assert winner.rowcount == 1

    loser = db.execute(_INSERT_SQL, [params])
    assert loser.rowcount == 0, (
        "a racing duplicate INSERT must be a silent no-op, not an error"
    )

    assert _row_count(db) == before + 1
