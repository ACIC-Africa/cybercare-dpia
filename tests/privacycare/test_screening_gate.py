# The screening verdict itself (plan 18, Task 2). Carol's rule is one
# sentence: any single trigger means a DPIA is required; none means the
# activity is screened out and needs a one-line justification. No
# weighting, no threshold, no scoring — this is a gate, not a risk
# assessment.
#
# UPDATE (Task 5, spec 2026-09-17, plan 18): the authorized `--commit` seed
# has now run against this same live database and Carol's six trigger rows
# are meant to stay there permanently (same as the Kenya template — see
# test_seed_kenya_template.py's own UPDATE note). The `triggers` fixture
# below is written as INSERT ... ON CONFLICT (trigger_key) DO NOTHING so it
# stays a no-op against the six real rows rather than raising
# UniqueViolation; this file never asserts on label/description content
# (only trigger_key identity and display_order, both unchanged by the real
# seed), so reading back Carol's real rows instead of this fixture's own
# throwaway ones changes nothing any test here checks.
import uuid

import pytest
import sqlalchemy
from sqlalchemy.orm import Session

from fides.api.privacycare.screening.gate import (
    ScreeningVerdict,
    current_verdict,
    decision_history,
    is_screened_out,
    list_triggers,
    record_decision,
)
from tests.privacycare.test_context import _seed_declaration, _seed_system

DB_URL = "postgresql://postgres:fides@127.0.0.1:5442/fides"

# The six trigger keys, in the order Task 1's seed script assigns them
# display_order. Not Carol's exact label/description text — that fidelity
# is test_seed_screening_triggers.py's job; this file only needs stable
# keys to screen against.
TRIGGER_KEYS = (
    "large_scale",
    "special_category",
    "systematic_monitoring",
    "new_technology",
    "automated_decision",
    "vulnerable_subjects",
)


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


@pytest.fixture
def triggers(db):
    """Ensures the six trigger rows this test file needs exist, inside the
    same rolled-back session every test uses. ON CONFLICT (trigger_key) DO
    NOTHING: Task 5's real seed means these six keys already exist in the
    live database, so this is a no-op there and a real insert only against
    a from-empty database (e.g. CI) — either way, the six keys this file
    screens against are guaranteed present by the time this fixture
    returns."""
    for order, key in enumerate(TRIGGER_KEYS, start=1):
        db.execute(
            sqlalchemy.text(
                "INSERT INTO privacycare_screening_trigger "
                "(id, trigger_key, label, description, display_order) "
                "VALUES (:id, :key, :label, :description, :order) "
                "ON CONFLICT (trigger_key) DO NOTHING"
            ),
            {
                "id": str(uuid.uuid4()),
                "key": key,
                "label": key.replace("_", " ").title(),
                "description": f"Test description for {key}.",
                "order": order,
            },
        )
    return TRIGGER_KEYS


@pytest.fixture
def declaration_id(db) -> str:
    system_id = _seed_system(db, f"sys_{uuid.uuid4().hex[:8]}")
    return _seed_declaration(db, system_id, "marketing")


# --- list_triggers -----------------------------------------------------


def test_list_triggers_is_ordered_by_display_order(db, triggers):
    rows = list_triggers(db)
    assert [row["trigger_key"] for row in rows] == list(TRIGGER_KEYS)


def test_list_triggers_is_empty_when_nothing_is_seeded(db):
    # Task 5's real seed means the live table is no longer empty by
    # default (and is not meant to become empty again — Carol's six rows
    # are permanent, same as the Kenya template). Proving list_triggers
    # returns [] for an empty table still means clearing it, just inside
    # this test's own rolled-back transaction rather than assuming the
    # live database starts that way — the DELETE never escapes past this
    # test's own session.rollback() teardown.
    db.execute(sqlalchemy.text("DELETE FROM privacycare_screening_trigger"))
    assert list_triggers(db) == []


# --- the derivation rule -------------------------------------------------


def test_one_trigger_means_a_dpia_is_required(db, triggers, declaration_id):
    verdict = record_decision(
        db,
        declaration_id=declaration_id,
        triggered_keys=["large_scale"],
        justification=None,
        decided_by="carol@example.com",
    )

    assert isinstance(verdict, ScreeningVerdict)
    assert verdict.dpia_required is True
    assert verdict.triggered_keys == ["large_scale"]
    assert verdict.justification is None


def test_all_six_triggers_mean_a_dpia_is_required(db, triggers, declaration_id):
    verdict = record_decision(
        db,
        declaration_id=declaration_id,
        triggered_keys=list(TRIGGER_KEYS),
        justification=None,
        decided_by="carol@example.com",
    )

    assert verdict.dpia_required is True
    assert verdict.triggered_keys == sorted(TRIGGER_KEYS)


def test_no_triggers_means_screened_out(db, triggers, declaration_id):
    verdict = record_decision(
        db,
        declaration_id=declaration_id,
        triggered_keys=[],
        justification="Internal-only headcount report, no external sharing.",
        decided_by="carol@example.com",
    )

    assert verdict.dpia_required is False
    assert verdict.triggered_keys == []
    assert verdict.justification == "Internal-only headcount report, no external sharing."


def test_dpia_required_cannot_be_passed_in_by_the_caller(db, triggers, declaration_id):
    # There is no dpia_required parameter at all — it is derived, never
    # accepted, so a caller cannot tick three triggers and also declare no
    # DPIA needed. Confirmed here two ways: the interface itself rejects an
    # attempt to pass it, and passing every trigger key still comes back
    # required regardless of what a caller might have wished for.
    with pytest.raises(TypeError):
        record_decision(
            db,
            declaration_id=declaration_id,
            triggered_keys=["large_scale"],
            justification=None,
            decided_by="carol@example.com",
            dpia_required=False,
        )

    verdict = record_decision(
        db,
        declaration_id=declaration_id,
        triggered_keys=["large_scale", "new_technology"],
        justification=None,
        decided_by="carol@example.com",
    )
    assert verdict.dpia_required is True


# --- justification rules --------------------------------------------------


def test_a_screen_out_without_a_justification_is_rejected(db, triggers, declaration_id):
    with pytest.raises(ValueError, match="justification"):
        record_decision(
            db,
            declaration_id=declaration_id,
            triggered_keys=[],
            justification=None,
            decided_by="carol@example.com",
        )


def test_a_screen_out_with_a_blank_justification_is_rejected(db, triggers, declaration_id):
    with pytest.raises(ValueError, match="justification"):
        record_decision(
            db,
            declaration_id=declaration_id,
            triggered_keys=[],
            justification="   ",
            decided_by="carol@example.com",
        )


def test_a_screen_in_with_a_justification_is_rejected(db, triggers, declaration_id):
    with pytest.raises(ValueError, match="justification"):
        record_decision(
            db,
            declaration_id=declaration_id,
            triggered_keys=["large_scale"],
            justification="Not needed but here's one anyway.",
            decided_by="carol@example.com",
        )


# --- unknown inputs ---------------------------------------------------


def test_an_unknown_trigger_key_is_rejected_by_name(db, triggers, declaration_id):
    with pytest.raises(ValueError, match="not_a_real_trigger"):
        record_decision(
            db,
            declaration_id=declaration_id,
            triggered_keys=["large_scale", "not_a_real_trigger"],
            justification=None,
            decided_by="carol@example.com",
        )


def test_an_unknown_declaration_id_is_rejected(db, triggers):
    with pytest.raises(ValueError, match="no such declaration"):
        record_decision(
            db,
            declaration_id="decl_does_not_exist",
            triggered_keys=["large_scale"],
            justification=None,
            decided_by="carol@example.com",
        )


# --- append-only ---------------------------------------------------------


def _decision_rows(db, declaration_id: str) -> list:
    return db.execute(
        sqlalchemy.text(
            "SELECT id, dpia_required, justification, decided_at FROM "
            "privacycare_screening_decision WHERE declaration_id = :id"
        ),
        {"id": declaration_id},
    ).all()


def test_recording_twice_appends_rather_than_updates(db, triggers, declaration_id):
    first = record_decision(
        db,
        declaration_id=declaration_id,
        triggered_keys=[],
        justification="No processing risk identified this quarter.",
        decided_by="carol@example.com",
    )

    rows_after_first = _decision_rows(db, declaration_id)
    assert len(rows_after_first) == 1
    first_row_before = dict(rows_after_first[0]._mapping)

    second = record_decision(
        db,
        declaration_id=declaration_id,
        triggered_keys=["large_scale"],
        justification=None,
        decided_by="michael@example.com",
    )

    rows_after_second = _decision_rows(db, declaration_id)
    assert len(rows_after_second) == 2

    # The first row is unchanged — re-screening appended, it never updated.
    unchanged_row = next(
        dict(r._mapping) for r in rows_after_second
        if dict(r._mapping)["id"] == first_row_before["id"]
    )
    assert unchanged_row == first_row_before
    assert len({r.id for r in rows_after_second}) == 2
    assert second.dpia_required is True


# --- current_verdict / decision_history -----------------------------------


def test_current_verdict_is_none_for_a_declaration_never_screened(db, declaration_id):
    assert current_verdict(db, declaration_id) is None


def _backdate(db, declaration_id: str, triggered_keys: list[str], *, hours: int) -> None:
    """Pushes one decision's decided_at into the past by hand.

    Postgres' now() (this column's server_default) is the TRANSACTION's
    start time, not the statement's — every row this test's still-open,
    never-committed session inserts shares one identical decided_at unless
    something moves it. Backdating the earlier decision is what makes
    "newest by decided_at" an actual, observable ordering in this test
    rather than an accidental tie resolved by the id tie-break instead
    (that tie-break has its own dedicated test)."""
    db.execute(
        sqlalchemy.text(
            "UPDATE privacycare_screening_decision "
            "SET decided_at = decided_at - (:hours || ' hours')::interval "
            "WHERE declaration_id = :decl_id AND triggered_keys = :keys"
        ),
        {"hours": hours, "decl_id": declaration_id, "keys": triggered_keys},
    )


def test_current_verdict_returns_the_newest_decision(db, triggers, declaration_id):
    record_decision(
        db, declaration_id=declaration_id, triggered_keys=[],
        justification="First pass: nothing ticked.", decided_by="carol@example.com",
    )
    _backdate(db, declaration_id, [], hours=1)
    second = record_decision(
        db, declaration_id=declaration_id, triggered_keys=["special_category"],
        justification=None, decided_by="carol@example.com",
    )

    verdict = current_verdict(db, declaration_id)

    assert verdict is not None
    assert verdict.dpia_required is True
    assert verdict.triggered_keys == ["special_category"]
    assert verdict.decided_at == second.decided_at


def test_decision_history_returns_both_decisions_newest_first(db, triggers, declaration_id):
    first = record_decision(
        db, declaration_id=declaration_id, triggered_keys=[],
        justification="First pass: nothing ticked.", decided_by="carol@example.com",
    )
    _backdate(db, declaration_id, [], hours=1)
    second = record_decision(
        db, declaration_id=declaration_id, triggered_keys=["special_category"],
        justification=None, decided_by="carol@example.com",
    )

    history = decision_history(db, declaration_id)

    assert len(history) == 2
    assert history[0].decided_at >= history[1].decided_at
    assert {h.dpia_required for h in history} == {first.dpia_required, second.dpia_required}
    assert history[0].triggered_keys == second.triggered_keys
    assert history[1].triggered_keys == first.triggered_keys


def test_current_verdict_tie_break_is_deterministic_by_id(db, triggers, declaration_id):
    # Force a genuine tie: two decisions that share the exact same
    # decided_at. record_decision itself has no way to pass decided_at (the
    # column defaults it), so the tie is manufactured directly against the
    # table after the fact — see gate.py's current_verdict/decision_history
    # comment for why ties break by id rather than being left
    # nondeterministic.
    first = record_decision(
        db, declaration_id=declaration_id, triggered_keys=["large_scale"],
        justification=None, decided_by="carol@example.com",
    )
    second = record_decision(
        db, declaration_id=declaration_id, triggered_keys=["new_technology"],
        justification=None, decided_by="carol@example.com",
    )
    db.execute(
        sqlalchemy.text(
            "UPDATE privacycare_screening_decision SET decided_at = :ts "
            "WHERE declaration_id = :decl_id"
        ),
        {"ts": second.decided_at, "decl_id": declaration_id},
    )

    # ScreeningVerdict carries no id (it is not part of the dataclass's
    # published shape), so the row ids that decide the tie are read back
    # directly — each row is uniquely identifiable here by its
    # triggered_keys, which first and second do not share.
    id_by_triggers = {
        tuple(row.triggered_keys): row.id
        for row in db.execute(
            sqlalchemy.text(
                "SELECT id, triggered_keys FROM privacycare_screening_decision "
                "WHERE declaration_id = :decl_id"
            ),
            {"decl_id": declaration_id},
        ).all()
    }
    first_id = id_by_triggers[tuple(first.triggered_keys)]
    second_id = id_by_triggers[tuple(second.triggered_keys)]
    by_id = {first_id: first, second_id: second}
    expected_winner = by_id[max(by_id)]
    expected_loser = by_id[min(by_id)]

    # Calling current_verdict/decision_history repeatedly against the exact
    # same tied rows must keep naming the same winner — the whole point of
    # a deterministic tie-break, as opposed to whatever order Postgres
    # happens to hand rows back in.
    for _ in range(3):
        verdict = current_verdict(db, declaration_id)
        assert verdict is not None
        assert verdict.triggered_keys == expected_winner.triggered_keys

    history = decision_history(db, declaration_id)
    assert [h.triggered_keys for h in history] == [
        expected_winner.triggered_keys,
        expected_loser.triggered_keys,
    ]


# --- is_screened_out -------------------------------------------------------


def test_is_screened_out_is_false_for_a_declaration_never_screened(db, declaration_id):
    # Not an error, and not true: an unscreened activity has not been
    # screened out, and the gate is opt-in.
    assert is_screened_out(db, declaration_id) is False


def test_is_screened_out_is_true_after_a_screen_out_decision(db, triggers, declaration_id):
    record_decision(
        db, declaration_id=declaration_id, triggered_keys=[],
        justification="Nothing ticked.", decided_by="carol@example.com",
    )
    assert is_screened_out(db, declaration_id) is True


def test_is_screened_out_is_false_after_a_dpia_required_decision(db, triggers, declaration_id):
    record_decision(
        db, declaration_id=declaration_id, triggered_keys=["large_scale"],
        justification=None, decided_by="carol@example.com",
    )
    assert is_screened_out(db, declaration_id) is False


def test_is_screened_out_reflects_the_latest_decision_only(db, triggers, declaration_id):
    record_decision(
        db, declaration_id=declaration_id, triggered_keys=[],
        justification="Screened out last quarter.", decided_by="carol@example.com",
    )
    assert is_screened_out(db, declaration_id) is True

    # Backdate the first decision — see _backdate's docstring: this test's
    # session never commits, so both decisions would otherwise share one
    # identical, transaction-scoped decided_at and this assertion would
    # depend on the id tie-break instead of proving "latest" actually means
    # "latest."
    _backdate(db, declaration_id, [], hours=1)
    record_decision(
        db, declaration_id=declaration_id, triggered_keys=["automated_decision"],
        justification=None, decided_by="carol@example.com",
    )
    assert is_screened_out(db, declaration_id) is False
