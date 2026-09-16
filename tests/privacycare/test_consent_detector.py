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
from sqlalchemy import String
from sqlalchemy.orm import Session

from fides.api.db.encryption_utils import encrypted_type
from fides.api.models.privacy_preference import PrivacyPreferenceHistory
from fides.api.privacycare.consent.detector import StaleConsent, find_stale_consents
from fides.api.privacycare.consent.materiality import seed_consent_rule

# privacypreferencehistory.email / .fides_user_device are StringEncryptedType
# (AES-GCM) columns: the detector now resolves subject identity through the
# ORM specifically so it gets the decrypting side of this type (see
# detector.py's module docstring for why raw SQL can't). Fixture rows built
# by a raw INSERT therefore have to write real ciphertext too, using this
# same type's encrypt side, or the ORM's decrypt side chokes on plain text
# that isn't valid AES-GCM ciphertext.
_IDENTITY_TYPE = encrypted_type(type_in=String())


def _encrypt(value):
    return None if value is None else _IDENTITY_TYPE.process_bind_param(value, dialect=None)

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
        # Task 4 committed one permanent demo notice/version/preference
        # (privacycare_demo_fuel_card_marketing) so the detector has
        # something true to find in the running system. Every test below
        # asserts an EXACT count or an empty result from an unfiltered
        # find_stale_consents(db) call, written when these tables were
        # guaranteed empty — that guarantee no longer holds against the
        # live database. Clearing the notice tables here, same idiom as
        # the rule table just above, makes each test see only the rows it
        # builds itself; rollback at teardown leaves Task 4's committed
        # demo row untouched.
        session.execute(sqlalchemy.text("DELETE FROM privacypreferencehistory"))
        session.execute(sqlalchemy.text("DELETE FROM privacynoticehistory"))
        session.execute(sqlalchemy.text("DELETE FROM noticetranslation"))
        session.execute(sqlalchemy.text("DELETE FROM privacynotice"))
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
                    device=None, phone=None, external_id=None,
                    received_at=None, created_at=None) -> str:
    """One privacypreferencehistory row.

    `created_at` is explicit-or-default rather than always defaulted,
    because Postgres' `now()` is TRANSACTION start time: every row a test
    inserts inside the rolled-back `db` fixture would otherwise carry an
    IDENTICAL created_at, which makes the detector's created_at tiebreak
    untestable. Pass a real timestamp when the test cares about order.
    """
    pref_id = str(uuid4())
    db.execute(sqlalchemy.text("""
        INSERT INTO privacypreferencehistory
          (id, preference, privacy_notice_history_id, email, fides_user_device,
           phone_number, external_id, received_at, created_at)
        VALUES (:id, :pref, :hid, :email, :device, :phone, :external_id,
                :received_at, COALESCE(:created_at, now()))
    """), {"id": pref_id, "pref": preference, "hid": history_id,
           "email": _encrypt(email), "device": _encrypt(device),
           "phone": _encrypt(phone), "external_id": _encrypt(external_id),
           "received_at": received_at, "created_at": created_at})
    return pref_id


def _notice_that_gained_a_use(db, *, key="fuel_card", name="Fuel Card Marketing"):
    """A notice with v1 -> v2 gaining `marketing.advertising.third_party`.
    Returns (translation_id, v1_id, v2_id)."""
    _, translation_id = make_notice(db, key=key, name=name)
    v1 = make_version(
        db, translation_id=translation_id, key=key, name=name,
        version=1.0, data_uses=["marketing.advertising"],
    )
    v2 = make_version(
        db, translation_id=translation_id, key=key, name=name,
        version=2.0, data_uses=["marketing.advertising", "marketing.advertising.third_party"],
    )
    return translation_id, v1, v2


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


def test_a_preference_recorded_through_the_orm_decrypts_to_its_plaintext_identity(db):
    # The fix-round test. Every other test's make_preference() writes email
    # via a raw INSERT, so it round-trips as plain text regardless of
    # whether the detector reads it correctly through the ORM's decrypting
    # type or incorrectly through raw SQL -- that's self-consistent and
    # proves nothing about real data. `email` is a StringEncryptedType
    # (AES-GCM) column: writing it through the ORM's own create/persist_obj
    # path is what actually encrypts it at rest, so this is the one
    # fixture in the file where a raw-SQL read of `email` would come back
    # as ciphertext instead of "carol@example.com".
    _, translation_id = make_notice(db, key="fuel_card", name="Fuel Card Marketing")
    v1 = make_version(
        db, translation_id=translation_id, key="fuel_card", name="Fuel Card Marketing",
        version=1.0, data_uses=["marketing.advertising"],
    )
    make_version(
        db, translation_id=translation_id, key="fuel_card", name="Fuel Card Marketing",
        version=2.0, data_uses=["marketing.advertising", "marketing.advertising.third_party"],
    )
    # PrivacyPreferenceHistory.create -> persist_obj does add/commit/refresh
    # unconditionally; the `db` fixture's commit->flush patch is what keeps
    # this rolled back at teardown instead of actually committing.
    PrivacyPreferenceHistory.create(
        db,
        data={
            "preference": "opt_in",
            "privacy_notice_history_id": v1,
            "email": "carol@example.com",
        },
        check_name=False,
    )

    result = find_stale_consents(db)

    assert len(result) == 1
    assert result[0].subject == "carol@example.com"
    assert result[0].subject_kind == "email"


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


# ---------------------------------------------------------------------------
# Final review, Finding 2: phone_number is Fides' fourth consent identity.
# ---------------------------------------------------------------------------


def test_a_subject_identified_only_by_phone_number_is_reported_by_phone(db):
    # If consent is captured in a fuel-card or loyalty base, the identifier
    # is an MSISDN — as it would be for essentially any Kenyan retailer.
    # This used to come back as subject_kind "none" with nothing but a row
    # id: uncontactable, and indistinguishable from the corrupt-data case
    # "none" exists to flag.
    _, v1, _v2 = _notice_that_gained_a_use(db)
    make_preference(db, history_id=v1, preference="opt_in", phone="+254712345678")

    result = find_stale_consents(db)

    assert len(result) == 1
    assert result[0].subject == "+254712345678"
    assert result[0].subject_kind == "phone_number"


def test_a_subject_identified_only_by_external_id_is_still_reported(db):
    _, v1, _v2 = _notice_that_gained_a_use(db)
    make_preference(db, history_id=v1, preference="opt_in", external_id="loyalty-99")

    result = find_stale_consents(db)

    assert len(result) == 1
    assert result[0].subject == "loyalty-99"
    assert result[0].subject_kind == "external_id"


# ---------------------------------------------------------------------------
# Final review, Finding 3: deleting a notice translation must not erase a
# finding. privacynoticehistory.translation_id is ondelete="SET NULL" and
# Ethyca's own comment says the row is retained deliberately "for consent
# reporting"; PrivacyNotice.update deletes every translation not supplied
# in the request.
# ---------------------------------------------------------------------------


def _drop_translation(db, translation_id: str) -> None:
    """What Ethyca's own ondelete="SET NULL" does when
    delete_notice_translations removes a translation the update request did
    not supply: the history rows survive with a NULL translation_id."""
    db.execute(
        sqlalchemy.text(
            "UPDATE privacynoticehistory SET translation_id = NULL "
            "WHERE translation_id = :tid"
        ),
        {"tid": translation_id},
    )
    db.execute(
        sqlalchemy.text("DELETE FROM noticetranslation WHERE id = :tid"),
        {"tid": translation_id},
    )


def test_a_preference_against_a_translation_less_history_row_is_still_reported(db):
    # Josephine's notice has English and Swahili. She gains a purpose and
    # in the same save drops Swahili. Every preference recorded against a
    # Swahili history row used to fall out of all three INNER JOINs and
    # vanish — the API returned 200 with a shorter list.
    notice_id, english = make_notice(db, key="fuel_card", name="Fuel Card Marketing")
    swahili = str(uuid4())
    db.execute(sqlalchemy.text("""
        INSERT INTO noticetranslation (id, language, privacy_notice_id, title)
        VALUES (:tid, 'sw', :nid, :title)
    """), {"tid": swahili, "nid": notice_id, "title": "Fuel Card Marketing"})

    sw_v1 = make_version(
        db, translation_id=swahili, key="fuel_card", name="Fuel Card Marketing",
        version=1.0, data_uses=["marketing.advertising"],
    )
    make_version(
        db, translation_id=english, key="fuel_card", name="Fuel Card Marketing",
        version=2.0, data_uses=["marketing.advertising", "marketing.advertising.third_party"],
    )
    make_preference(db, history_id=sw_v1, preference="opt_in", email="asha@example.com")

    _drop_translation(db, swahili)

    result = find_stale_consents(db)

    assert len(result) == 1
    assert result[0].subject == "asha@example.com"
    assert result[0].notice_key == "fuel_card"
    assert result[0].live_version == 2.0
    assert result[0].added_uses == ["marketing.advertising.third_party"]


def test_a_notice_whose_translations_are_all_gone_still_reports_its_stale_consents(db):
    # The `translations: []` save. Every history row ends up with a NULL
    # translation_id, so the whole notice used to disappear from the report
    # — "nothing is stale" for a notice that had just gained a processing
    # purpose, which is the exact false reassurance this feature exists to
    # prevent.
    _, v1, _v2 = _notice_that_gained_a_use(db)
    translation_id = db.execute(
        sqlalchemy.text("SELECT id FROM noticetranslation")
    ).scalar()
    make_preference(db, history_id=v1, preference="opt_in", email="alice@example.com")

    _drop_translation(db, translation_id)

    result = find_stale_consents(db)

    assert len(result) == 1
    assert result[0].notice_key == "fuel_card"
    assert result[0].subject == "alice@example.com"
    # And the filter Josephine would actually type still reaches it.
    assert len(find_stale_consents(db, notice_key="fuel_card")) == 1


# ---------------------------------------------------------------------------
# Final review, minor: both notice_key and notice_name are the LIVE ones.
# ---------------------------------------------------------------------------


def test_the_reported_notice_key_is_the_live_one_not_the_consented_one(db):
    # notice_key is denormalised onto each history row at the time of that
    # edit, so a key change leaves older rows carrying the old key. The
    # report — and the filter — must name the key Josephine sees today, or
    # filtering by it returns nothing for exactly the people who need
    # re-consent.
    _, translation_id = make_notice(db, key="fuel_card_renamed", name="Fuel Card Marketing")
    v1 = make_version(
        db, translation_id=translation_id, key="fuel_card_old",
        name="Fuel Card Marketing", version=1.0, data_uses=["marketing.advertising"],
    )
    make_version(
        db, translation_id=translation_id, key="fuel_card_renamed",
        name="Fuel Card Marketing", version=2.0,
        data_uses=["marketing.advertising", "marketing.advertising.third_party"],
    )
    make_preference(db, history_id=v1, preference="opt_in", email="alice@example.com")

    result = find_stale_consents(db)
    assert len(result) == 1
    assert result[0].notice_key == "fuel_card_renamed"

    assert len(find_stale_consents(db, notice_key="fuel_card_renamed")) == 1
    assert find_stale_consents(db, notice_key="fuel_card_old") == []
