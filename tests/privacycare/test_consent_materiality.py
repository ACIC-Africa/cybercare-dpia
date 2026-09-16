"""When does a notice change invalidate consent already given?

Spec D-CON-2. Gaining a data use asks the subject to agree to something they
never saw, so consent given before it is no longer informed. Losing a use
narrows processing and cannot invalidate agreement already given. Wording
changes are not material on their own — otherwise every typo fix would
re-consent an entire customer base, and a rule that cries wolf gets switched
off, which is worse than not having it.

The predicate is PROVISIONAL and owned by Carol (OQ-CON-01), which is why it
lives in a configuration row rather than in this module's logic.
"""
import pytest
import sqlalchemy
from sqlalchemy.orm import Session

from fides.api.privacycare.consent.materiality import (
    RULE_GAINED_USE,
    active_rule,
    added_uses,
    is_materially_different,
    seed_consent_rule,
)

DB_URL = "postgresql://postgres:fides@127.0.0.1:5442/fides"


@pytest.fixture
def db(monkeypatch):
    engine = sqlalchemy.create_engine(DB_URL)
    with Session(engine) as session:
        monkeypatch.setattr(session, "commit", session.flush)
        yield session
        session.rollback()


def test_gaining_a_use_is_material():
    assert is_materially_different(
        ["marketing.advertising"],
        ["marketing.advertising", "marketing.advertising.third_party"],
    )


def test_losing_a_use_is_not_material():
    # Narrowing processing cannot invalidate agreement already given: the
    # subject consented to MORE than we now do.
    assert not is_materially_different(
        ["marketing.advertising", "marketing.advertising.third_party"],
        ["marketing.advertising"],
    )


def test_an_unchanged_set_is_not_material():
    assert not is_materially_different(["marketing.advertising"], ["marketing.advertising"])


def test_order_and_duplicates_do_not_matter():
    # data_uses is a set in meaning even though it is an array in storage.
    assert not is_materially_different(
        ["a", "b", "b"], ["b", "a"],
    )


def test_a_simultaneous_gain_and_loss_is_material():
    # The gain is what matters; the loss does not cancel it out.
    assert is_materially_different(["a", "b"], ["a", "c"])


def test_added_uses_names_exactly_what_was_gained():
    # D-CON-4: the report must say WHAT changed, not just that something did.
    assert added_uses(["a"], ["a", "c", "b"]) == ["b", "c"]
    assert added_uses(["a", "b"], ["a"]) == []


def test_an_empty_earlier_set_gaining_anything_is_material():
    assert is_materially_different([], ["a"])


def test_both_empty_is_not_material():
    assert not is_materially_different([], [])


def _clear_rules(db):
    # The live DB may already hold a committed rule row from the Task 4 seed.
    # Clearing inside the rolled-back session makes these tests independent of
    # what the deployment happens to hold, without touching the real row.
    db.execute(sqlalchemy.text("DELETE FROM privacycare_consent_rule"))


def test_the_rule_is_seeded_and_readable(db):
    _clear_rules(db)
    seed_consent_rule(db)
    assert active_rule(db) == RULE_GAINED_USE


def test_seeding_twice_does_not_duplicate(db):
    _clear_rules(db)
    seed_consent_rule(db)
    seed_consent_rule(db)
    count = db.execute(
        sqlalchemy.text("SELECT count(*) FROM privacycare_consent_rule")
    ).scalar()
    assert count == 1


def test_an_unseeded_rule_is_an_error_not_a_silent_default(db):
    # The same trap the DSR register hit: a NULL that means "not configured"
    # must not be readable as a meaningful value.
    _clear_rules(db)
    with pytest.raises(ValueError, match="privacycare_consent_rule"):
        active_rule(db)


def test_a_second_row_is_rejected_rather_than_silently_picked(db):
    # Two rows means the table's only sanctioned invariant (exactly one row)
    # has already been violated by some writer other than seed_consent_rule
    # (whose fixed id is the only thing that makes ON CONFLICT DO NOTHING
    # idempotent). Picking one via ORDER BY/LIMIT would hide that violation
    # behind whichever row was touched most recently — this is not a tie to
    # break, it is a config state nobody should be able to reach silently.
    _clear_rules(db)
    seed_consent_rule(db)
    db.execute(
        sqlalchemy.text(
            "INSERT INTO privacycare_consent_rule (id, rule) "
            "VALUES ('some-other-writer-row', 'gained_data_use')"
        )
    )
    with pytest.raises(ValueError, match="privacycare_consent_rule"):
        active_rule(db)


def test_an_unknown_rule_is_rejected():
    with pytest.raises(ValueError, match="vibes"):
        is_materially_different(["a"], ["b"], rule="vibes")
