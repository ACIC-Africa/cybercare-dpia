"""The stale-consent detector's HTTP surface (spec D-CON-1, Task 3).

Our own /api/v1/privacycare/consent namespace, not Ethyca's plus/: nothing
in the shipped admin UI calls this route, so taking a path in Plus's
namespace would only risk colliding with a real Plus endpoint later. Same
reasoning api/dsr.py records for the DSR register.

Fixture and notice/version/preference builders are reused from
test_consent_detector.py rather than duplicated — that module already
proves the detection logic itself; this file only proves the HTTP
envelope, the filter, the scope, and the unconfigured-rule behaviour on
top of it.
"""
from datetime import datetime, timezone

import sqlalchemy
from fastapi import HTTPException
from fastapi_pagination import Params
from sqlalchemy.orm import Session

import pytest

from fides.api.oauth.roles import (
    CONTRIBUTOR,
    OWNER,
    ROLES_TO_SCOPES_MAPPING,
    VIEWER,
)
from fides.api.privacycare.api.consent import list_stale_consents
from fides.api.privacycare.consent.materiality import seed_consent_rule
from fides.common.scope_registry import PRIVACYCARE_CONSENT_READ
from tests.privacycare.test_consent_detector import (
    make_notice,
    make_preference,
    make_version,
)

DB_URL = "postgresql://postgres:fides@127.0.0.1:5442/fides"

# Every table this route's query can touch, plus our own rule table — used
# by test_the_route_writes_nothing to prove the read-only claim across a
# real call through the HTTP layer, not just through the detector directly
# (which test_consent_detector.py's own test_the_detector_writes_nothing
# already covers).
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
        # Same reasoning as test_consent_detector.py's own fixture: the live
        # DB may hold zero rows (Task 4's seed CLI has not run) or a real
        # committed row. Clearing and reseeding inside the rolled-back
        # session makes every test independent of which.
        session.execute(sqlalchemy.text("DELETE FROM privacycare_consent_rule"))
        seed_consent_rule(session)
        yield session
        session.rollback()


def _row_counts(db) -> dict:
    return {
        table: db.execute(
            sqlalchemy.text(f"SELECT count(*) FROM {table}")
        ).scalar()
        for table in _CONSENT_TABLES
    }


def _make_stale_pair(db, *, key="fuel_card", name="Fuel Card Marketing", email="alice@example.com"):
    """One notice that gained a use between v1 and v2, plus a v1 opt_in —
    exactly the shape test_consent_detector.py proves is stale.

    test_consent_detector.py's own make_preference leaves received_at NULL
    (that module never asserts on it), but the response schema types it as
    a required datetime, matching consent/detector.py's StaleConsent
    dataclass exactly — so this sets it explicitly here rather than
    relaxing the schema to paper over an untested fixture gap.
    """
    _, translation_id = make_notice(db, key=key, name=name)
    v1 = make_version(
        db, translation_id=translation_id, key=key, name=name,
        version=1.0, data_uses=["marketing.advertising"],
    )
    make_version(
        db, translation_id=translation_id, key=key, name=name,
        version=2.0, data_uses=["marketing.advertising", "marketing.advertising.third_party"],
    )
    pref_id = make_preference(db, history_id=v1, preference="opt_in", email=email)
    db.execute(
        sqlalchemy.text(
            "UPDATE privacypreferencehistory SET received_at = :when WHERE id = :id"
        ),
        {"when": datetime.now(timezone.utc), "id": pref_id},
    )
    return pref_id


def test_the_route_returns_the_page_envelope_and_the_stale_entries(db):
    _make_stale_pair(db)

    page = list_stale_consents(
        notice_key=None, params=Params(page=1, size=50), db=db,
    )

    assert {"items", "total", "page", "size", "pages"} <= set(page.model_dump())
    assert len(page.items) == 1
    entry = page.items[0]
    assert entry.subject == "alice@example.com"
    assert entry.subject_kind == "email"
    assert entry.notice_key == "fuel_card"
    assert entry.notice_name == "Fuel Card Marketing"
    assert entry.consented_version == 1.0
    assert entry.live_version == 2.0
    assert entry.added_uses == ["marketing.advertising.third_party"]
    assert entry.preference == "opt_in"
    assert entry.received_at is not None


def test_notice_key_filters_the_results(db):
    _make_stale_pair(db, key="fuel_card", name="Fuel Card Marketing", email="alice@example.com")
    _make_stale_pair(db, key="loyalty", name="Loyalty Programme", email="bob@example.com")

    page = list_stale_consents(
        notice_key="fuel_card", params=Params(page=1, size=50), db=db,
    )

    assert len(page.items) == 1
    assert page.items[0].notice_key == "fuel_card"


def test_an_empty_result_is_a_200_with_an_empty_list_not_a_404(db):
    # No stale consents exist at all — an empty register, or a fresh
    # database with a rule configured but nothing to report against.
    page = list_stale_consents(
        notice_key=None, params=Params(page=1, size=50), db=db,
    )

    assert page.items == []
    assert page.total == 0


def test_the_route_writes_nothing(db):
    _make_stale_pair(db)
    before = _row_counts(db)

    list_stale_consents(notice_key=None, params=Params(page=1, size=50), db=db)

    after = _row_counts(db)
    assert after == before


def test_viewer_lacks_the_scope_while_owner_and_contributor_have_read(db):
    # PENDING A PRODUCT RULING (roles.py, same as PRIVACYCARE_DSR_READ):
    # this report names data subjects and what they consented to, so
    # Viewer gets neither read nor any other scope for it — Owner and
    # Contributor pick PRIVACYCARE_CONSENT_READ up by registry derivation,
    # not by an explicit entry in any list this test could otherwise miss.
    assert PRIVACYCARE_CONSENT_READ not in ROLES_TO_SCOPES_MAPPING[VIEWER]
    assert PRIVACYCARE_CONSENT_READ in ROLES_TO_SCOPES_MAPPING[OWNER]
    assert PRIVACYCARE_CONSENT_READ in ROLES_TO_SCOPES_MAPPING[CONTRIBUTOR]


def test_the_route_requires_the_read_scope():
    from fides.api.oauth.utils import verify_oauth_client
    from fides.api.privacycare.api.router import PRIVACYCARE_CONSENT_PREFIX
    from fides.api.privacycare.asgi import app

    routes = [
        r for r in app.routes if getattr(r, "path", "").startswith(PRIVACYCARE_CONSENT_PREFIX)
    ]
    assert routes, "no consent routes found — registration itself is broken"

    checked = 0
    for route in routes:
        methods = getattr(route, "methods", None) or set()
        for method in methods - {"HEAD", "OPTIONS"}:
            assert method == "GET", f"unexpected write verb {method} on {route.path}"
            deps = getattr(route, "dependencies", [])
            oauth_deps = [
                d for d in deps if getattr(d, "dependency", None) is verify_oauth_client
            ]
            assert oauth_deps, (
                f"{route.path} [{method}] has no verify_oauth_client "
                "dependency — unauthenticated"
            )
            assert any(
                PRIVACYCARE_CONSENT_READ in getattr(d, "scopes", []) for d in oauth_deps
            ), f"{route.path} [{method}] does not require {PRIVACYCARE_CONSENT_READ!r}"
            checked += 1

    # One logical route (list stale consents), one HTTP method — but
    # fides.api.util.api_router.APIRouter registers BOTH a trailing-slash
    # and a no-trailing-slash variant of every path as separate route
    # objects (same guard as test_every_dsr_route_requires_its_declared_
    # scope in test_api_dsr.py), so app.routes holds two entries: 1 * 2 = 2.
    assert checked == 2, f"expected 2 consent route/method pairs, checked {checked}"


def test_the_route_is_503_not_500_or_a_silent_empty_list_when_unconfigured(db):
    # active_rule (materiality.py) raises ValueError when
    # privacycare_consent_rule holds zero rows — the live deployment's
    # actual state until Task 4's seed CLI runs. A 500 stack trace would
    # hide an operator-fixable configuration gap behind an opaque error;
    # a quiet 200 with an empty list would misreport "nothing is stale"
    # when the truth is "the detector never ran". This route must answer
    # neither of those — 503, naming the cause, is the same shape
    # api/reports.py's get_assessment_pdf uses for an operator-fixable
    # PDFRenderError.
    _make_stale_pair(db)
    db.execute(sqlalchemy.text("DELETE FROM privacycare_consent_rule"))

    with pytest.raises(HTTPException) as caught:
        list_stale_consents(notice_key=None, params=Params(page=1, size=50), db=db)

    assert caught.value.status_code == 503
    assert "privacycare_consent_rule" in caught.value.detail
