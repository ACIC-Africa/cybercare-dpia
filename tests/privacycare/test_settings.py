# The reader that makes the settings screen govern something.
#
# Every rung of the precedence in privacycare/settings.py is tested here
# against real rows (per-request value -> configured override ->
# llm.DEFAULT_MODEL), and the two call sites that consume it are tested
# where they live: test_tasks.py for generation, test_api_chat.py for chat.
#
# Every write below is rolled back by this file's own `db` fixture, same
# convention as every other file in this package.
import uuid

import pytest
import sqlalchemy
from sqlalchemy.orm import Session

from fides.api.privacycare.llm import DEFAULT_MODEL
from fides.api.privacycare.settings import (
    CONFIG_SINGLETON_ORDER_BY,
    resolve_assessment_model,
    resolve_chat_model,
)

DB_URL = "postgresql://postgres:fides@127.0.0.1:5442/fides"


@pytest.fixture
def db():
    engine = sqlalchemy.create_engine(DB_URL)
    with Session(engine) as session:
        yield session
        session.rollback()


def _set_model_overrides(
    db, *, assessment: str | None = None, chat: str | None = None
) -> str:
    """Make the config singleton carry exactly these overrides.

    Replaces the table's contents rather than updating whatever row a
    developer database happens to hold, so a test asserts against a state
    it fully specified. Rolled back with the caller's transaction; nothing
    here reaches the shared database.
    """
    db.execute(sqlalchemy.text("DELETE FROM privacy_assessment_config"))
    config_id = f"pri_{uuid.uuid4()}"
    db.execute(
        sqlalchemy.text(
            "INSERT INTO privacy_assessment_config "
            "(id, assessment_model_override, chat_model_override) "
            "VALUES (:id, :assessment, :chat)"
        ),
        {"id": config_id, "assessment": assessment, "chat": chat},
    )
    return config_id


def _empty_config_table(db) -> None:
    db.execute(sqlalchemy.text("DELETE FROM privacy_assessment_config"))


# ── Rung 1: the per-request value wins ───────────────────────────────────


def test_an_explicit_per_request_model_wins_over_the_configured_override(db):
    # A task's llm_model was chosen for THAT run by the officer who started
    # it. A standing setting must not silently redirect a run somebody
    # deliberately pointed somewhere else.
    _set_model_overrides(db, assessment="claude-opus-5", chat="claude-opus-5")

    assert resolve_assessment_model(db, "claude-haiku-5") == "claude-haiku-5"
    assert resolve_chat_model(db, "claude-haiku-5") == "claude-haiku-5"


# ── Rung 2: the configured override ──────────────────────────────────────


def test_the_configured_override_is_used_when_the_request_chose_nothing(db):
    # The finding this module exists to close: before it, an officer could
    # set this and nothing anywhere would read it.
    _set_model_overrides(db, assessment="claude-opus-5", chat="claude-haiku-5")

    assert resolve_assessment_model(db, None) == "claude-opus-5"
    assert resolve_chat_model(db, None) == "claude-haiku-5"


def test_the_two_overrides_do_not_leak_into_each_other(db):
    # Generation and chat are separately configurable on the screen; a
    # reader that read one column for both would make that a lie.
    _set_model_overrides(db, assessment="claude-opus-5", chat=None)

    assert resolve_assessment_model(db) == "claude-opus-5"
    assert resolve_chat_model(db) == DEFAULT_MODEL


# ── Rung 3: the platform default ─────────────────────────────────────────


def test_no_override_falls_back_to_the_platform_default(db):
    _set_model_overrides(db, assessment=None, chat=None)

    assert resolve_assessment_model(db, None) == DEFAULT_MODEL
    assert resolve_chat_model(db, None) == DEFAULT_MODEL


def test_an_empty_config_table_is_the_platform_default_not_a_crash(db):
    # A deployment whose settings screen has never been opened. Resolution
    # must not 500, and must not bootstrap a row either — creating
    # configuration as a side effect of a generation run is api/config.py's
    # job, under its advisory lock, not this reader's.
    _empty_config_table(db)

    assert resolve_assessment_model(db) == DEFAULT_MODEL
    assert resolve_chat_model(db) == DEFAULT_MODEL
    count = db.execute(
        sqlalchemy.text("SELECT COUNT(*) FROM privacy_assessment_config")
    ).scalar()
    assert count == 0, "the reader wrote a config row; it must only read"


def test_a_blank_override_is_no_override(db):
    # The column is nullable with no CHECK against '', and the screen's own
    # effective_* fields treat '' as absent (`override or DEFAULT_MODEL`).
    # Resolution must agree, or the screen would report the default while a
    # run called the empty string as a model id.
    _set_model_overrides(db, assessment="", chat="")

    assert resolve_assessment_model(db) == DEFAULT_MODEL
    assert resolve_chat_model(db) == DEFAULT_MODEL


# ── The screen and the reader must pick the same row ─────────────────────


def test_the_reader_and_the_settings_screen_read_the_same_singleton(db, monkeypatch):
    """Two rows should not exist — api/config.py's advisory lock is there to
    keep it that way — but if they ever do, the screen and the running code
    must at least agree on which one is "the" config. They share one ORDER
    BY constant; this proves the sharing is real by putting an older row
    underneath a newer one.
    """
    from fides.api.privacycare.api.config import _get_or_create_config

    monkeypatch.setattr(db, "commit", lambda: None)
    _empty_config_table(db)
    db.execute(
        sqlalchemy.text(
            "INSERT INTO privacy_assessment_config "
            "(id, assessment_model_override, created_at) "
            "VALUES (:id, 'older-row-model', now() - interval '1 day')"
        ),
        {"id": f"pri_{uuid.uuid4()}"},
    )
    db.execute(
        sqlalchemy.text(
            "INSERT INTO privacy_assessment_config "
            "(id, assessment_model_override, created_at) "
            "VALUES (:id, 'newer-row-model', now())"
        ),
        {"id": f"pri_{uuid.uuid4()}"},
    )

    assert "created_at" in CONFIG_SINGLETON_ORDER_BY
    screen = _get_or_create_config(db)
    assert screen["effective_assessment_model"] == resolve_assessment_model(db)
    assert resolve_assessment_model(db) == "older-row-model"
