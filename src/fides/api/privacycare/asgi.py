# ASGI entrypoint for PrivacyCare.
#
# isort: skip_file — see the hazard documented below. pyproject.toml enables
# ruff's "I" (isort) rule with `fixable = ["ALL"]`, so `ruff --fix` or an
# editor's "organize imports" action would otherwise be free to hoist the
# `fides.api.main` import above `register()`. This directive tells ruff's
# isort implementation (isort-comment-compatible) to leave this file's
# import order alone.
#
# Start the server against `fides.api.privacycare.asgi:app` instead of
# `fides.api.main:app`. This module imports PrivacyCare first — installing the
# autogenerate guard and registering our routes — and only then imports Fides'
# app, so ordering is deterministic rather than hoped for. Nothing Ethyca owns
# is edited; the compose overlay repoints the command.
from fides.api.privacycare.api.router import register

register()

# `fides.api.main` executes `create_fides_app()` at import time (main.py:130)
# and caches the resulting `app` at module scope. `register()` above MUST run
# — and complete — before this import, because `create_fides_app`'s router
# list is a mutable default bound to `app_setup.ROUTERS` at *function-definition*
# time: appending to that list only reaches the app if the append happens
# before this call executes. Reorder these two statements — by hand, via an
# import-sorter, or via a well-meaning editor's "organize imports" — and the
# app that gets built and cached here will silently have none of our routes.
# Nothing raises: `fides.api.main` imports cleanly either way, the app boots,
# and only a missing-route test or a 404 in the field reveals it.
#
# Runtime is not exposed to this: uvicorn always enters through this module
# (the compose `command` targets `fides.api.privacycare.asgi:app`), so
# `register()` always runs first in the server process. The real exposure is
# any *other* process — a test file, a script, a REPL — that imports
# `fides.api.main` directly before anything imports this module, since that
# permanently caches a routeless `app` in `sys.modules` for the rest of that
# process. `tests/privacycare/conftest.py` imports this module before any
# test in the package is collected, regardless of file name or which single
# test file pytest was asked to run — that is what actually guarantees this
# ordering now (alphabetical-sort-of-the-first-test-file was never a real
# guarantee and stopped being true once more test files were added).
from fides.api.main import app  # noqa: E402  (import order is the point)

# Registering plan 15's daily DSR deadline alert job (dsr/alert_job.py)
# happens down here, AFTER `app` exists, for the same reason `register()`
# had to run BEFORE `app` exists above: this is `app.add_event_handler`,
# which needs the object it is a method on. There is no earlier point in
# this file where `app` is available to call it on — trying to hoist this
# above the `fides.api.main` import would be a plain NameError, not the
# silent, only-discovered-in-the-field failure the routes above are
# protected against. Kept in this file rather than inside
# `initiate_scheduled_dsr_alerts` itself so the *whole* startup sequence —
# routes first, then this — reads in one place, matching the app-import
# ordering this file already exists to protect.
from fides.api.privacycare.dsr.alert_job import (  # noqa: E402
    initiate_scheduled_dsr_alerts,
)

app.add_event_handler("startup", initiate_scheduled_dsr_alerts)

__all__ = ["app"]
