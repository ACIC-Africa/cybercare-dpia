"""The discovery-findings core: list a discovered table's row and record a
reconciliation decision against it (2026-09-18 discovery-findings-API
brief).

No connector, no scanning, no HTTP: every `stagedresource` row here is
inserted by hand, exactly the shape reconcile.py's own INSERT produces —
same convention test_discovery_reconcile.py already uses for the module
one layer below this one. A dedicated, fabricated `monitor_key` per test
(never the real `privacycare_local_discovery` key the live scan uses)
keeps every assertion below scoped to rows THIS test wrote, regardless of
how many real tables happen to be staged in the shared database this
suite runs against.
"""
import uuid

import pytest
import sqlalchemy
from sqlalchemy.orm import Session

from fides.api.privacycare.discovery.findings import (
    STATE_IGNORED,
    STATE_MAPPED,
    STATE_NEEDS_REVIEW,
    list_findings,
    reconcile_finding,
    reconciliation_history,
)

DB_URL = "postgresql://postgres:fides@127.0.0.1:5442/fides"


@pytest.fixture
def db(monkeypatch):
    engine = sqlalchemy.create_engine(DB_URL)
    with Session(engine) as session:
        # NEVER a no-op: base_class.persist_obj does add/commit/refresh, and
        # a no-op commit starves refresh(). flush() gives write visibility
        # within the transaction without making it durable; rollback() on
        # teardown discards everything this test wrote — including every
        # synthetic stagedresource row _stage_table below inserts, so the
        # real 1881-row scan estate is never touched.
        monkeypatch.setattr(session, "commit", session.flush)
        yield session
        session.rollback()


def _monitor_key() -> str:
    return f"test_monitor_{uuid.uuid4().hex[:8]}"


def _stage_resource(
    db: Session,
    *,
    urn: str,
    name: str,
    resource_type: str,
    parent: str,
    monitor_key: str,
) -> None:
    # ON CONFLICT DO NOTHING: _stage_table (below) calls this for the same
    # Database/Schema urn once per table it stages under one monitor_key —
    # real walk_catalogue traversal never repeats a database/schema node,
    # but this test helper deliberately builds one table at a time, so
    # staging a second table under the SAME monitor must not collide on
    # the shared database/schema ancestor it already wrote.
    db.execute(
        sqlalchemy.text(
            """
            INSERT INTO stagedresource (
                id, urn, name, resource_type, parent, monitor_config_id,
                diff_status, classifications, user_assigned_data_categories,
                children, meta
            ) VALUES (
                'sta_' || gen_random_uuid(), :urn, :name, :resource_type,
                :parent, :monitor_key, 'addition', '{}', '{}', '{}', '{}'::jsonb
            )
            ON CONFLICT (urn) DO NOTHING
            """
        ),
        {
            "urn": urn,
            "name": name,
            "resource_type": resource_type,
            "parent": parent,
            "monitor_key": monitor_key,
        },
    )


def _stage_table(db: Session, *, monitor_key: str, table_name: str, field_count: int) -> str:
    """One Database -> Schema -> Table -> N Fields, the exact shape
    walk.py's own traversal produces, so list_findings' joins (parent ->
    Schema for the name, parent -> Table for the field count) see real
    ancestry rather than a shortcut."""
    database_urn = f"{monitor_key}.db"
    schema_urn = f"{database_urn}.public"
    table_urn = f"{schema_urn}.{table_name}"
    _stage_resource(
        db, urn=database_urn, name="db", resource_type="Database",
        parent=None, monitor_key=monitor_key,
    )
    _stage_resource(
        db, urn=schema_urn, name="public", resource_type="Schema",
        parent=database_urn, monitor_key=monitor_key,
    )
    _stage_resource(
        db, urn=table_urn, name=table_name, resource_type="Table",
        parent=schema_urn, monitor_key=monitor_key,
    )
    for i in range(field_count):
        _stage_resource(
            db, urn=f"{table_urn}.col_{i}", name=f"col_{i}",
            resource_type="Field", parent=table_urn, monitor_key=monitor_key,
        )
    return table_urn


def _real_system_id(db: Session) -> str:
    """A real ctl_systems.id from the live estate, read-only — never
    written to. Using a real id (rather than fabricating a ctl_systems row,
    which carries its own required-column surface) is enough to prove
    reconcile_finding's existence check without touching Ethyca's table at
    all."""
    row = db.execute(sqlalchemy.text("SELECT id FROM ctl_systems LIMIT 1")).first()
    assert row is not None, "fixture assumption: at least one ctl_systems row exists"
    return row.id


# --- list_findings ---------------------------------------------------------


def test_lists_one_row_per_table_with_its_field_count(db):
    monitor_key = _monitor_key()
    urn_a = _stage_table(db, monitor_key=monitor_key, table_name="customers", field_count=5)
    urn_b = _stage_table(db, monitor_key=monitor_key, table_name="orders", field_count=3)

    findings = {f.urn: f for f in list_findings(db, monitor_key=monitor_key)}

    assert set(findings) == {urn_a, urn_b}
    assert findings[urn_a].field_count == 5
    assert findings[urn_a].table_name == "customers"
    assert findings[urn_a].schema_name == "public"
    assert findings[urn_b].field_count == 3


def test_a_table_with_no_reconciliation_is_needs_review(db):
    monitor_key = _monitor_key()
    urn = _stage_table(db, monitor_key=monitor_key, table_name="t", field_count=1)

    [finding] = list_findings(db, monitor_key=monitor_key)

    assert finding.urn == urn
    assert finding.state == STATE_NEEDS_REVIEW
    assert finding.system_id is None
    assert finding.reason is None
    assert finding.decided_by is None
    assert finding.decided_at is None


def test_fields_are_never_listed_as_findings_of_their_own(db):
    # 1703 fields is not a work queue (the brief's own words) — this is the
    # regression test for that: a Field-typed stagedresource row must never
    # surface as its own list_findings entry, only as another Table row's
    # field_count.
    monitor_key = _monitor_key()
    _stage_table(db, monitor_key=monitor_key, table_name="t", field_count=4)

    findings = list_findings(db, monitor_key=monitor_key)

    assert len(findings) == 1
    assert findings[0].field_count == 4


def test_list_is_one_statement_regardless_of_table_count(db):
    # Not a timing assertion (flaky by nature) — a structural one: the
    # module docstring's whole claim is ONE query, so this proves it by
    # counting statements, not by racing a clock.
    monitor_key = _monitor_key()
    for i in range(10):
        _stage_table(db, monitor_key=monitor_key, table_name=f"t{i}", field_count=2)

    statement_count = 0

    def _count(*args, **kwargs):
        nonlocal statement_count
        statement_count += 1

    engine = db.get_bind()
    sqlalchemy.event.listen(engine, "before_cursor_execute", _count)
    try:
        findings = list_findings(db, monitor_key=monitor_key)
    finally:
        sqlalchemy.event.remove(engine, "before_cursor_execute", _count)

    assert len(findings) == 10
    assert statement_count == 1, (
        f"list_findings issued {statement_count} statements for 10 tables — "
        "expected exactly one"
    )


def test_filters_to_needs_review(db):
    monitor_key = _monitor_key()
    system_id = _real_system_id(db)
    mapped_urn = _stage_table(db, monitor_key=monitor_key, table_name="mapped", field_count=1)
    unreviewed_urn = _stage_table(db, monitor_key=monitor_key, table_name="unreviewed", field_count=1)
    reconcile_finding(
        db, urn=mapped_urn, state=STATE_MAPPED, system_id=system_id,
        reason=None, decided_by="tester",
    )

    needs_review = list_findings(db, monitor_key=monitor_key, state=STATE_NEEDS_REVIEW)

    assert [f.urn for f in needs_review] == [unreviewed_urn]


def test_an_unknown_state_filter_is_a_value_error(db):
    with pytest.raises(ValueError, match="unknown finding state filter"):
        list_findings(db, state="bogus")


# --- reconcile_finding ------------------------------------------------------


def test_marking_mapped_requires_a_system_id(db):
    monitor_key = _monitor_key()
    urn = _stage_table(db, monitor_key=monitor_key, table_name="t", field_count=1)

    with pytest.raises(ValueError, match="system_id is required"):
        reconcile_finding(
            db, urn=urn, state=STATE_MAPPED, system_id=None, reason=None,
            decided_by="tester",
        )


def test_marking_mapped_rejects_a_reason(db):
    monitor_key = _monitor_key()
    system_id = _real_system_id(db)
    urn = _stage_table(db, monitor_key=monitor_key, table_name="t", field_count=1)

    with pytest.raises(ValueError, match="reason must not be given"):
        reconcile_finding(
            db, urn=urn, state=STATE_MAPPED, system_id=system_id,
            reason="not needed", decided_by="tester",
        )


def test_marking_mapped_with_an_unknown_system_is_rejected(db):
    monitor_key = _monitor_key()
    urn = _stage_table(db, monitor_key=monitor_key, table_name="t", field_count=1)

    with pytest.raises(ValueError, match="unknown system"):
        reconcile_finding(
            db, urn=urn, state=STATE_MAPPED, system_id="sys_does_not_exist",
            reason=None, decided_by="tester",
        )


def test_ignoring_requires_a_non_blank_reason(db):
    monitor_key = _monitor_key()
    urn = _stage_table(db, monitor_key=monitor_key, table_name="t", field_count=1)

    with pytest.raises(ValueError, match="reason is required"):
        reconcile_finding(
            db, urn=urn, state=STATE_IGNORED, system_id=None, reason=None,
            decided_by="tester",
        )
    with pytest.raises(ValueError, match="reason is required"):
        reconcile_finding(
            db, urn=urn, state=STATE_IGNORED, system_id=None, reason="   ",
            decided_by="tester",
        )


def test_ignoring_rejects_a_system_id(db):
    monitor_key = _monitor_key()
    system_id = _real_system_id(db)
    urn = _stage_table(db, monitor_key=monitor_key, table_name="t", field_count=1)

    with pytest.raises(ValueError, match="system_id must not be given"):
        reconcile_finding(
            db, urn=urn, state=STATE_IGNORED, system_id=system_id,
            reason="no personal data here", decided_by="tester",
        )


def test_an_unknown_state_is_rejected(db):
    monitor_key = _monitor_key()
    urn = _stage_table(db, monitor_key=monitor_key, table_name="t", field_count=1)

    with pytest.raises(ValueError, match="unknown reconciliation state"):
        reconcile_finding(
            db, urn=urn, state="bogus", system_id=None, reason=None,
            decided_by="tester",
        )


def test_an_unknown_urn_is_rejected_by_name(db):
    with pytest.raises(ValueError, match="no such finding"):
        reconcile_finding(
            db, urn="no-such-urn", state=STATE_IGNORED, system_id=None,
            reason="whatever", decided_by="tester",
        )


def test_ignoring_a_field_urn_is_rejected_same_as_an_unknown_urn(db):
    # Reconciliation is scoped to TABLES (the work queue), never a Field —
    # _table_exists filters on resource_type = 'Table', so a Field's own
    # urn (a real stagedresource row, just the wrong kind) must be
    # rejected exactly like a urn that was never staged at all.
    monitor_key = _monitor_key()
    _stage_table(db, monitor_key=monitor_key, table_name="t", field_count=1)
    field_urn = f"{monitor_key}.db.public.t.col_0"

    with pytest.raises(ValueError, match="no such finding"):
        reconcile_finding(
            db, urn=field_urn, state=STATE_IGNORED, system_id=None,
            reason="whatever", decided_by="tester",
        )


def test_reconciling_records_who_when_and_why(db):
    monitor_key = _monitor_key()
    urn = _stage_table(db, monitor_key=monitor_key, table_name="t", field_count=1)

    result = reconcile_finding(
        db, urn=urn, state=STATE_IGNORED, system_id=None,
        reason="Internal ops table, no personal data.", decided_by="carol@customer.example",
    )

    assert result.urn == urn
    assert result.state == STATE_IGNORED
    assert result.reason == "Internal ops table, no personal data."
    assert result.decided_by == "carol@customer.example"
    assert result.decided_at is not None


def test_a_mapped_reconciliation_resolves_the_system_name(db):
    monitor_key = _monitor_key()
    system_id = _real_system_id(db)
    system_name = db.execute(
        sqlalchemy.text("SELECT name FROM ctl_systems WHERE id = :id"), {"id": system_id}
    ).scalar_one()
    urn = _stage_table(db, monitor_key=monitor_key, table_name="t", field_count=1)

    result = reconcile_finding(
        db, urn=urn, state=STATE_MAPPED, system_id=system_id, reason=None,
        decided_by="tester",
    )

    assert result.system_id == system_id
    assert result.system_name == system_name


# --- append-only + history --------------------------------------------------


def test_reconciling_twice_appends_rather_than_updates(db):
    monitor_key = _monitor_key()
    system_id = _real_system_id(db)
    urn = _stage_table(db, monitor_key=monitor_key, table_name="t", field_count=1)

    reconcile_finding(
        db, urn=urn, state=STATE_IGNORED, system_id=None,
        reason="first look: nothing here", decided_by="carol",
    )
    reconcile_finding(
        db, urn=urn, state=STATE_MAPPED, system_id=system_id, reason=None,
        decided_by="josephine",
    )

    history = reconciliation_history(db, urn)

    assert len(history) == 2, "re-reconciling must append, never update in place"
    decided_bys = {h.decided_by for h in history}
    assert decided_bys == {"carol", "josephine"}


def test_history_is_empty_for_a_real_table_never_reconciled(db):
    monitor_key = _monitor_key()
    urn = _stage_table(db, monitor_key=monitor_key, table_name="t", field_count=1)

    assert reconciliation_history(db, urn) == []


def test_list_findings_reflects_the_latest_reconciliation_across_two_transactions(db):
    # reconcile_finding's own INSERT uses server_default now() for
    # decided_at, which resolves ONCE PER TRANSACTION on Postgres — two
    # writes in the SAME uncommitted transaction can tie. This is the same
    # limitation gate.py's own _SELECT_DECISIONS_SQL comment already
    # documents for screening decisions, and it does not affect real usage
    # (each API call is its own committed transaction, so decided_at
    # differs). Proven here across two real, separately-committed
    # transactions on a throwaway monitor key, with an explicit cleanup so
    # the shared database is left exactly as found.
    engine = sqlalchemy.create_engine(DB_URL)
    monitor_key = _monitor_key()
    system_id = _real_system_id(db)
    db.flush()  # make this fixture's own inserts visible to other sessions
    with Session(engine) as setup:
        urn = _stage_table(setup, monitor_key=monitor_key, table_name="t", field_count=1)
        setup.commit()
    try:
        with Session(engine) as first:
            reconcile_finding(
                first, urn=urn, state=STATE_MAPPED, system_id=system_id,
                reason=None, decided_by="first",
            )
            first.commit()
        with Session(engine) as second:
            reconcile_finding(
                second, urn=urn, state=STATE_IGNORED, system_id=None,
                reason="reconsidered", decided_by="second",
            )
            second.commit()
        with Session(engine) as reader:
            [finding] = list_findings(reader, monitor_key=monitor_key)
            assert finding.state == STATE_IGNORED
            assert finding.decided_by == "second"
    finally:
        with Session(engine) as cleanup:
            cleanup.execute(
                sqlalchemy.text(
                    "DELETE FROM privacycare_discovery_reconciliation "
                    "WHERE stagedresource_urn = :urn"
                ),
                {"urn": urn},
            )
            cleanup.execute(
                sqlalchemy.text(
                    "DELETE FROM stagedresource WHERE monitor_config_id = :key"
                ),
                {"key": monitor_key},
            )
            cleanup.commit()
