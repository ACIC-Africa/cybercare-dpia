"""Who consented against a notice version that has since gained a use?

Spec D-CON-2/D-CON-4. Fides records which `privacynoticehistory` version a
subject consented against; it never compares that version to the notice's
current one. This is the detector that does — see
`fides.api.privacycare.consent.detector` for the read-only query and why it
is read-only (spec D-CON-3).

Every test builds its own notices, versions and preferences inside a
rolled-back session — no seed dependency (Task 4's demo seed is a separate,
committed concern). `privacycare_consent_rule` is cleared and reseeded by
the `db` fixture below, the same way `test_consent_materiality.py`'s
`_clear_rules` does, because `active_rule` (Task 1) raises on an empty
table and the live DB does not carry a committed rule row yet.
"""
from uuid import uuid4

import pytest
import sqlalchemy
from sqlalchemy.orm import Session

from fides.api.privacycare.consent.detector import StaleConsent, find_stale_consents
from fides.api.privacycare.consent.materiality import seed_consent_rule

DB_URL = "postgresql://postgres:fides@127.0.0.1:5442/fides"

# Every Ethyca table this module's query touches, plus our own rule table —
# used by test_the_detector_writes_nothing to prove the read-only claim.
_CONSENT_TABLES = [
    "privacynotice",
    "noticetranslation",
    "privacynoticehistory",
    "privacypreferencehistory",
    "privacycare_consent_rule",
]


@pytest.fixture
def db(monkeypatch):
    engine = sqlalchemy.create_engine(DB_URL)
    with Session(engine) as session:
        monkeypatch.setattr(session, "commit", session.flush)
        # The live DB may hold zero rows (nobody has run Task 4's seed CLI
        # yet) or a real committed row. Clearing inside the rolled-back
        # session makes every test independent of which, without touching
        # the real row.
        session.execute(sqlalchemy.text("DELETE FROM privacycare_consent_rule"))
        seed_consent_rule(session)
        yield session
        session.rollback()


def make_notice(db, *, key: str, name: str) -> tuple[str, str]:
    """One privacynotice plus one English translation.

    Returns (notice_id, translation_id) — the translation id is what a
    privacynoticehistory row points at, so callers need both.
    """
    notice_id, translation_id = str(uuid4()), str(uuid4())
    db.execute(sqlalchemy.text("""
        INSERT INTO privacynotice
          (id, name, notice_key, consent_mechanism, disabled,
           enforcement_level, has_gpc_flag)
        VALUES (:id, :name, :key, 'opt_in', false, 'system_wide', false)
    """), {"id": notice_id, "name": name, "key": key})
    db.execute(sqlalchemy.text("""
        INSERT INTO noticetranslation (id, language, privacy_notice_id, title)
        VALUES (:tid, 'en', :nid, :title)
    """), {"tid": translation_id, "nid": notice_id, "title": name})
    return notice_id, translation_id


def make_version(db, *, translation_id, key, name, version, data_uses) -> str:
    """One privacynoticehistory row. Returns its id — this is what a
    preference points at, and what carries data_uses and version."""
    history_id = str(uuid4())
    db.execute(sqlalchemy.text("""
        INSERT INTO privacynoticehistory
          (id, name, notice_key, title, version, data_uses, translation_id,
           consent_mechanism, disabled, enforcement_level, has_gpc_flag)
        VALUES (:id, :name, :key, :name, :version, :uses, :tid,
                'opt_in', false, 'system_wide', false)
    """), {"id": history_id, "name": name, "key": key, "version": version,
           "uses": data_uses, "tid": translation_id})
    return history_id


def make_preference(db, *, history_id, preference="opt_in", email=None,
                    device=None) -> str:
    pref_id = str(uuid4())
    db.execute(sqlalchemy.text("""
        INSERT INTO privacypreferencehistory
          (id, preference, privacy_notice_history_id, email, fides_user_device)
        VALUES (:id, :pref, :hid, :email, :device)
    """), {"id": pref_id, "pref": preference, "hid": history_id,
           "email": email, "device": device})
    return pref_id


def test_a_preference_against_a_version_that_later_gained_a_use_is_stale(db):
    _, translation_id = make_notice(db, key="fuel_card", name="Fuel Card Marketing")
    v1 = make_version(
        db, translation_id=translation_id, key="fuel_card", name="Fuel Card Marketing",
        version=1.0, data_uses=["marketing.advertising"],
    )
    make_version(
        db, translation_id=translation_id, key="fuel_card", name="Fuel Card Marketing",
        version=2.0, data_uses=["marketing.advertising", "marketing.advertising.third_party"],
    )
    make_preference(db, history_id=v1, preference="opt_in", email="alice@example.com")

    result = find_stale_consents(db)

    assert len(result) == 1
    stale = result[0]
    assert isinstance(stale, StaleConsent)
    assert stale.subject == "alice@example.com"
    assert stale.subject_kind == "email"
    assert stale.notice_key == "fuel_card"
    assert stale.consented_version == 1.0
    assert stale.live_version == 2.0
    assert stale.added_uses == ["marketing.advertising.third_party"]
    assert stale.preference == "opt_in"


def test_a_preference_against_the_live_version_is_not_stale(db):
    _, translation_id = make_notice(db, key="fuel_card", name="Fuel Card Marketing")
    make_version(
        db, translation_id=translation_id, key="fuel_card", name="Fuel Card Marketing",
        version=1.0, data_uses=["marketing.advertising"],
    )
    v2 = make_version(
        db, translation_id=translation_id, key="fuel_card", name="Fuel Card Marketing",
        version=2.0, data_uses=["marketing.advertising", "marketing.advertising.third_party"],
    )
    make_preference(db, history_id=v2, preference="opt_in", email="alice@example.com")

    assert find_stale_consents(db) == []


def test_a_version_that_only_lost_a_use_does_not_make_consent_stale(db):
    _, translation_id = make_notice(db, key="fuel_card", name="Fuel Card Marketing")
    v1 = make_version(
        db, translation_id=translation_id, key="fuel_card", name="Fuel Card Marketing",
        version=1.0, data_uses=["marketing.advertising", "marketing.advertising.third_party"],
    )
    make_version(
        db, translation_id=translation_id, key="fuel_card", name="Fuel Card Marketing",
        version=2.0, data_uses=["marketing.advertising"],
    )
    make_preference(db, history_id=v1, preference="opt_in", email="alice@example.com")

    # Narrowing processing cannot invalidate agreement already given: the
    # subject consented to MORE than the notice now does.
    assert find_stale_consents(db) == []


def test_a_wording_only_change_does_not_make_consent_stale(db):
    # Same data_uses, different title/description, higher version.
    _, translation_id = make_notice(db, key="fuel_card", name="Fuel Card Marketing")
    v1 = make_version(
        db, translation_id=translation_id, key="fuel_card", name="Fuel Card Marketing",
        version=1.0, data_uses=["marketing.advertising"],
    )
    make_version(
        db, translation_id=translation_id, key="fuel_card",
        name="Fuel Card Marketing (clarified wording)",
        version=2.0, data_uses=["marketing.advertising"],
    )
    make_preference(db, history_id=v1, preference="opt_in", email="alice@example.com")

    assert find_stale_consents(db) == []


def test_an_opted_out_preference_is_never_stale(db):
    # Nothing to re-consent; reporting it would send the subject a request to
    # re-agree to something they declined.
    _, translation_id = make_notice(db, key="fuel_card", name="Fuel Card Marketing")
    v1 = make_version(
        db, translation_id=translation_id, key="fuel_card", name="Fuel Card Marketing",
        version=1.0, data_uses=["marketing.advertising"],
    )
    make_version(
        db, translation_id=translation_id, key="fuel_card", name="Fuel Card Marketing",
        version=2.0, data_uses=["marketing.advertising", "marketing.advertising.third_party"],
    )
    make_preference(db, history_id=v1, preference="opt_out", email="alice@example.com")

    assert find_stale_consents(db) == []


def test_an_acknowledged_notice_only_preference_is_never_stale(db):
    # notice_only notices carry no consent to invalidate in the first place —
    # `acknowledge` records that the notice was shown, not that the subject
    # agreed to anything, so there is nothing to re-consent to either.
    _, translation_id = make_notice(db, key="fuel_card", name="Fuel Card Marketing")
    v1 = make_version(
        db, translation_id=translation_id, key="fuel_card", name="Fuel Card Marketing",
        version=1.0, data_uses=["marketing.advertising"],
    )
    make_version(
        db, translation_id=translation_id, key="fuel_card", name="Fuel Card Marketing",
        version=2.0, data_uses=["marketing.advertising", "marketing.advertising.third_party"],
    )
    make_preference(db, history_id=v1, preference="acknowledge", email="alice@example.com")

    assert find_stale_consents(db) == []


def test_the_report_names_the_subject_the_versions_and_what_was_added(db):
    # D-CON-4: "412 people need re-consent" is a metric; naming them is an
    # action. Every field the dataclass carries must be right, not just
    # "is it stale".
    _, translation_id = make_notice(db, key="fuel_card", name="Fuel Card Marketing")
    v1 = make_version(
        db, translation_id=translation_id, key="fuel_card", name="Fuel Card Marketing",
        version=1.0, data_uses=["marketing.advertising"],
    )
    make_version(
        db, translation_id=translation_id, key="fuel_card", name="Fuel Card Marketing v2",
        version=2.0, data_uses=["marketing.advertising", "marketing.advertising.third_party"],
    )
    make_preference(db, history_id=v1, preference="opt_in", email="bob@example.com")

    result = find_stale_consents(db)

    assert len(result) == 1
    stale = result[0]
    assert stale.subject == "bob@example.com"
    assert stale.subject_kind == "email"
    assert stale.notice_key == "fuel_card"
    # The notice's CURRENT name — what the subject would see if asked to
    # re-consent — not the name it carried when they originally consented.
    assert stale.notice_name == "Fuel Card Marketing v2"
    assert stale.consented_version == 1.0
    assert stale.live_version == 2.0
    assert stale.added_uses == ["marketing.advertising.third_party"]
    assert stale.preference == "opt_in"


def test_the_live_version_is_the_highest_history_version_not_the_newest_row(db):
    # Insert histories out of order (v3 created after v5) and assert the
    # detector still treats v5 as live. `privacynotice` has no version
    # column, so this is the load-bearing query detail.
    _, translation_id = make_notice(db, key="fuel_card", name="Fuel Card Marketing")
    v1 = make_version(
        db, translation_id=translation_id, key="fuel_card", name="Fuel Card Marketing",
        version=1.0, data_uses=["marketing.advertising"],
    )
    # v5 row created BEFORE v3's row, but its version NUMBER is higher.
    make_version(
        db, translation_id=translation_id, key="fuel_card", name="Fuel Card Marketing",
        version=5.0, data_uses=["marketing.advertising", "marketing.advertising.geolocation"],
    )
    make_version(
        db, translation_id=translation_id, key="fuel_card", name="Fuel Card Marketing",
        version=3.0, data_uses=["marketing.advertising", "marketing.advertising.third_party"],
    )
    make_preference(db, history_id=v1, preference="opt_in", email="alice@example.com")

    result = find_stale_consents(db)

    assert len(result) == 1
    stale = result[0]
    assert stale.live_version == 5.0
    # If the detector wrongly picked the newest ROW (v3) instead of the
    # highest VERSION (v5), this would read
    # ["marketing.advertising.third_party"] instead.
    assert stale.added_uses == ["marketing.advertising.geolocation"]


def test_a_subject_with_no_email_is_reported_by_device_id(db):
    _, translation_id = make_notice(db, key="fuel_card", name="Fuel Card Marketing")
    v1 = make_version(
        db, translation_id=translation_id, key="fuel_card", name="Fuel Card Marketing",
        version=1.0, data_uses=["marketing.advertising"],
    )
    make_version(
        db, translation_id=translation_id, key="fuel_card", name="Fuel Card Marketing",
        version=2.0, data_uses=["marketing.advertising", "marketing.advertising.third_party"],
    )
    make_preference(db, history_id=v1, preference="opt_in", device="device-abc-123")

    result = find_stale_consents(db)

    assert len(result) == 1
    assert result[0].subject == "device-abc-123"
    assert result[0].subject_kind == "fides_user_device"


def test_the_detector_writes_nothing(db):
    _, translation_id = make_notice(db, key="fuel_card", name="Fuel Card Marketing")
    v1 = make_version(
        db, translation_id=translation_id, key="fuel_card", name="Fuel Card Marketing",
        version=1.0, data_uses=["marketing.advertising"],
    )
    make_version(
        db, translation_id=translation_id, key="fuel_card", name="Fuel Card Marketing",
        version=2.0, data_uses=["marketing.advertising", "marketing.advertising.third_party"],
    )
    make_preference(db, history_id=v1, preference="opt_in", email="alice@example.com")

    def counts():
        return {
            table: db.execute(sqlalchemy.text(f"SELECT count(*) FROM {table}")).scalar()
            for table in _CONSENT_TABLES
        }

    before = counts()
    result = find_stale_consents(db)
    after = counts()

    assert len(result) == 1  # the call actually did something
    assert before == after


def test_filtering_by_notice_key_narrows_the_report(db):
    _, t1 = make_notice(db, key="fuel_card", name="Fuel Card Marketing")
    v1a = make_version(
        db, translation_id=t1, key="fuel_card", name="Fuel Card Marketing",
        version=1.0, data_uses=["marketing.advertising"],
    )
    make_version(
        db, translation_id=t1, key="fuel_card", name="Fuel Card Marketing",
        version=2.0, data_uses=["marketing.advertising", "marketing.advertising.third_party"],
    )
    make_preference(db, history_id=v1a, preference="opt_in", email="alice@example.com")

    _, t2 = make_notice(db, key="loyalty", name="Loyalty Programme")
    v1b = make_version(
        db, translation_id=t2, key="loyalty", name="Loyalty Programme",
        version=1.0, data_uses=["marketing.advertising"],
    )
    make_version(
        db, translation_id=t2, key="loyalty", name="Loyalty Programme",
        version=2.0, data_uses=["marketing.advertising", "third_party_sharing"],
    )
    make_preference(db, history_id=v1b, preference="opt_in", email="bob@example.com")

    all_stale = find_stale_consents(db)
    assert {s.notice_key for s in all_stale} == {"fuel_card", "loyalty"}

    fuel_only = find_stale_consents(db, notice_key="fuel_card")
    assert len(fuel_only) == 1
    assert fuel_only[0].notice_key == "fuel_card"
    assert fuel_only[0].subject == "alice@example.com"


def test_a_notice_with_one_version_yields_nothing(db):
    _, translation_id = make_notice(db, key="fuel_card", name="Fuel Card Marketing")
    v1 = make_version(
        db, translation_id=translation_id, key="fuel_card", name="Fuel Card Marketing",
        version=1.0, data_uses=["marketing.advertising"],
    )
    make_preference(db, history_id=v1, preference="opt_in", email="alice@example.com")

    assert find_stale_consents(db) == []
