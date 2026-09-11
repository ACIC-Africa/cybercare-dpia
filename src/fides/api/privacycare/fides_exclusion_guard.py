# C1: Fides' own Alembic chain must never propose dropping our tables.
#
# `alembic revision --autogenerate` on the FIDES chain (src/fides/api/alembic/)
# compares the live database against `fides.api.db.base.Base.metadata` using
# `fides.api.db.database.include_object`, which only excludes table names
# listed in `fides.api.db.database.EXCLUDED_TABLES`. PrivacyCare's tables are
# not in that set and are not part of Base.metadata (by design — see
# models.py), so without this module, the moment anyone adds a Fides-side
# model and runs `fides db generate-migration`, autogenerate would emit
# `op.drop_table('privacycare_business_process')` and friends.
#
# We may never edit `fides.api.db.database` (Ethyca-authored). It happens
# that EXCLUDED_TABLES is a plain, mutable `set` object, and `include_object`
# re-checks membership against it at call time rather than freezing a copy
# at import time. So we can register our own table names into that same set
# object from our own side, on import, without touching Ethyca source:
from fides.api.db.database import EXCLUDED_TABLES
from fides.api.privacycare.models import PRIVACYCARE_METADATA

_PRIVACYCARE_TABLE_NAMES = {
    table.name for table in PRIVACYCARE_METADATA.tables.values()
}
# Our version table is never part of PRIVACYCARE_METADATA (Alembic manages
# it directly), but it is just as real a table to Fides' autogenerate.
_PRIVACYCARE_TABLE_NAMES.add("privacycare_alembic_version")

EXCLUDED_TABLES.update(_PRIVACYCARE_TABLE_NAMES)

# RESIDUAL RISK — read before trusting this in production:
# This protects only a Python process that has imported
# `fides.api.privacycare` (this module runs on package import — see
# `fides/api/privacycare/__init__.py`) *before* Fides' autogenerate runs.
# As of this writing nothing under `src/fides/` outside `privacycare/`
# imports this package, so a bare `fides db generate-migration` invoked
# from a fresh interpreter that never touches PrivacyCare code is NOT
# protected by this module alone — EXCLUDED_TABLES in that process would
# still hold only Fides' original three entries. Test processes (which
# import privacycare directly) and anything that imports privacycare
# first (e.g. the app process once PrivacyCare's API surface is wired in)
# are protected. Closing the gap for a bare `fides` CLI invocation needs
# either an Ethyca-side import (forbidden here) or a wrapper entrypoint
# that imports this module before invoking the Fides CLI. See
# tests/privacycare/test_migrations.py::test_c1_fides_autogenerate_would_not_drop_privacycare_tables
# for the regression check this closes, and its docstring for how this is
# verified.
