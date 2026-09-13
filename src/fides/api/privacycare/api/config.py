# The assessment configuration singleton behind the admin UI's settings
# screen: GET/PUT the one privacy_assessment_config row, plus a read-only
# GET of the platform defaults it falls back to.
#
# `privacy_assessment_config` is an Ethyca-authored table (fides.api.models.
# privacy_assessment_config.PrivacyAssessmentConfig) — this module writes
# rows through raw SQL, same no-ORM-coupling convention as answers.py and
# assessments.py, and adds NO Alembic migration and NO unique constraint. A
# migration already seeds one row on a fresh install (xx_2026_02_23_1400_
# 074796d61d8a_add_privacy_assessment_config.py's `INSERT ... ON CONFLICT DO
# NOTHING`), but that migration's INSERT is not this module's safety net: a
# deployment that has run `fides db reset`, or a test that deletes every row
# to exercise the from-empty path (D-CFG-1), must still work.
#
# THE RACE THIS MODULE EXISTS TO CLOSE (the pre-dispatch ruling): a
# read-then-write on a singleton is the exact shape that has bitten this
# branch three times already (answer-handle creation, version numbering, the
# chat cursor — see api/answers.py's fix-round-1 comment for the first two).
# Two concurrent first reads against an empty table would each see zero rows
# and each insert one; every later `SELECT ... LIMIT 1` would then return
# whichever row it happened to find, silently alternating between two
# different configurations depending on which replica/connection served the
# request. The table is Ethyca's, so no unique constraint may be added to
# make Postgres itself reject the second insert.
#
# _lock_config_row below closes it WITHOUT a schema change, adapting
# _lock_assessment_and_get_template_id's discipline (api/answers.py) to a
# singleton with no natural parent row: lock, re-read, insert only if still
# absent. See that function's own docstring for the two-tier reasoning (a
# cheap row-level FOR UPDATE in the common case; a table-level lock only
# when the table is empty, which is the one case a row lock cannot help
# with — FOR UPDATE against zero matching rows locks nothing at all).
#
# `questionnaire_tone_prompt` (added by a later migration, ca2c622bad39) is
# NEVER read, written, or exposed by this module. It has no TypeScript
# counterpart — see PrivacyAssessmentConfigUpdate's and
# PrivacyAssessmentConfigResponse's own docstrings in schemas.py for the
# same note on the other side. It is deliberately absent from every SQL
# column list below so a later reader cannot "helpfully" wire it back in by
# copying a SELECT that already happens to include it.
import uuid

import sqlalchemy
from fastapi import Depends, Security
from loguru import logger
from sqlalchemy.orm import Session

from fides.api.deps import get_db
from fides.api.models.client import ClientDetail
from fides.api.oauth.utils import verify_oauth_client
from fides.api.privacycare.api.identity import _created_by_from_client
from fides.api.privacycare.api.router import privacycare_router
from fides.api.privacycare.api.schemas import (
    PrivacyAssessmentConfigDefaults,
    PrivacyAssessmentConfigResponse,
    PrivacyAssessmentConfigUpdate,
)
from fides.api.privacycare.llm import DEFAULT_MODEL
from fides.common.scope_registry import SYSTEM_READ

# Every column PrivacyAssessmentConfigResponse can ever need, and NOT ONE
# more — see the module docstring on questionnaire_tone_prompt.
_CONFIG_COLUMNS = (
    "id, assessment_model_override, chat_model_override, "
    "reassessment_enabled, reassessment_cron, slack_channel_id, "
    "slack_channel_name, created_at, updated_at"
)

# Deterministic even in the (should-be-impossible, pre-this-module) case of
# more than one row already existing: oldest row wins, `id` as a final
# tiebreaker. Not merely defensive — this is also the ORDER BY the
# empty-table race's second reader relies on to agree with the first reader
# about which row is "the" singleton once both can see it.
_SELECT_CONFIG_FOR_UPDATE_SQL = sqlalchemy.text(
    f"SELECT {_CONFIG_COLUMNS} FROM privacy_assessment_config "
    "ORDER BY created_at ASC NULLS LAST, id ASC LIMIT 1 FOR UPDATE"
)

# An ADVISORY lock, not LOCK TABLE. Both serialise the bootstrap correctly,
# and a two-connection probe confirmed the table lock genuinely blocks — but
# SHARE ROW EXCLUSIVE blocks every writer of privacy_assessment_config for the
# duration, including Ethyca code this module cannot see. We are a guest in
# their schema: taking a table-wide lock on their table to solve a problem
# entirely of our own making is more coupling than the job needs.
#
# pg_advisory_xact_lock never touches the table's lock manager at all. It
# serialises only callers that ask for this same key, which is exactly and
# only this function's bootstrap path, and it releases with the transaction so
# no caller can leak one. The key is an arbitrary constant chosen to be
# recognisable in pg_locks when someone is debugging a stall.
_CONFIG_BOOTSTRAP_LOCK_KEY = 8_675_309
_LOCK_CONFIG_TABLE_SQL = sqlalchemy.text(
    "SELECT pg_advisory_xact_lock(:key)"
)

# Column-less INSERT, same shape as the seeding migration's own `INSERT INTO
# privacy_assessment_config (id) VALUES (:id)`: every other column's server_
# default supplies the platform defaults (reassessment_enabled='f',
# reassessment_cron='0 9 * * *', created_at/updated_at=now()) without this
# module having to restate them. RETURNING avoids a second round trip back
# to read what was just inserted.
_INSERT_CONFIG_ROW_SQL = sqlalchemy.text(
    f"INSERT INTO privacy_assessment_config (id) VALUES (:id) "
    f"RETURNING {_CONFIG_COLUMNS}"
)


def _lock_config_row(db: Session) -> dict:
    """Return the singleton privacy_assessment_config row as a dict,
    creating it with platform defaults if the table is empty. First thing
    any config read or write in this module does — see the module
    docstring for why a read-then-write on this table is the race the
    pre-dispatch ruling named by name.

    Two-tier locking, cheapest case first:

    1. A plain `SELECT ... FOR UPDATE LIMIT 1`. If a row exists, this locks
       THAT row — the same discipline every other singleton-row read in
       this package already uses (e.g. _lock_assessment_and_get_template_id
       in api/answers.py) — and nothing more is needed. This is the
       overwhelmingly common case: a deployed instance always has exactly
       one row (the migration seeds it), so almost every call returns here
       without ever taking the heavier lock below.

    2. `SELECT ... FOR UPDATE` against an EMPTY table locks NOTHING —
       there are no matching rows for FOR UPDATE to lock — which is
       exactly the shape of the named race: two concurrent callers each
       run step 1, each see zero rows, and with nothing locked yet, both
       would proceed to insert. So when (and only when) step 1 comes back
       empty, this escalates to an advisory lock,
       which DOES serialize against a second caller doing the same thing —
       the second transaction to reach this statement blocks until the
       first commits or rolls back. Once unblocked, it re-reads (this same
       step 1 query, called again) and finds the row the first caller
       already created, so it returns that row instead of inserting a
       second one. Only if the re-read is STILL empty (the truly-first
       caller, table genuinely never seeded) does this insert.

    The lock survives until the caller's transaction ends — this function
    never commits, same "caller owns the transaction" convention as
    write_answer/recompute_completeness in api/answers.py.
    """
    row = db.execute(_SELECT_CONFIG_FOR_UPDATE_SQL).mappings().first()
    if row is not None:
        return dict(row)

    db.execute(_LOCK_CONFIG_TABLE_SQL, {"key": _CONFIG_BOOTSTRAP_LOCK_KEY})
    row = db.execute(_SELECT_CONFIG_FOR_UPDATE_SQL).mappings().first()
    if row is not None:
        return dict(row)

    # generate_record_id("pri")'s own prefix convention (fides.api.alembic.
    # migrations.helpers.database_functions) — matching it keeps every row
    # this table has ever held, migration-seeded or created here, in the
    # same id family.
    new_id = f"pri_{uuid.uuid4()}"
    row = db.execute(_INSERT_CONFIG_ROW_SQL, {"id": new_id}).mappings().first()
    return dict(row)


def _as_str(value):
    # Same one-line helper as assessments.py's own _as_str, duplicated
    # rather than imported: importing anything from assessments.py here
    # would bind ALL of its routes at import time and defeat router.py's
    # load-bearing import ORDER (see that file's register() docstring, and
    # api/identity.py's own docstring for the same hazard already once hit
    # here).
    return value.isoformat() if hasattr(value, "isoformat") else value


def _shape_config(row: dict) -> dict:
    """Turn a raw privacy_assessment_config row into the dict
    PrivacyAssessmentConfigResponse expects: every stored column this
    surface exposes, PLUS the two computed `effective_*` fields.

    `effective_assessment_model`/`effective_chat_model` are `override or
    DEFAULT_MODEL` — DEFAULT_MODEL is IMPORTED from
    fides.api.privacycare.llm, never retyped, so this can never disagree
    with the constant generation and chat actually call (D-CFG-2). An
    empty-string override is not a case this needs to guard against: the
    column is nullable, not a `CHECK (override <> '')`, and nothing in this
    module or PrivacyAssessmentConfigUpdate ever writes `""` — `or` here is
    exactly the None-check `override is not None` would be, just shorter.
    """
    return {
        "id": row["id"],
        "assessment_model_override": row["assessment_model_override"],
        "chat_model_override": row["chat_model_override"],
        "effective_assessment_model": row["assessment_model_override"] or DEFAULT_MODEL,
        "effective_chat_model": row["chat_model_override"] or DEFAULT_MODEL,
        "reassessment_enabled": row["reassessment_enabled"],
        "reassessment_cron": row["reassessment_cron"],
        "slack_channel_id": row["slack_channel_id"],
        "slack_channel_name": row["slack_channel_name"],
        "created_at": _as_str(row["created_at"]),
        "updated_at": _as_str(row["updated_at"]),
    }


def _get_or_create_config(db: Session) -> dict:
    """The read half of this surface: the singleton row, creating it with
    platform defaults if none exists yet (D-CFG-1 — a fresh deployment's
    settings screen must be reachable, not 404). Never commits; the route
    (get_assessment_config, below) does, because this function may have
    just written the bootstrap row.
    """
    return _shape_config(_lock_config_row(db))


# Every field PrivacyAssessmentConfigUpdate can ever carry — see that
# model's own docstring in schemas.py — and the only column names this SET
# clause may ever interpolate. Same guard, same reasoning, as
# _UPDATABLE_ASSESSMENT_FIELDS / _update_assessment in api/assessments.py:
# values below are bound parameters, but a SQL column identifier cannot be,
# so this allow-list is the only thing standing between a caller-supplied
# key and the raw SET clause. It raises rather than asserts (`python -O`
# strips asserts) precisely because it is meant to be unreachable —
# PrivacyAssessmentConfigUpdate.model_fields already closes the set of keys
# `model_dump()` can produce.
_UPDATABLE_CONFIG_FIELDS = {
    "assessment_model_override",
    "chat_model_override",
    "reassessment_enabled",
    "reassessment_cron",
    "slack_channel_id",
    "slack_channel_name",
}


def _update_config(db: Session, request: PrivacyAssessmentConfigUpdate) -> dict:
    """The write half: a PARTIAL update of the singleton row.

    `request.model_dump(exclude_unset=True)` is what makes "the client
    never sent this key" (leave the column alone) distinguishable from
    "the client sent this key with value null" (clear it back to the
    platform default — D-CFG-3); PrivacyAssessmentConfigUpdate's own
    validator already rejects an explicit null for the two NOT NULL
    columns before this function ever sees it, so every null that reaches
    here is a legitimate "clear the override" on a nullable column.

    _lock_config_row is called FIRST (guaranteeing the row exists, under
    the same lock discipline as a plain read) and again AFTER the UPDATE
    (to read back updated_at and the values as Postgres now has them,
    rather than trusting Python's copy of what was just bound) — both
    calls take the lock's cheap fast path once the row exists, so this is
    one extra round trip, not one extra table lock.

    An empty `updates` (a PUT with every field omitted) is a no-op write,
    same precedent as _update_assessment's own empty-body branch: the row
    is still locked and its current, unchanged state is still returned,
    rather than skipped as a no-op read.

    Never commits — update_assessment_config (the route) does, once this
    returns successfully.
    """
    updates = request.model_dump(exclude_unset=True)

    unknown_fields = set(updates) - _UPDATABLE_CONFIG_FIELDS
    if unknown_fields:
        raise ValueError(
            f"_update_config received field(s) outside "
            f"_UPDATABLE_CONFIG_FIELDS: {sorted(unknown_fields)}"
        )

    row = _lock_config_row(db)
    if updates:
        set_clause = ", ".join(f"{field} = :{field}" for field in updates)
        db.execute(
            sqlalchemy.text(
                f"UPDATE privacy_assessment_config SET {set_clause}, "
                "updated_at = now() WHERE id = :id"
            ),
            {**updates, "id": row["id"]},
        )
        row = _lock_config_row(db)
    return _shape_config(row)


# What the platform would use for a brand-new deployment / with every
# override cleared — i.e. exactly what `effective_*` falls back to.
# Mirrors the ORM model's own `server_default="0 9 * * *"` for
# reassessment_cron (fides.api.models.privacy_assessment_config,
# verified against the live migration) as a literal, not a fetched value —
# same convention api/answers.py already uses for other known-fixed DB
# defaults (_ANSWER_STATUSES et al.): this is a constant this codebase
# controls, not data.
_DEFAULT_REASSESSMENT_CRON = "0 9 * * *"


def _defaults() -> dict:
    """Read-only report of the platform defaults. `default_assessment_model`
    and `default_chat_model` are BOTH DEFAULT_MODEL: assessment generation
    and questionnaire chat share one platform default today (llm.py has
    exactly one DEFAULT_MODEL constant) — this reports that fact rather
    than inventing a second, independent default that does not exist
    anywhere in the running system.
    """
    return {
        "default_assessment_model": DEFAULT_MODEL,
        "default_chat_model": DEFAULT_MODEL,
        "default_reassessment_cron": _DEFAULT_REASSESSMENT_CRON,
    }


@privacycare_router.get(
    "/config",
    # Same blanket SYSTEM_READ dependency as every other route on this
    # surface — see update_answer's own comment in api/assessments.py for
    # why (no privacy-assessment-specific scope exists yet).
    dependencies=[Security(verify_oauth_client, scopes=[SYSTEM_READ])],
    response_model=PrivacyAssessmentConfigResponse,
)
def get_assessment_config(
    *, db: Session = Depends(get_db)
) -> PrivacyAssessmentConfigResponse:
    """GET the assessment configuration singleton. Never 404s (D-CFG-1):
    on a fresh deployment with zero rows, _get_or_create_config creates the
    row with platform defaults and this returns it, same as if it had
    always existed.

    Commits — unlike every other GET on this surface — because
    _get_or_create_config may have just inserted the bootstrap row. A read
    that rolled that insert back would "succeed" once and then repeat the
    exact same bootstrap (and, worse, the exact same race this module
    exists to close) on every subsequent request forever.
    """
    config = _get_or_create_config(db)
    db.commit()
    return PrivacyAssessmentConfigResponse(**config)


@privacycare_router.put(
    "/config",
    dependencies=[Security(verify_oauth_client, scopes=[SYSTEM_READ])],
    response_model=PrivacyAssessmentConfigResponse,
)
def update_assessment_config(
    request: PrivacyAssessmentConfigUpdate,
    *,
    db: Session = Depends(get_db),
    # Resolved (not just required by the dependencies= entry above) so the
    # change is attributable — same reasoning as update_answer's own
    # `client` parameter in api/assessments.py, with one difference:
    # privacy_assessment_config carries no created_by/updated_by column
    # (verified against the live schema and against PrivacyAssessmentConfig,
    # the ORM model), so there is nowhere to WRITE authorship. It is
    # logged instead of persisted — the closest analogue this table
    # supports to the audit trail every write elsewhere on this surface
    # gets a real column for.
    client: ClientDetail = Security(verify_oauth_client, scopes=[SYSTEM_READ]),
) -> PrivacyAssessmentConfigResponse:
    """PUT (partial update) the assessment configuration singleton. See
    _update_config's own docstring for the exclude_unset partial-update
    mechanism, and PrivacyAssessmentConfigUpdate's in schemas.py for why an
    explicit null is meaningful here (D-CFG-3) rather than just noise.
    """
    actor = _created_by_from_client(client)
    config = _update_config(db, request)
    db.commit()
    logger.info(
        "PrivacyCare assessment config updated by {}: fields={}",
        actor,
        sorted(request.model_dump(exclude_unset=True)),
    )
    return PrivacyAssessmentConfigResponse(**config)


@privacycare_router.get(
    "/config/defaults",
    dependencies=[Security(verify_oauth_client, scopes=[SYSTEM_READ])],
    response_model=PrivacyAssessmentConfigDefaults,
)
def get_assessment_config_defaults() -> PrivacyAssessmentConfigDefaults:
    """GET the platform defaults `effective_*` falls back to. Reads no
    database row at all — DEFAULT_MODEL and _DEFAULT_REASSESSMENT_CRON are
    both process-wide constants, not per-tenant state — so, unlike the two
    routes above, this needs no `db` parameter.
    """
    return PrivacyAssessmentConfigDefaults(**_defaults())
