"""The DPIA risk register's HTTP surface (spec 2026-09-16 D-W2-2, Task 5).

Fixture and _seed_template/_seed_assessment helpers are copied from
test_risk_register.py rather than imported — that module already proves
the register core (score/band computation, ordering, the projected
risk_level sync); this file proves only the HTTP envelope, that adding a
risk changes the ODPC finding, scope enforcement, and the unknown-
assessment-id 404 decision this task's brief asks for (see api/risk.py's
own module docstring, "THE PARKED TASK-3 FINDING", for the reasoning).
"""
import uuid

import pytest
import sqlalchemy
from fastapi import HTTPException
from fastapi_pagination import Params
from sqlalchemy.orm import Session

from fides.api.oauth.roles import CONTRIBUTOR, OWNER, ROLES_TO_SCOPES_MAPPING, VIEWER
from fides.api.privacycare.api.risk import (
    add_assessment_risk,
    get_odpc_finding,
    list_assessment_risks,
    remove_assessment_risk,
)
from fides.api.privacycare.api.risk_schemas import RiskCreate
from fides.api.privacycare.risk.banding import CRITICAL, LOW, band, score
from fides.common.scope_registry import PRIVACYCARE_RISK_CREATE, PRIVACYCARE_RISK_READ

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


def _add(db, aid, **overrides):
    body = dict(
        category="confidentiality",
        description="Fuel card PINs logged in plaintext to the ops console.",
        likelihood=3,
        severity=3,
    )
    body.update(overrides)
    return add_assessment_risk(aid, RiskCreate(**body), db=db)


# --- List route ---------------------------------------------------------


def test_list_returns_the_page_envelope_and_the_recorded_risk(db, assessment_id):
    _add(db, assessment_id, category="integrity", description="one risk", likelihood=4, severity=3)

    page = list_assessment_risks(assessment_id, params=Params(page=1, size=50), db=db)

    assert {"items", "total", "page", "size", "pages"} <= set(page.model_dump())
    assert len(page.items) == 1
    entry = page.items[0]
    assert entry.assessment_id == assessment_id
    assert entry.category == "integrity"
    assert entry.description == "one risk"
    assert entry.likelihood == 4
    assert entry.severity == 3
    assert entry.score == score(4, 3)
    assert entry.band == band(score(4, 3))


def test_an_assessment_with_no_risks_is_a_200_empty_list_not_a_404(db, assessment_id):
    page = list_assessment_risks(assessment_id, params=Params(page=1, size=50), db=db)

    assert page.items == []
    assert page.total == 0


def test_an_unknown_assessment_id_is_404_on_list_not_a_silent_empty_page(db):
    # THE PARKED TASK-3 FINDING (see api/risk.py's module docstring):
    # list_risks itself has no existence check and would otherwise return
    # an indistinguishable empty page for a typo'd id. This route must tell
    # the two apart.
    with pytest.raises(HTTPException) as caught:
        list_assessment_risks(str(uuid.uuid4()), params=Params(page=1, size=50), db=db)
    assert caught.value.status_code == 404


# --- Add route ------------------------------------------------------------


def test_add_returns_the_created_risk_with_score_and_band_computed(db, assessment_id):
    created = _add(
        db, assessment_id, category="integrity", description="a severe risk",
        likelihood=5, severity=5,
    )

    assert created.assessment_id == assessment_id
    assert created.category == "integrity"
    assert created.description == "a severe risk"
    assert created.score == 25
    assert created.band == CRITICAL

    [row] = list_assessment_risks(assessment_id, params=Params(page=1, size=50), db=db).items
    assert row.id == created.id


def test_add_on_an_unknown_assessment_is_404_not_400(db):
    # register.add_risk already raises ValueError for both "no such
    # assessment" and "not one of the seven categories" — this route must
    # not collapse the two into the same status code: the path segment
    # names a resource (the parent assessment) that does not exist, which
    # is a 404, not a 400.
    with pytest.raises(HTTPException) as caught:
        _add(db, str(uuid.uuid4()))
    assert caught.value.status_code == 404
    assert "no such assessment" in caught.value.detail


def test_add_with_a_category_outside_the_seven_is_400_not_404(db, assessment_id):
    with pytest.raises(HTTPException) as caught:
        _add(db, assessment_id, category="sabotage")
    assert caught.value.status_code == 400
    assert "sabotage" in caught.value.detail


def test_add_with_an_out_of_range_likelihood_is_400(db, assessment_id):
    with pytest.raises(HTTPException) as caught:
        _add(db, assessment_id, likelihood=9)
    assert caught.value.status_code == 400


def test_a_failed_add_writes_nothing(db, assessment_id):
    with pytest.raises(HTTPException):
        _add(db, assessment_id, category="sabotage")

    # Not list_assessment_risks(assessment_id, ...): the route's own
    # db.rollback() on this ValueError ends the CURRENT transaction, which
    # also discards this test's fixture-flushed (never committed)
    # assessment/template rows — the same thing a real, separate request's
    # rollback would do to ITS OWN uncommitted work, harmless there because
    # each real request gets a fresh transaction. Querying the risk table
    # directly, scoped to assessment_id, proves "nothing written" without
    # depending on the now-gone parent row still resolving through
    # _require_assessment.
    remaining = db.execute(
        sqlalchemy.text(
            "SELECT count(*) FROM privacycare_dpia_risk WHERE assessment_id = :aid"
        ),
        {"aid": assessment_id},
    ).scalar()
    assert remaining == 0


# --- ODPC finding route -----------------------------------------------------


def test_adding_a_risk_changes_the_odpc_finding(db, assessment_id):
    before = get_odpc_finding(assessment_id, db=db)
    assert before.required is False
    assert before.band == LOW
    assert before.highest_risk is None

    _add(
        db, assessment_id, category="integrity", description="catastrophic",
        likelihood=5, severity=5,
    )

    after = get_odpc_finding(assessment_id, db=db)
    assert after.required is True
    assert after.band == CRITICAL
    assert after.window_days == 60
    assert after.highest_risk is not None
    assert after.highest_risk.category == "integrity"


def test_the_odpc_finding_on_an_unknown_assessment_is_404_not_a_silent_low(db):
    # Same parked finding as the list route: odpc.evaluate reads
    # assessment_band, which has no existence check either and would
    # otherwise silently report "low, not required" for a typo'd id.
    with pytest.raises(HTTPException) as caught:
        get_odpc_finding(str(uuid.uuid4()), db=db)
    assert caught.value.status_code == 404


# --- Remove route -----------------------------------------------------------


def test_remove_deletes_the_risk_and_the_finding_reflects_it(db, assessment_id):
    created = _add(
        db, assessment_id, category="integrity", description="catastrophic",
        likelihood=5, severity=5,
    )
    assert get_odpc_finding(assessment_id, db=db).required is True

    result = remove_assessment_risk(created.id, db=db)

    assert result.removed is True
    assert result.id == created.id
    assert list_assessment_risks(assessment_id, params=Params(page=1, size=50), db=db).items == []
    assert get_odpc_finding(assessment_id, db=db).required is False


def test_remove_on_an_unknown_risk_id_is_404(db):
    with pytest.raises(HTTPException) as caught:
        remove_assessment_risk(str(uuid.uuid4()), db=db)
    assert caught.value.status_code == 404


# --- Scopes -------------------------------------------------------------


def test_viewer_can_read_but_not_write_while_owner_and_contributor_do_both():
    # Unlike PRIVACYCARE_DSR_READ and PRIVACYCARE_CONSENT_READ, the risk
    # register names no data subject (see roles.py's own comment on
    # PRIVACYCARE_RISK_READ), so Viewer gets the read scope outright.
    assert PRIVACYCARE_RISK_READ in ROLES_TO_SCOPES_MAPPING[VIEWER]
    assert PRIVACYCARE_RISK_CREATE not in ROLES_TO_SCOPES_MAPPING[VIEWER]

    assert PRIVACYCARE_RISK_READ in ROLES_TO_SCOPES_MAPPING[OWNER]
    assert PRIVACYCARE_RISK_CREATE in ROLES_TO_SCOPES_MAPPING[OWNER]

    assert PRIVACYCARE_RISK_READ in ROLES_TO_SCOPES_MAPPING[CONTRIBUTOR]
    assert PRIVACYCARE_RISK_CREATE in ROLES_TO_SCOPES_MAPPING[CONTRIBUTOR]


def test_every_risk_route_requires_its_declared_scope():
    from fides.api.oauth.utils import verify_oauth_client
    from fides.api.privacycare.api.router import PRIVACYCARE_RISK_PREFIX
    from fides.api.privacycare.asgi import app

    # GET reads (list, odpc finding); POST/DELETE are both writes and share
    # the one write scope, PRIVACYCARE_RISK_CREATE — same shape
    # PRIVACYCARE_DSR_UPDATE already uses across create/decision/
    # notification (test_every_dsr_route_requires_its_declared_scope, this
    # package's test_response_model_ts_parity.py).
    expected_scope_by_method = {
        "GET": PRIVACYCARE_RISK_READ,
        "POST": PRIVACYCARE_RISK_CREATE,
        "DELETE": PRIVACYCARE_RISK_CREATE,
    }

    routes = [
        r for r in app.routes if getattr(r, "path", "").startswith(PRIVACYCARE_RISK_PREFIX)
    ]
    assert routes, "no risk routes found — registration itself is broken"

    checked = 0
    for route in routes:
        methods = getattr(route, "methods", None) or set()
        for method in methods - {"HEAD", "OPTIONS"}:
            expected_scope = expected_scope_by_method[method]
            deps = getattr(route, "dependencies", [])
            oauth_deps = [
                d for d in deps if getattr(d, "dependency", None) is verify_oauth_client
            ]
            assert oauth_deps, (
                f"{route.path} [{method}] has no verify_oauth_client "
                "dependency — unauthenticated"
            )
            assert any(
                expected_scope in getattr(d, "scopes", []) for d in oauth_deps
            ), f"{route.path} [{method}] does not require {expected_scope!r}"
            checked += 1

    # 4 logical routes (list, add, remove, odpc finding), one HTTP method
    # apiece — but fides.api.util.api_router.APIRouter registers BOTH a
    # trailing-slash and a no-trailing-slash variant of every path as
    # separate route objects (same guard as test_every_dsr_route_requires_
    # its_declared_scope in test_response_model_ts_parity.py), so app.routes
    # holds two entries per logical route: 4 * 2 = 8.
    assert checked == 8, f"expected 8 risk route/method pairs, checked {checked}"
