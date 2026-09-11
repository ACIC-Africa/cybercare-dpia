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
# process. `tests/privacycare/test_api_registration.py` sorts alphabetically
# before the other test files in this package for exactly this reason.
from fides.api.main import app  # noqa: E402  (import order is the point)

__all__ = ["app"]
