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
# is edited; the compose overlay repoints the command. M7: specifically,
# `/home/shikoli/Cybota/LightHouse/docker-compose.privacycare.yml` — the
# overlay the running container actually uses — sets the `fides` service's
# `command` to `... fides.api.privacycare.asgi:app`. This repo's own
# same-named `docker-compose.privacycare.yml` is NOT that file: it overrides
# only the worker services and leaves `fides` on the base image's default
# command, `fides.api.main:app` — an app with no PrivacyCare routes and, as
# of the fix below, no alert job either, and nothing about booting it raises.
from contextlib import asynccontextmanager

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
# had to run BEFORE `app` exists above: this needs the object it wraps.
# There is no earlier point in this file where `app` is available — trying
# to hoist this above the `fides.api.main` import would be a plain
# NameError, not the silent, only-discovered-in-the-field failure this is
# itself protecting against (see below). Kept in this file rather than
# inside `initiate_scheduled_dsr_alerts` itself so the *whole* startup
# sequence — routes first, then this — reads in one place, matching the
# app-import ordering this file already exists to protect.
#
# CRITICAL, final review of plan 15 (finding C1): this used to be
# `app.add_event_handler("startup", initiate_scheduled_dsr_alerts)`. That
# is dead code in the deployed process, and nothing about it fails loudly.
# Starlette's `Router` only builds a `_DefaultLifespan` — the *sole* caller
# of `Router.startup()`, which is in turn the only thing that ever iterates
# `router.on_startup` — when `lifespan is None`
# (`starlette/routing.py`: `if lifespan is None: self.lifespan_context =
# _DefaultLifespan(self)`; `Router.startup()`'s only caller is
# `_DefaultLifespan.__aenter__`). `create_fides_app()` always builds
# `FastAPI(lifespan=lifespan)` with Fides' own `lifespan` function
# (`app_setup.py:97`, defined in `main.py`), so `router.on_startup` is
# never consulted and `add_event_handler` was appending to a list nobody
# reads. Verified in-container: the handler sits in `router.on_startup`,
# and only `fides_lifespan` (main.py's `lifespan`) ever actually runs.
# Nothing raises, no route 404s — the daily alert job simply never
# registered against the scheduler in the process that was actually
# deployed, and only a test that drives the real ASGI lifespan (rather
# than calling `initiate_scheduled_dsr_alerts()` directly, which only
# proves the test-mode no-op) can catch that. See
# `tests/privacycare/test_asgi_alert_wiring.py`.
#
# The fix wraps Fides' own lifespan context manager instead of trying to
# hook `on_startup` back up — an event handler is not a viable fix here at
# all, custom lifespan and on_startup/on_shutdown are mutually exclusive by
# design once a custom `lifespan` is supplied. Wrapping reaches every
# process that boots through this module without editing anything Ethyca
# owns: `app.router.lifespan_context` is a runtime attribute we capture and
# re-set from here, not a change to the source file that defines it.
# Registering *inside* the `async with` block, after `_fides_lifespan` has
# already reached its `yield`, also means `scheduler.start()` (main.py's
# `lifespan`, which runs before the `yield`) has unconditionally already
# executed by the time `initiate_scheduled_dsr_alerts`'s own `assert
# scheduler.running` runs — true by construction, not by luck of import
# order.
from fides.api.privacycare.dsr.alert_job import (  # noqa: E402
    initiate_scheduled_dsr_alerts,
)

_fides_lifespan = app.router.lifespan_context


@asynccontextmanager
async def _privacycare_lifespan(asgi_app):
    async with _fides_lifespan(asgi_app) as state:
        initiate_scheduled_dsr_alerts()  # scheduler.start() already ran
        yield state


app.router.lifespan_context = _privacycare_lifespan

__all__ = ["app"]
