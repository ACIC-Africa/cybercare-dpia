# The DPIA risk register: each row is one identified risk, scored and
# banded through Task 1's pure banding module (fides.api.privacycare.risk.
# banding) rather than a second copy of the arithmetic. score and band are
# never stored — see models.py's dpia_risk_table comment — so every
# assertion here reads them back computed, not persisted.
import threading
import time
import uuid

import pytest
import sqlalchemy
from sqlalchemy.orm import Session

from fides.api.privacycare.risk.banding import CRITICAL, HIGH, LOW, MEDIUM, band, score
from fides.api.privacycare.risk.register import (
    RiskEntry,
    add_risk,
    assessment_band,
    list_risks,
    remove_risk,
    sync_projection,
)

DB_URL = "postgresql://postgres:fides@127.0.0.1:5442/fides"


@pytest.fixture
def db(monkeypatch):
    engine = sqlalchemy.create_engine(DB_URL)
    with Session(engine) as session:
        # NEVER a no-op: base_class.persist_obj does add/commit/refresh, and
        # a no-op commit starves refresh(). flush() gives write visibility
        # within the transaction without making it durable; rollback() on
        # teardown discards everything this test wrote.
        monkeypatch.setattr(session, "commit", session.flush)
        yield session
        session.rollback()


def _seed_template(db) -> str:
    # assessment_type/region are NOT NULL on the live table; assessment_type
    # defaults to a unique value so parallel tests never collide on
    # uq_assessment_template_active_type (see test_api_assessments.py's
    # _seed_template for the full story).
    tid = f"tpl_{uuid.uuid4().hex[:8]}"
    db.execute(
        sqlalchemy.text(
            "INSERT INTO assessment_template "
            "(id, version, name, assessment_type, region, is_active) "
            "VALUES (:id, '1.0', 'Kenya DPA 2019 DPIA', :assessment_type, 'KE', true)"
        ),
        {"id": tid, "assessment_type": f"dpia_{uuid.uuid4().hex[:8]}"},
    )
    return tid


def _seed_assessment(db, template_id: str) -> str:
    aid = f"asmt_{uuid.uuid4().hex[:8]}"
    db.execute(
        sqlalchemy.text(
            "INSERT INTO privacy_assessment "
            "(id, template_id, name, status, system_fides_key) "
            "VALUES (:id, :tid, 'Fuel card DPIA', 'in_progress', 'sys_test')"
        ),
        {"id": aid, "tid": template_id},
    )
    return aid


@pytest.fixture
def assessment_id(db) -> str:
    return _seed_assessment(db, _seed_template(db))


def test_a_risk_round_trips_with_its_score_and_band_computed(db, assessment_id):
    entry = add_risk(
        db,
        assessment_id=assessment_id,
        category="confidentiality",
        description="Fuel card PINs logged in plaintext to the ops console.",
        likelihood=4,
        severity=3,
    )

    assert isinstance(entry, RiskEntry)
    assert entry.assessment_id == assessment_id
    assert entry.category == "confidentiality"
    assert entry.likelihood == 4
    assert entry.severity == 3
    # Computed through banding.score/band, not re-implemented.
    assert entry.score == score(4, 3) == 12
    assert entry.band == band(12) == HIGH

    [row] = list_risks(db, assessment_id)
    assert row == entry


@pytest.mark.parametrize("bad", [0, 6, -1, 25])
def test_likelihood_or_severity_outside_one_to_five_is_rejected_before_anything_is_written(
    db, assessment_id, bad
):
    # A 5x5 matrix has no cell for a 6 (or a 0). Rejected up front, before the
    # database is ever reached — nothing is written for either offending arg.
    with pytest.raises(ValueError):
        add_risk(
            db,
            assessment_id=assessment_id,
            category="confidentiality",
            description="bad likelihood",
            likelihood=bad,
            severity=3,
        )
    with pytest.raises(ValueError):
        add_risk(
            db,
            assessment_id=assessment_id,
            category="confidentiality",
            description="bad severity",
            likelihood=3,
            severity=bad,
        )

    assert list_risks(db, assessment_id) == []


def test_a_category_outside_the_seven_is_rejected_by_name(db, assessment_id):
    # Carol's seven categories carry across unchanged; anything else is
    # rejected by name so the error is actionable, not just "invalid".
    with pytest.raises(ValueError, match="sabotage"):
        add_risk(
            db,
            assessment_id=assessment_id,
            category="sabotage",
            description="not one of the seven",
            likelihood=3,
            severity=3,
        )

    assert list_risks(db, assessment_id) == []


def test_list_risks_orders_by_score_descending(db, assessment_id):
    low = add_risk(
        db, assessment_id=assessment_id, category="availability",
        description="minor", likelihood=1, severity=2,
    )
    high = add_risk(
        db, assessment_id=assessment_id, category="integrity",
        description="major", likelihood=5, severity=5,
    )
    mid = add_risk(
        db, assessment_id=assessment_id, category="physical_harm",
        description="middling", likelihood=3, severity=3,
    )

    rows = list_risks(db, assessment_id)

    assert [r.id for r in rows] == [high.id, mid.id, low.id]
    assert [r.score for r in rows] == sorted((r.score for r in rows), reverse=True)


def test_assessment_band_is_the_band_of_the_maximum(db, assessment_id):
    # One critical risk among trivial ones: the maximum wins, not the
    # average (same rule test_risk_banding.py locks down for overall_band
    # itself — this proves the register wires it up correctly).
    add_risk(db, assessment_id=assessment_id, category="confidentiality",
              description="trivial", likelihood=1, severity=1)
    add_risk(db, assessment_id=assessment_id, category="discrimination",
              description="trivial too", likelihood=1, severity=1)
    add_risk(db, assessment_id=assessment_id, category="financial_or_reputational_harm",
              description="the one that matters", likelihood=5, severity=5)

    assert assessment_band(db, assessment_id) == CRITICAL


def test_an_assessment_with_no_risks_is_low(db, assessment_id):
    assert list_risks(db, assessment_id) == []
    assert assessment_band(db, assessment_id) == LOW


def test_removing_a_risk_changes_the_band_back(db, assessment_id):
    add_risk(db, assessment_id=assessment_id, category="loss_of_autonomy",
              description="stays", likelihood=2, severity=2)
    critical = add_risk(db, assessment_id=assessment_id, category="integrity",
                          description="goes", likelihood=5, severity=5)
    assert assessment_band(db, assessment_id) == CRITICAL

    removed = remove_risk(db, critical.id)

    assert removed is True
    assert assessment_band(db, assessment_id) == LOW
    remaining_ids = {r.id for r in list_risks(db, assessment_id)}
    assert critical.id not in remaining_ids


def test_remove_risk_on_a_missing_id_returns_false(db):
    assert remove_risk(db, str(uuid.uuid4())) is False


def test_risks_for_one_assessment_never_appear_under_another(db):
    template_id = _seed_template(db)
    assessment_a = _seed_assessment(db, template_id)
    assessment_b = _seed_assessment(db, template_id)

    add_risk(db, assessment_id=assessment_a, category="confidentiality",
              description="belongs to A", likelihood=3, severity=3)
    add_risk(db, assessment_id=assessment_b, category="integrity",
              description="belongs to B", likelihood=2, severity=2)

    a_rows = list_risks(db, assessment_a)
    b_rows = list_risks(db, assessment_b)

    assert len(a_rows) == 1 and a_rows[0].description == "belongs to A"
    assert len(b_rows) == 1 and b_rows[0].description == "belongs to B"


def test_add_risk_rejects_an_assessment_that_does_not_exist(db):
    # assessment_id carries no ForeignKey (deliberately — see models.py), so
    # existence is checked here at write time instead.
    with pytest.raises(ValueError, match="no such assessment"):
        add_risk(
            db,
            assessment_id=str(uuid.uuid4()),
            category="confidentiality",
            description="orphaned",
            likelihood=3,
            severity=3,
        )


def _assessment_row(db, assessment_id: str) -> dict:
    row = db.execute(
        sqlalchemy.text("SELECT * FROM privacy_assessment WHERE id = :id"),
        {"id": assessment_id},
    ).mappings().first()
    assert row is not None
    return dict(row)


def test_sync_projection_writes_only_risk_level_and_nothing_else_on_the_row(
    db, assessment_id
):
    # add_risk already resyncs risk_level to the correct value, so to prove
    # sync_projection actually *moves* the column (rather than this
    # assertion vacuously passing because nothing changed), go stale by
    # hand first — the same way a live row could end up wrong before a
    # resync runs.
    add_risk(db, assessment_id=assessment_id, category="confidentiality",
              description="something", likelihood=3, severity=3)
    db.execute(
        sqlalchemy.text(
            "UPDATE privacy_assessment SET risk_level = 'low' WHERE id = :id"
        ),
        {"id": assessment_id},
    )
    before = _assessment_row(db, assessment_id)
    assert before["risk_level"] == LOW

    sync_projection(db, assessment_id)

    after = _assessment_row(db, assessment_id)
    changed = {k for k in before if before[k] != after[k]}
    assert changed == {"risk_level"}
    assert after["risk_level"] == MEDIUM


def test_sync_projection_returns_our_unprojected_band_while_the_column_holds_the_projection(
    db, assessment_id
):
    # A critical risk collapses to "high" on Ethyca's three-value column, but
    # the caller (e.g. an ODPC-escalation decision in Task 4) must still be
    # able to tell critical from high, so the return value must not collapse.
    add_risk(db, assessment_id=assessment_id, category="integrity",
              description="catastrophic", likelihood=5, severity=5)

    returned = sync_projection(db, assessment_id)

    assert returned == CRITICAL
    row = _assessment_row(db, assessment_id)
    assert row["risk_level"] == HIGH
    assert returned != row["risk_level"]


def test_sync_projection_on_an_empty_register_writes_low(db, assessment_id):
    assert list_risks(db, assessment_id) == []

    returned = sync_projection(db, assessment_id)

    assert returned == LOW
    row = _assessment_row(db, assessment_id)
    assert row["risk_level"] == LOW


def test_sync_projection_is_idempotent(db, assessment_id):
    add_risk(db, assessment_id=assessment_id, category="physical_harm",
              description="moderate", likelihood=3, severity=3)

    first = sync_projection(db, assessment_id)
    row_after_first = _assessment_row(db, assessment_id)
    second = sync_projection(db, assessment_id)
    row_after_second = _assessment_row(db, assessment_id)

    assert first == second == MEDIUM
    assert row_after_first == row_after_second


def test_sync_projection_overwrites_a_hand_typed_value(db, assessment_id):
    # Nothing of ours offers a band/risk-level setter, but Ethyca's own API
    # still accepts risk_level directly (_UPDATABLE_ASSESSMENT_FIELDS). A
    # value typed in by hand must not survive the next sync — that is what
    # makes "computed, never entered" true rather than merely intended.
    db.execute(
        sqlalchemy.text(
            "UPDATE privacy_assessment SET risk_level = 'high' WHERE id = :id"
        ),
        {"id": assessment_id},
    )
    assert _assessment_row(db, assessment_id)["risk_level"] == HIGH

    sync_projection(db, assessment_id)

    # No risks recorded, so the computed truth is LOW, overwriting the
    # hand-typed HIGH.
    assert _assessment_row(db, assessment_id)["risk_level"] == LOW


def test_add_risk_moves_ethycas_risk_level_column(db, assessment_id):
    assert _assessment_row(db, assessment_id)["risk_level"] is None

    add_risk(db, assessment_id=assessment_id, category="confidentiality",
              description="pushes the column", likelihood=5, severity=5)

    assert _assessment_row(db, assessment_id)["risk_level"] == HIGH


def test_remove_risk_moves_ethycas_risk_level_column(db, assessment_id):
    stays = add_risk(db, assessment_id=assessment_id, category="availability",
                       description="stays", likelihood=1, severity=1)
    goes = add_risk(db, assessment_id=assessment_id, category="integrity",
                      description="goes", likelihood=5, severity=5)
    assert _assessment_row(db, assessment_id)["risk_level"] == HIGH

    removed = remove_risk(db, goes.id)

    assert removed is True
    # Only the trivial risk remains: LOW.
    assert _assessment_row(db, assessment_id)["risk_level"] == LOW


# --- IMPORTANT-1 (final whole-branch review): the lost-update race in
# sync_projection. Every test above shares the module-level `db` fixture,
# which is ONE Session per test with commit monkeypatched to flush and a
# rollback on teardown — deliberately, so tests never pollute the live
# database. That fixture cannot exercise a cross-transaction race: a single
# session has no "other transaction" to race against, and READ COMMITTED
# visibility only diverges across separate connections. Reproducing the
# race for real needs two independent engines/sessions that actually
# commit, so this one small section uses its own fixture and cleans up by
# hand (DELETE, not rollback) rather than borrowing `db`/`assessment_id`.
@pytest.fixture
def committed_assessment_id():
    """A template + assessment that are REALLY committed, not just flushed
    within one still-open session — required so two independent sessions
    opened in the test below can each see the row via their own READ
    COMMITTED snapshot. Cleaned up by hand on teardown since nothing here
    is going to roll back."""
    engine = sqlalchemy.create_engine(DB_URL)
    with Session(engine) as session:
        template_id = _seed_template(session)
        assessment_id = _seed_assessment(session, template_id)
        session.commit()

    yield assessment_id

    with Session(sqlalchemy.create_engine(DB_URL)) as session:
        session.execute(
            sqlalchemy.text(
                "DELETE FROM privacycare_dpia_risk WHERE assessment_id = :id"
            ),
            {"id": assessment_id},
        )
        session.execute(
            sqlalchemy.text("DELETE FROM privacy_assessment WHERE id = :id"),
            {"id": assessment_id},
        )
        session.execute(
            sqlalchemy.text(
                "DELETE FROM assessment_template WHERE id = :tid"
            ),
            {"tid": template_id},
        )
        session.commit()


def test_concurrent_add_risk_does_not_lose_the_higher_band(committed_assessment_id):
    """Reproduces IMPORTANT-1's scenario with two real sessions on two real
    connections, forced into the exact interleaving the finding describes,
    rather than asserting anything about the SQL text sync_projection
    issues.

    Thread A adds a 5x5 (critical) risk via add_risk and, deliberately,
    does NOT commit yet — its transaction (and, with the fix, the FOR
    UPDATE lock sync_projection now takes on the privacy_assessment row)
    stays open. Thread B is only started once A has finished its own
    add_risk call (INSERT + locked read + UPDATE, all uncommitted), so B's
    add_risk necessarily blocks trying to acquire the same lock. Only after
    B has had time to actually reach and block on that call does the test
    let A commit, unblocking B.

    With the fix (lock taken before the register is read), B's block
    happens BEFORE it reads the register, so once unblocked it re-reads
    post-A's-commit state and correctly recomputes "critical" (-> "high")
    from both risks. Without the fix, B's list_risks runs before it ever
    blocks (the old code's only lock is the UPDATE statement's own implicit
    row lock), so B computes "low" from its own risk alone, blocks on
    A's UPDATE, and once unblocked overwrites A's correct "high" with its
    stale "low". This test asserts the final column is "high", not "low" —
    exactly the durable-wrong-projection failure IMPORTANT-1 describes.
    """
    assessment_id = committed_assessment_id

    a_ready = threading.Event()
    release_a = threading.Event()
    errors: list[BaseException] = []

    def txn_a():
        try:
            engine_a = sqlalchemy.create_engine(DB_URL)
            with Session(engine_a) as session_a:
                add_risk(
                    session_a,
                    assessment_id=assessment_id,
                    category="integrity",
                    description="Fuel depot SCADA control-plane takeover.",
                    likelihood=5,
                    severity=5,
                )
                # Transaction intentionally left open here: the row lock
                # sync_projection took (fixed code) or the UPDATE's own
                # implicit lock (either way) is still held.
                a_ready.set()
                held = release_a.wait(timeout=10)
                if not held:
                    raise AssertionError("txn_a: release_a was never set")
                session_a.commit()
        except BaseException as exc:  # noqa: BLE001 - surfaced via errors list
            errors.append(exc)

    def txn_b():
        try:
            a_ready.wait(timeout=10)
            engine_b = sqlalchemy.create_engine(DB_URL)
            with Session(engine_b) as session_b:
                # This call blocks inside sync_projection until txn_a
                # commits and releases the row lock.
                add_risk(
                    session_b,
                    assessment_id=assessment_id,
                    category="availability",
                    description="Backup generator fuel gauge under-reports by 2%.",
                    likelihood=1,
                    severity=2,
                )
                session_b.commit()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    thread_a = threading.Thread(target=txn_a)
    thread_b = threading.Thread(target=txn_b)

    thread_a.start()
    assert a_ready.wait(timeout=10), "txn_a never reached its held-open point"
    thread_b.start()
    # Give txn_b time to actually issue add_risk and block on txn_a's lock
    # before txn_a is allowed to commit. Generous relative to local
    # Postgres round-trip latency; the interleaving this proves does not
    # depend on the exact duration, only on B attempting its lock/update
    # before A releases it.
    time.sleep(0.5)
    release_a.set()

    thread_a.join(timeout=10)
    thread_b.join(timeout=10)

    assert not thread_a.is_alive(), "txn_a did not finish"
    assert not thread_b.is_alive(), "txn_b did not finish"
    assert not errors, f"background transaction(s) raised: {errors}"

    with Session(sqlalchemy.create_engine(DB_URL)) as verify:
        final = _assessment_row(verify, assessment_id)

    # The critical risk (score 25) must still win the projection even
    # though the low risk (score 2) was the one whose UPDATE committed
    # last in wall-clock time.
    assert final["risk_level"] == HIGH, (
        "lost update: the register's critical risk was overwritten by the "
        f"low risk's stale read (got risk_level={final['risk_level']!r})"
    )


# --- The migration itself. Statically parsed rather than imported and run:
# proving it creates exactly one table and touches nothing else does not
# require Alembic machinery, and a static check catches an accidental
# second op (e.g. an ALTER TYPE on risklevel) that a purely behavioural test
# against an already-migrated database would not distinguish from "some
# earlier migration did this."

import os
import re

_VERSIONS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "src", "fides", "api", "privacycare", "migrations", "versions",
)


def _find_migration_source(down_revision: str) -> str:
    pattern = re.compile(r"^down_revision\s*=\s*['\"]" + re.escape(down_revision) + r"['\"]")
    for filename in os.listdir(_VERSIONS_DIR):
        if not filename.endswith(".py"):
            continue
        path = os.path.join(_VERSIONS_DIR, filename)
        with open(path, "r") as f:
            source = f.read()
        for line in source.splitlines():
            if pattern.match(line):
                return source
    raise AssertionError(
        f"no migration in {_VERSIONS_DIR} declares down_revision = {down_revision!r}"
    )


def test_the_migration_creates_exactly_one_table_and_touches_no_ethyca_table():
    # This task's migration chains onto bf6d64cab75c (consent_rule), the
    # current head — found by down_revision rather than hardcoding this
    # task's own revision id, so the check still finds the right file if
    # that id is regenerated.
    source = _find_migration_source("bf6d64cab75c")

    assert source.count("op.create_table(") == 1
    assert "'privacycare_dpia_risk'" in source

    # Scope the "touches nothing else" check to upgrade() alone: downgrade()
    # legitimately drops the very table this migration creates, which would
    # otherwise false-positive on "op.drop_table".
    upgrade_body = source.split("def upgrade():", 1)[1].split("def downgrade():", 1)[0]

    # No operation against anything but our own new table: no alter/drop of
    # an existing table or column, and in particular no widening of
    # Ethyca's risklevel enum (checked by actual op/DDL shape, not by
    # banning the word "risklevel" outright — the docstring is allowed to
    # explain the enum it is declining to touch).
    for forbidden in (
        "op.alter_column",
        "op.add_column",
        "op.drop_column",
        "op.drop_table",
        "op.alter_table",
        "ALTER TYPE",
        "ADD VALUE",
        "sa.Enum(",
        "postgresql.ENUM(",
    ):
        assert forbidden not in upgrade_body, (
            f"migration's upgrade() touches something via {forbidden!r}"
        )
