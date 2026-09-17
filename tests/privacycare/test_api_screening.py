"""The screening gate's HTTP surface (plan 18, Task 4).

Follows api/risk.py (plan 17) and its test file's own shape: routes are
called directly as plain functions against a rolled-back session (no
TestClient), and scope enforcement is proved two ways — the role-mapping
assertion below, and a walk of app.routes (mirroring
test_every_risk_route_requires_its_declared_scope /
test_every_dsr_route_requires_its_declared_scope) confirming every route
actually declares the scope it is supposed to.

UPDATE (Task 5, spec 2026-09-17, plan 18): the authorized `--commit` seed
has now run against this same live database and Carol's six trigger rows
stay there permanently (same as the Kenya template). The `triggers`
fixture below is INSERT ... ON CONFLICT (trigger_key) DO NOTHING, so it is
a no-op against those six real rows rather than raising UniqueViolation;
every test using it below cares only about trigger_key identity (whether a
given key is valid to screen against), never about the fixture's own
throwaway label/description text, with the one exception noted at
test_triggers_come_back_in_display_order itself.

THE DISTINCTION THIS FILE EXISTS TO PROVE: an unknown declaration_id is a
404 on every read AND the write route; a declaration that exists but has
never been screened is a 200 with no verdict — never collapsed into
either the 404 case or a fabricated "screened out".
"""
import uuid

import pytest
import sqlalchemy
from fastapi import HTTPException
from sqlalchemy.orm import Session

from fides.api.oauth.roles import CONTRIBUTOR, OWNER, ROLES_TO_SCOPES_MAPPING, VIEWER
from fides.api.privacycare.api.screening import (
    get_current_screening_verdict,
    get_screening_history,
    list_screening_triggers,
    record_screening_decision,
)
from fides.api.privacycare.api.screening_schemas import ScreeningDecisionRequest
from fides.common.scope_registry import (
    PRIVACYCARE_SCREENING_CREATE,
    PRIVACYCARE_SCREENING_READ,
)
from tests.privacycare.test_api_assessments import _fake_client
from tests.privacycare.test_context import _seed_declaration, _seed_system

DB_URL = "postgresql://postgres:fides@127.0.0.1:5442/fides"

# The six trigger keys, in the order Task 1's seed script assigns them
# display_order — same list test_screening_gate.py uses; this file only
# needs stable keys to screen against, not Carol's exact label text.
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
    a from-empty database (e.g. CI)."""
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


def _record(db, declaration_id, **overrides):
    body = dict(triggered_keys=[], justification="Internal only, no external sharing.")
    body.update(overrides)
    return record_screening_decision(
        declaration_id,
        ScreeningDecisionRequest(**body),
        db=db,
        client=_fake_client("carol@example.com"),
    )


# --- Triggers route ------------------------------------------------------


def test_triggers_come_back_in_display_order(db, triggers):
    # UPDATE (Task 5): this test's own name is "...in display order", and
    # that's the contract this route actually needs to prove — exact
    # label/description text is test_seed_screening_triggers.py's job (per
    # this file's own docstring, "not Carol's exact label text"). Before
    # Task 5's real seed, the `triggers` fixture owned every row in the
    # table outright, so asserting its own synthetic label/description
    # text back was an easy way to prove passthrough; now that Carol's six
    # real rows already exist, the fixture's INSERT is a no-op (ON
    # CONFLICT DO NOTHING) and what comes back is her real text, not the
    # fixture's placeholder — checked here only for non-emptiness, not a
    # hardcoded pattern this file was never meant to own.
    response = list_screening_triggers(db=db)

    assert [t.trigger_key for t in response.triggers] == list(TRIGGER_KEYS)
    for trigger in response.triggers:
        assert trigger.label
        assert trigger.description


def test_triggers_route_is_a_200_empty_envelope_when_nothing_is_seeded(db):
    # UPDATE (Task 5): the live table is no longer empty by default (Carol's
    # six rows are permanent, same as the Kenya template), so proving a
    # genuinely empty table renders as [] rather than an exception now means
    # clearing it inside this test's own rolled-back transaction — the
    # DELETE never escapes past this test's session.rollback() teardown.
    db.execute(sqlalchemy.text("DELETE FROM privacycare_screening_trigger"))
    response = list_screening_triggers(db=db)
    assert response.triggers == []


# --- Current verdict route -------------------------------------------------


def test_an_unscreened_declaration_is_a_200_with_no_verdict_not_a_404(db, declaration_id):
    response = get_current_screening_verdict(declaration_id, db=db)

    assert response.declaration_id == declaration_id
    assert response.verdict is None


def test_an_unknown_declaration_id_is_404_on_current_verdict(db):
    with pytest.raises(HTTPException) as caught:
        get_current_screening_verdict(str(uuid.uuid4()), db=db)
    assert caught.value.status_code == 404


def test_recording_a_decision_changes_the_current_verdict(db, triggers, declaration_id):
    before = get_current_screening_verdict(declaration_id, db=db)
    assert before.verdict is None

    _record(db, declaration_id, triggered_keys=["large_scale"], justification=None)

    after = get_current_screening_verdict(declaration_id, db=db)
    assert after.verdict is not None
    assert after.verdict.dpia_required is True
    assert after.verdict.triggered_keys == ["large_scale"]
    assert after.verdict.declaration_id == declaration_id
    assert after.verdict.decided_by == "carol@example.com"


# --- History route --------------------------------------------------------


def test_history_is_empty_for_a_never_screened_declaration(db, declaration_id):
    response = get_screening_history(declaration_id, db=db)
    assert response.declaration_id == declaration_id
    assert response.decisions == []


def test_an_unknown_declaration_id_is_404_on_history(db):
    with pytest.raises(HTTPException) as caught:
        get_screening_history(str(uuid.uuid4()), db=db)
    assert caught.value.status_code == 404


def _backdate(db, declaration_id: str, triggered_keys: list, *, hours: int) -> None:
    """Pushes one decision's decided_at into the past by hand — same
    reasoning as test_screening_gate.py's own _backdate: this test's
    session never commits, so Postgres' now() (decided_at's server_default)
    is the whole TRANSACTION's start time, and both decisions recorded here
    would otherwise share one identical decided_at, leaving "newest first"
    resolved by the id tie-break rather than actually observable."""
    db.execute(
        sqlalchemy.text(
            "UPDATE privacycare_screening_decision "
            "SET decided_at = decided_at - (:hours || ' hours')::interval "
            "WHERE declaration_id = :decl_id AND triggered_keys = :keys"
        ),
        {"hours": hours, "decl_id": declaration_id, "keys": triggered_keys},
    )


def test_history_returns_both_decisions_after_rescreening(db, triggers, declaration_id):
    _record(db, declaration_id, triggered_keys=[], justification="Nothing ticked this quarter.")
    _backdate(db, declaration_id, [], hours=1)
    _record(db, declaration_id, triggered_keys=["special_category"], justification=None)

    response = get_screening_history(declaration_id, db=db)

    assert response.declaration_id == declaration_id
    assert len(response.decisions) == 2
    assert {d.dpia_required for d in response.decisions} == {True, False}
    # Newest first (gate.decision_history's own ordering).
    assert response.decisions[0].triggered_keys == ["special_category"]
    assert response.decisions[1].triggered_keys == []


# --- Record decision route -------------------------------------------------


def test_record_returns_the_created_verdict(db, triggers, declaration_id):
    verdict = _record(
        db, declaration_id, triggered_keys=["new_technology", "large_scale"],
        justification=None,
    )

    assert verdict.declaration_id == declaration_id
    assert verdict.dpia_required is True
    assert verdict.triggered_keys == ["large_scale", "new_technology"]
    assert verdict.justification is None
    assert verdict.decided_by == "carol@example.com"
    assert verdict.decided_at is not None


def test_a_screen_out_without_a_justification_is_400(db, triggers, declaration_id):
    with pytest.raises(HTTPException) as caught:
        _record(db, declaration_id, triggered_keys=[], justification=None)
    assert caught.value.status_code == 400


def test_a_screen_out_with_a_blank_justification_is_400(db, triggers, declaration_id):
    with pytest.raises(HTTPException) as caught:
        _record(db, declaration_id, triggered_keys=[], justification="   ")
    assert caught.value.status_code == 400


def test_an_unknown_trigger_key_is_400(db, triggers, declaration_id):
    with pytest.raises(HTTPException) as caught:
        _record(db, declaration_id, triggered_keys=["not_a_real_trigger"], justification=None)
    assert caught.value.status_code == 400


def test_recording_against_an_unknown_declaration_is_404(db, triggers):
    with pytest.raises(HTTPException) as caught:
        _record(db, str(uuid.uuid4()), triggered_keys=["large_scale"], justification=None)
    assert caught.value.status_code == 404


def test_a_caller_cannot_pass_dpia_required(db, triggers, declaration_id):
    # ScreeningDecisionRequest carries no dpia_required field at all —
    # record_decision (gate.py) derives it from triggered_keys and does not
    # accept it as an argument. Pydantic's default extra="ignore" means a
    # stray dpia_required in the payload is silently dropped rather than
    # rejected outright, but the important guarantee is behavioural: a
    # caller cannot use it to override the derivation. Ticking a trigger
    # while also passing dpia_required=False must still come back required.
    request = ScreeningDecisionRequest(
        triggered_keys=["large_scale"], justification=None, dpia_required=False,
    )
    assert not hasattr(request, "dpia_required")

    verdict = record_screening_decision(
        declaration_id, request, db=db, client=_fake_client("carol@example.com"),
    )
    assert verdict.dpia_required is True


def test_a_failed_record_writes_nothing(db, triggers, declaration_id):
    with pytest.raises(HTTPException):
        _record(db, declaration_id, triggered_keys=["not_a_real_trigger"], justification=None)

    remaining = db.execute(
        sqlalchemy.text(
            "SELECT count(*) FROM privacycare_screening_decision "
            "WHERE declaration_id = :id"
        ),
        {"id": declaration_id},
    ).scalar()
    assert remaining == 0


# --- Route ordering ------------------------------------------------------


def test_the_triggers_route_is_matched_before_the_declaration_id_route():
    # GET /triggers would otherwise be swallowed by GET /{declaration_id}
    # (declaration_id="triggers"), and the triggers route would 404 forever
    # against a route that demonstrably exists — same hazard, same fix
    # shape, as test_api_tasks.py's test_the_tasks_route_is_matched_before_
    # the_assessment_id_route. A comment at the decorators documents the
    # ordering; this test is the guard that actually catches a regression
    # (screening.py's route functions are called directly everywhere else
    # in this file, which proves each route WORKS but not that FastAPI
    # would ever reach it through the real dispatch path).
    #
    # route.path on this router carries the full PRIVACYCARE_SCREENING_PREFIX
    # prefix (Fides' APIRouter subclass applies it at add_api_route time,
    # same as PRIVACYCARE_PREFIX's own routes), so the paths compared here
    # must match that.
    from fides.api.privacycare.api.router import (
        PRIVACYCARE_SCREENING_PREFIX,
        privacycare_screening_router,
    )

    paths = [route.path for route in privacycare_screening_router.routes]
    assert paths.index(f"{PRIVACYCARE_SCREENING_PREFIX}/triggers") < paths.index(
        f"{PRIVACYCARE_SCREENING_PREFIX}/{{declaration_id}}"
    )


# --- Scopes -------------------------------------------------------------


def test_viewer_can_read_but_not_write_while_owner_and_contributor_do_both():
    # A screening decision names an activity (which triggers were ticked, a
    # free-text justification for a screen-out), never a data subject — same
    # reasoning PRIVACYCARE_RISK_READ's grant to Viewer records (roles.py) —
    # so Viewer gets the read scope outright; the write scope does not.
    assert PRIVACYCARE_SCREENING_READ in ROLES_TO_SCOPES_MAPPING[VIEWER]
    assert PRIVACYCARE_SCREENING_CREATE not in ROLES_TO_SCOPES_MAPPING[VIEWER]

    assert PRIVACYCARE_SCREENING_READ in ROLES_TO_SCOPES_MAPPING[OWNER]
    assert PRIVACYCARE_SCREENING_CREATE in ROLES_TO_SCOPES_MAPPING[OWNER]

    assert PRIVACYCARE_SCREENING_READ in ROLES_TO_SCOPES_MAPPING[CONTRIBUTOR]
    assert PRIVACYCARE_SCREENING_CREATE in ROLES_TO_SCOPES_MAPPING[CONTRIBUTOR]


def test_every_screening_route_requires_its_declared_scope():
    from fides.api.oauth.utils import verify_oauth_client
    from fides.api.privacycare.api.router import PRIVACYCARE_SCREENING_PREFIX
    from fides.api.privacycare.asgi import app

    expected_scope_by_method = {
        "GET": PRIVACYCARE_SCREENING_READ,
        "POST": PRIVACYCARE_SCREENING_CREATE,
    }

    routes = [
        r for r in app.routes
        if getattr(r, "path", "").startswith(PRIVACYCARE_SCREENING_PREFIX)
    ]
    assert routes, "no screening routes found — registration itself is broken"

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

    # 4 logical routes (triggers, current verdict, history, record decision),
    # one HTTP method apiece — but fides.api.util.api_router.APIRouter
    # registers BOTH a trailing-slash and a no-trailing-slash variant of
    # every path as separate route objects (same guard as
    # test_every_risk_route_requires_its_declared_scope /
    # test_every_dsr_route_requires_its_declared_scope), so app.routes holds
    # two entries per logical route: 4 * 2 = 8.
    assert checked == 8, f"expected 8 screening route/method pairs, checked {checked}"
