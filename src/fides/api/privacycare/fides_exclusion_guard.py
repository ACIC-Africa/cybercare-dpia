# C1: Fides' own Alembic chain must never propose dropping our tables.
#
# `alembic revision --autogenerate` on the FIDES chain (src/fides/api/alembic/)
# compares the live database against `fides.api.db.base.Base.metadata` using
# `fides.api.db.database.include_object` (database.py:36-44), which only
# excludes table names listed in `fides.api.db.database.EXCLUDED_TABLES`
# (database.py:29-33). PrivacyCare's tables are not in that set and are not
# part of Base.metadata (by design — see models.py), so without some form of
# protection, the moment anyone adds a Fides-side model and runs
# `fides db generate-migration`, autogenerate would emit
# `op.drop_table('privacycare_business_process')` and friends.
#
# WHY WE DO NOT TOUCH EXCLUDED_TABLES (this is the important part):
# EXCLUDED_TABLES is dual-purpose inside database.py, and only one of its
# two uses is one we want to affect:
#
#   - database.py:44 — `include_object()` checks `name in EXCLUDED_TABLES`
#     to decide what autogenerate should skip. This is the behaviour we
#     want to extend to our own tables.
#   - database.py:120-123 — `reset_db()` iterates the SAME set and runs
#     `DROP TABLE IF EXISTS {table_name} CASCADE` for every member, with a
#     `# nosemgrep: sql_injection_fstring` justified specifically on the
#     grounds that the names in EXCLUDED_TABLES are "hardcoded ... not user
#     input" (see that comment in database.py).
#
# An earlier version of this module added our `privacycare_*` names to
# EXCLUDED_TABLES directly. That closed the autogenerate gap, but it also
# meant `fides db reset` (which calls `reset_db`) would CASCADE-drop every
# PrivacyCare table on any developer or CI machine that had imported this
# package — turning an orphaning hazard (tables Fides doesn't know about,
# recoverable) into a silent destruction hazard (tables actively dropped).
# It also invalidated the nosemgrep justification above, since our table
# names would no longer be "hardcoded" from database.py's point of view —
# they'd be injected from elsewhere at import time.
#
# THE FIX: wrap the FILTER FUNCTION, not the set it reads.
#
# We may never edit `fides.api.db.database` (Ethyca-authored) as a *file*,
# but nothing stops us from rebinding the `include_object` name it exports,
# from our own side, on import. Two things make this work reliably:
#
#   - `fides.api.alembic.migrations.env.py:7` does
#     `from fides.api.db.database import include_object`, which resolves
#     the module attribute fresh every time Alembic actually runs.
#   - `database.py`'s own `include_object` calls (there are none internal
#     to reset_db — reset_db reads EXCLUDED_TABLES directly, never calling
#     through include_object) mean wrapping the function has zero effect
#     on reset_db's behaviour. reset_db only ever sees the three names
#     Fides ships with, because we never add to EXCLUDED_TABLES.
#
# So: we replace `fides.api.db.database.include_object` with a wrapper that
# delegates to the original Ethyca function first (preserving all of its
# behaviour, including the three EXCLUDED_TABLES entries it already
# protects), and additionally returns `False` for any table whose name
# starts with `privacycare_`. `_install()` is idempotent — it checks for a
# marker attribute before wrapping, so importing this module twice (or a
# reload) cannot double-wrap the function or make the "original" it holds
# a reference to another layer of our own wrapper.
#
# RESIDUAL RISK — read before trusting this in production:
# This protects only a Python process that has imported
# `fides.api.privacycare` (this module runs on package import — see
# `fides/api/privacycare/__init__.py`) *before* Fides' autogenerate runs.
# As of this writing nothing under `src/fides/` outside `privacycare/`
# imports this package, so a bare `fides db generate-migration` invoked
# from a fresh interpreter that never touches PrivacyCare code is NOT
# protected by this module alone — `include_object` in that process would
# still be Fides' original, unwrapped function. Test processes (which
# import privacycare directly) and anything that imports privacycare first
# (e.g. the app process once PrivacyCare's API surface is wired in) are
# protected. Closing the gap for a bare `fides` CLI invocation needs either
# an Ethyca-side import (forbidden here) or a wrapper entrypoint that
# imports this module before invoking the Fides CLI. See
# tests/privacycare/test_migrations.py::test_c1_fides_autogenerate_would_not_drop_privacycare_tables
# for the regression check this closes, and its docstring for how this is
# verified.
from fides.api.db import database as _fides_database

_PRIVACYCARE_TABLE_PREFIX = "privacycare_"


def _install() -> None:
    """Wrap `fides.api.db.database.include_object` so it also excludes
    `privacycare_*` tables from Fides' own autogenerate, without touching
    `EXCLUDED_TABLES` (see module docstring for why).

    Idempotent: if `include_object` has already been wrapped by this
    function (marked via `__privacycare_wrapped__`), this is a no-op. That
    keeps `__privacycare_original__` pointing at the true, unwrapped Ethyca
    function even if this module is imported more than once or reloaded —
    a second call may not treat a prior wrapper as "the original" and
    layer a second wrapper on top of it.
    """
    current = _fides_database.include_object
    if getattr(current, "__privacycare_wrapped__", False):
        return

    original = current

    def _include_object(object, name, type_, reflected, compare_to):  # pylint: disable=redefined-builtin
        if not original(object, name, type_, reflected, compare_to):
            return False
        if type_ == "table" and name.startswith(_PRIVACYCARE_TABLE_PREFIX):
            return False
        return True

    _include_object.__privacycare_wrapped__ = True
    _include_object.__privacycare_original__ = original
    _include_object.__wrapped__ = original
    _include_object.__doc__ = original.__doc__
    _include_object.__name__ = original.__name__

    _fides_database.include_object = _include_object


_install()
