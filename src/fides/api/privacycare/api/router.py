# PrivacyCare's router, and its registration into Fides' router list.
#
# Fides has no plugin hook, so registration works by appending to
# `app_setup.ROUTERS`. That list was verified to have exactly one consumer —
# the default argument of `create_fides_app` (app_setup.py:84) — before this
# approach was chosen. An earlier PrivacyCare change mutated a different Fides
# collection, `EXCLUDED_TABLES`, which turned out to be dual-purpose and made
# `fides db reset` drop our tables. Check the consumers before mutating anything
# of Ethyca's.
import importlib

from fides.api.util.api_router import APIRouter

PRIVACYCARE_PREFIX = "/plus/privacy-assessments"

privacycare_router = APIRouter(prefix=PRIVACYCARE_PREFIX, tags=["PrivacyCare"])

_REGISTERED_FLAG = "__privacycare_router_registered__"


def register() -> None:
    """Append privacycare_router to app_setup.ROUTERS.

    MUST be called — and must complete — before `fides.api.main` is
    imported anywhere in the process. `fides.api.main` builds and caches
    `app` at import time via `create_fides_app()`, whose router list is a
    mutable default bound to this same `ROUTERS` object at function-definition
    time: the append only reaches the built app if it happens first. Import
    `fides.api.main` before this runs and the cached app silently has none
    of our routes, with nothing raising to say so. See
    `fides/api/privacycare/asgi.py` for the entrypoint that gets this
    ordering right, and why it is marked `isort: skip_file`.
    """
    # Idempotent: importing twice must not register the router twice.
    from fides.api import app_setup

    if getattr(app_setup, _REGISTERED_FLAG, False):
        return

    # THE ORDER OF THE LAST TWO IMPORTS IS LOAD-BEARING. Do not sort them.
    #
    # api/tasks.py binds POST "", GET "/tasks" and GET "/tasks/{task_id}";
    # api/assessments.py binds GET "/{assessment_id}". Starlette matches
    # routes in registration order and stops at the first match, so if
    # "/{assessment_id}" bound first, a request for "/tasks" would match IT
    # (assessment_id="tasks") and the progress bar would 404 forever against
    # a route that demonstrably exists. api/tasks must therefore import
    # first. test_the_tasks_route_is_matched_before_the_assessment_id_route
    # fails if these two lines are swapped.
    #
    # This works only because the two route modules do not import each
    # other: the helper they share, _created_by_from_client, lives in
    # api/identity.py, which neither depends on. An import of one route
    # module from the other would bind ALL of that module's routes at import
    # time and silently defeat the ordering below, whatever order these
    # lines are in. That is exactly what happened once already, and why
    # identity.py exists.
    #
    # `tasks as api_tasks` is aliased because this package also has a
    # top-level `tasks` module (the Celery task, imported first below);
    # re-binding that name here would shadow it. That import is what
    # registers privacycare.generate_assessments with celery_app — the API
    # process needs it registered to queue a message, while the worker
    # process gets it from fides.api.privacycare.worker.
    # Both route modules decorate the SHARED privacycare_router at import
    # time, so the order they are imported in IS the order their routes are
    # registered in — and Starlette matches in registration order, stopping
    # at the first match. api/assessments.py binds GET "/{assessment_id}";
    # api/tasks.py binds GET "/tasks". If assessments bound first, a request
    # for "/tasks" would match IT (assessment_id="tasks") and the progress
    # bar would 404 forever against a route that demonstrably exists.
    #
    # importlib, not `from ... import ...`, precisely BECAUSE the order
    # matters: ruff's I001 alphabetises an import block and would silently
    # put assessments first. An `# isort: off` comment does not suppress it
    # here (verified with `ruff check --diff`). These two imports exist only
    # for their route-binding side effect (they carried unused-import
    # suppressions before), so nothing is lost by making the side effect,
    # and its order, explicit.
    # test_the_tasks_route_is_matched_before_the_assessment_id_route
    # fails if these two lines are swapped.
    #
    # This works only because the two route modules do not import each
    # other: the helper they share, _created_by_from_client, lives in
    # api/identity.py, which neither depends on. An import of one route
    # module from the other would bind ALL of that module's routes at import
    # time and defeat the ordering however these lines are written. That is
    # exactly what happened once already, and why identity.py exists.
    # Registers privacycare.generate_assessments with celery_app: the API
    # process needs it registered to queue a message, while the worker
    # process gets it from fides.api.privacycare.worker.
    importlib.import_module("fides.api.privacycare.tasks")

    importlib.import_module("fides.api.privacycare.api.tasks")
    importlib.import_module("fides.api.privacycare.api.assessments")

    app_setup.ROUTERS.append(privacycare_router)
    setattr(app_setup, _REGISTERED_FLAG, True)
