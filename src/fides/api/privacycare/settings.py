# What the officer's settings screen actually governs.
#
# api/config.py is the screen's HTTP surface — read and write the
# privacy_assessment_config singleton. This module is the other half: the
# READER the rest of PrivacyCare consults, so that setting a model on that
# screen changes what the platform calls.
#
# Until this module existed, nothing outside api/config.py read
# privacy_assessment_config at all. Generation took its model from
# privacy_assessment_task.llm_model (the create-task request body) and chat
# passed no model whatsoever, so `assessment_model_override` and
# `chat_model_override` were persisted, echoed back as
# `effective_assessment_model`/`effective_chat_model`, and consulted by no
# code path in `src/`. An officer who set one saw the screen confirm it and
# nothing changed. For a single-tenant compliance product where "which model
# processed our personal data" is a question the customer may have to answer
# in writing, a settings screen that reports a model the platform will not
# call is worse than no settings screen.
#
# PRECEDENCE, both here and in the `effective_*` fields that report it:
#
#   1. the per-request value, if one was given — a task's `llm_model` was
#      chosen explicitly for that run, by the officer who started it, and
#      must not be silently overridden by a standing setting;
#   2. the configured override, for every run that did not choose;
#   3. llm.DEFAULT_MODEL — IMPORTED, never retyped (D-CFG-2).
#
# This module is a READER. It never inserts the bootstrap row (that is
# api/config.py's job, under the advisory lock described there) and never
# commits: a generation run must not be the thing that creates a
# configuration row, and an empty table simply means "no override", which is
# rung 3. It also never falls back to a model of its own invention — there
# is exactly one default and llm.py owns it.
from __future__ import annotations

import sqlalchemy
from sqlalchemy.orm import Session

from fides.api.privacycare.llm import DEFAULT_MODEL

# The ORDER BY that decides WHICH row is "the" singleton, shared with
# api/config.py's own reads (which import this constant rather than
# restating it). If these two ever disagreed, the screen would display one
# row's overrides while generation obeyed another's — the exact
# screen-says-one-thing-code-does-another failure this module was written
# to end.
CONFIG_SINGLETON_ORDER_BY = "ORDER BY created_at ASC NULLS LAST, id ASC"

# Only the two columns that have a reader. See _UNCONSUMED_CONFIG_FIELDS in
# api/config.py for the ones that do not, and why they are deliberately left
# alone rather than half-wired here.
_SELECT_MODEL_OVERRIDES_SQL = sqlalchemy.text(
    "SELECT assessment_model_override, chat_model_override "
    f"FROM privacy_assessment_config {CONFIG_SINGLETON_ORDER_BY} LIMIT 1"
)


def _override(db: Session, column: str) -> str | None:
    """The configured override in `column`, or None if there isn't one.

    None covers all three of "no config row yet" (a deployment whose
    settings screen has never been opened and whose seeding migration has
    not run), "the column is null", and "the column holds an empty string"
    — each of which means the officer has expressed no preference, which is
    the same thing as far as resolution is concerned. Same `or`-shaped
    emptiness rule as _shape_config's `effective_*`, so the screen's
    reported value and the model actually called cannot diverge on a blank.
    """
    row = db.execute(_SELECT_MODEL_OVERRIDES_SQL).mappings().first()
    if row is None:
        return None
    return row[column] or None


def resolve_assessment_model(db: Session, requested: str | None = None) -> str:
    """The model assessment generation must call: the task's own
    `llm_model` if that run chose one, else the configured override, else
    llm.DEFAULT_MODEL. See this module's docstring for why that order.
    """
    return requested or _override(db, "assessment_model_override") or DEFAULT_MODEL


def resolve_chat_model(db: Session, requested: str | None = None) -> str:
    """The model the questionnaire chat must call — same three rungs.

    `requested` exists for symmetry and for a caller that has an explicit
    per-request model; no chat route has one today (the chat API carries no
    model field), so in practice chat resolves at rung 2 or 3.
    """
    return requested or _override(db, "chat_model_override") or DEFAULT_MODEL
