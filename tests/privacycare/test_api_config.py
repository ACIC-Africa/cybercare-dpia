# The assessment configuration singleton. Endpoint behaviour against real
# rows; every insert/update below is rolled back by this file's own `db`
# fixture (commit is monkeypatched to a no-op in every test that writes, so
# nothing here ever persists past this file's transaction).
import pytest
import sqlalchemy
from sqlalchemy.orm import Session

from fides.api.privacycare.api.config import (
    _defaults,
    _get_or_create_config,
    _update_config,
    get_assessment_config,
    get_assessment_config_defaults,
    update_assessment_config,
)
from fides.api.privacycare.api.router import PRIVACYCARE_PREFIX, privacycare_router
from fides.api.privacycare.api.schemas import PrivacyAssessmentConfigUpdate
from tests.privacycare.test_api_assessments import _fake_client

DB_URL = "postgresql://postgres:fides@127.0.0.1:5442/fides"


@pytest.fixture
def db():
    engine = sqlalchemy.create_engine(DB_URL)
    with Session(engine) as session:
        yield session
        session.rollback()


def test_reading_the_config_when_none_exists_creates_and_returns_it(db, monkeypatch):
    # D-CFG-1: the settings screen must be reachable on a fresh deployment.
    # A 404 here means nobody can ever configure anything.
    monkeypatch.setattr(db, "commit", lambda: None)
    db.execute(sqlalchemy.text("DELETE FROM privacy_assessment_config"))

    config = get_assessment_config(db=db)

    assert config.id
    assert config.reassessment_cron
    assert config.assessment_model_override is None


def test_a_second_read_of_an_empty_table_returns_the_same_row_not_a_second_one(
    db, monkeypatch
):
    # The ruling this task exists to satisfy, stated as a test: two
    # "concurrent" first reads (simulated here as two sequential calls in
    # the same transaction, since _lock_config_row's insert branch can only
    # ever run once per empty table) must not each insert a row. The second
    # call must find, and return, exactly the row the first call created —
    # never a second one alternating underneath later readers.
    monkeypatch.setattr(db, "commit", lambda: None)
    db.execute(sqlalchemy.text("DELETE FROM privacy_assessment_config"))

    first = get_assessment_config(db=db)
    second = get_assessment_config(db=db)

    assert first.id == second.id
    count = db.execute(
        sqlalchemy.text("SELECT COUNT(*) FROM privacy_assessment_config")
    ).scalar()
    assert count == 1


def test_the_effective_model_falls_back_to_the_platform_default(db, monkeypatch):
    # D-CFG-2: imported from llm.DEFAULT_MODEL, not retyped, so the settings
    # screen cannot disagree with what generation actually calls.
    from fides.api.privacycare.llm import DEFAULT_MODEL

    monkeypatch.setattr(db, "commit", lambda: None)
    config = get_assessment_config(db=db)

    assert config.assessment_model_override is None
    assert config.effective_assessment_model == DEFAULT_MODEL


def test_an_override_wins_over_the_default(db, monkeypatch):
    monkeypatch.setattr(db, "commit", lambda: None)
    update_assessment_config(
        PrivacyAssessmentConfigUpdate(assessment_model_override="claude-opus-5"),
        db=db,
        client=_fake_client("carol@example.com"),
    )

    config = get_assessment_config(db=db)

    assert config.effective_assessment_model == "claude-opus-5"


def test_a_partial_update_leaves_absent_fields_alone(db, monkeypatch):
    monkeypatch.setattr(db, "commit", lambda: None)
    update_assessment_config(
        PrivacyAssessmentConfigUpdate(
            reassessment_enabled=True, slack_channel_name="#privacy"
        ),
        db=db,
        client=_fake_client("carol@example.com"),
    )

    update_assessment_config(
        PrivacyAssessmentConfigUpdate(reassessment_enabled=False),
        db=db,
        client=_fake_client("carol@example.com"),
    )

    config = get_assessment_config(db=db)
    assert config.reassessment_enabled is False
    assert config.slack_channel_name == "#privacy", (
        "an absent field was overwritten; PUT is a partial update"
    )


def test_an_explicit_null_clears_an_override(db, monkeypatch):
    # D-CFG-3: distinguishable from absent, and it MEANS something — go back
    # to the platform default.
    monkeypatch.setattr(db, "commit", lambda: None)
    update_assessment_config(
        PrivacyAssessmentConfigUpdate(chat_model_override="claude-opus-5"),
        db=db,
        client=_fake_client("carol@example.com"),
    )

    update_assessment_config(
        PrivacyAssessmentConfigUpdate(chat_model_override=None),
        db=db,
        client=_fake_client("carol@example.com"),
    )

    from fides.api.privacycare.llm import DEFAULT_MODEL

    config = get_assessment_config(db=db)
    assert config.chat_model_override is None
    assert config.effective_chat_model == DEFAULT_MODEL


def test_explicit_null_is_rejected_for_the_not_null_columns():
    # reassessment_enabled/reassessment_cron are NOT NULL at the DB level —
    # an explicit null must 422 at the schema boundary, never reach SQL.
    with pytest.raises(ValueError):
        PrivacyAssessmentConfigUpdate(reassessment_cron=None)
    with pytest.raises(ValueError):
        PrivacyAssessmentConfigUpdate(reassessment_enabled=None)


def test_the_defaults_route_reports_what_the_platform_would_use(db):
    from fides.api.privacycare.llm import DEFAULT_MODEL

    defaults = get_assessment_config_defaults()

    assert defaults.default_assessment_model == DEFAULT_MODEL
    assert defaults.default_chat_model == DEFAULT_MODEL
    assert defaults.default_reassessment_cron


def test_the_config_response_never_exposes_the_tone_prompt(db, monkeypatch):
    # questionnaire_tone_prompt is a column with no TypeScript counterpart.
    # It is not ours to expose; this stops a later reader "helpfully" adding
    # it.
    monkeypatch.setattr(db, "commit", lambda: None)
    config = get_assessment_config(db=db)

    assert not hasattr(config, "questionnaire_tone_prompt")


def test_get_or_create_config_core_is_idempotent(db, monkeypatch):
    # The `_`-prefixed core, called directly (no HTTP/route wrapper, no
    # commit) — a second call within the same transaction must return the
    # same row the first call either found or created.
    monkeypatch.setattr(db, "commit", lambda: None)
    db.execute(sqlalchemy.text("DELETE FROM privacy_assessment_config"))

    first = _get_or_create_config(db)
    second = _get_or_create_config(db)

    assert first["id"] == second["id"]


def test_update_config_core_rejects_a_field_outside_the_allow_list(db):
    # The SET clause interpolates column NAMES — values bind, identifiers
    # cannot — so this allow-list is the only thing between a caller-supplied
    # key and the SQL. It must RAISE, not assert: `python -O` strips asserts,
    # and the guard would then vanish under an optimisation flag.
    #
    # This used to assert the CONTENTS of _UPDATABLE_CONFIG_FIELDS, which
    # proved the set had the values someone typed and nothing about whether
    # the guard exists. Deleting the raise left it green. It now calls the
    # function with a key outside the set.
    # Stands in for the future change this guards against: a field added to
    # the request contract without being added to the allow-list. Nothing can
    # construct that state through PrivacyAssessmentConfigUpdate today, which
    # is why the guard needs a stand-in rather than a real payload.
    class _ContractWithAnUnlistedField:
        def model_dump(self, **_kwargs):
            return {"completeness": 1.0}

    with pytest.raises(ValueError, match="_UPDATABLE_CONFIG_FIELDS"):
        _update_config(db, _ContractWithAnUnlistedField())


def test_the_allow_list_covers_exactly_the_updatable_contract_fields(db):
    # Separate from the guard test above, and for a different reason: a field
    # the UI can send that is NOT in the allow-list would be silently dropped
    # from every update, and the settings screen would appear to save a value
    # it never wrote.
    from fides.api.privacycare.api.config import _UPDATABLE_CONFIG_FIELDS
    from fides.api.privacycare.api.schemas import PrivacyAssessmentConfigUpdate

    assert set(PrivacyAssessmentConfigUpdate.model_fields) == set(
        _UPDATABLE_CONFIG_FIELDS
    ), (
        "the update contract and the SET allow-list disagree; a field in the "
        "contract but not the allow-list is silently discarded on every save"
    )


def test_the_defaults_are_the_constants_the_platform_actually_uses():
    # This used to compare _defaults() against the route that wraps it — the
    # function against itself, which holds however wrong both are. The
    # authority is llm.DEFAULT_MODEL, the constant generation and chat call.
    from fides.api.privacycare.llm import DEFAULT_MODEL

    defaults = _defaults()

    assert defaults["default_assessment_model"] == DEFAULT_MODEL
    assert defaults["default_chat_model"] == DEFAULT_MODEL
    assert defaults["default_reassessment_cron"], "a cron default must exist"


def test_the_config_routes_are_matched_before_the_assessment_id_route():
    # /config and /config/defaults would otherwise be swallowed by
    # /{assessment_id} (assessment_id="config"), and the settings screen
    # would 404 forever against a route that demonstrably exists. Same
    # precedent as test_the_tasks_route_is_matched_before_the_assessment_id_
    # route in test_api_tasks.py.
    paths = [route.path for route in privacycare_router.routes]
    assessment_id_index = paths.index(f"{PRIVACYCARE_PREFIX}/{{assessment_id}}")
    assert paths.index(f"{PRIVACYCARE_PREFIX}/config") < assessment_id_index
    assert paths.index(f"{PRIVACYCARE_PREFIX}/config/defaults") < assessment_id_index
