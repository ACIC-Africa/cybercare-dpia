# PrivacyCare's router, and its registration into Fides' router list.
#
# Fides has no plugin hook, so registration works by appending to
# `app_setup.ROUTERS`. That list was verified to have exactly one consumer —
# the default argument of `create_fides_app` (app_setup.py:84) — before this
# approach was chosen. An earlier PrivacyCare change mutated a different Fides
# collection, `EXCLUDED_TABLES`, which turned out to be dual-purpose and made
# `fides db reset` drop our tables. Check the consumers before mutating anything
# of Ethyca's.
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

    # api/tasks.py binds POST "", GET "/tasks" and GET "/tasks/{task_id}" —
    # imported BEFORE api/assessments (below) so those routes land in
    # privacycare_router.routes ahead of assessments' GET "/{assessment_id}".
    # FastAPI/Starlette match routes in registration order and stop at the
    # first match: if "/{assessment_id}" registered first, a request for
    # "/tasks" would match IT instead (assessment_id="tasks"), and the
    # progress bar would 404 forever against a route that does exist. This
    # import order is only sufficient because api/tasks.py itself imports
    # api/assessments's _created_by_from_client lazily, inside the request
    # handler rather than at module level — a module-level import there
    # would force api/assessments to bind its own routes first regardless of
    # what order these two lines run in.
    #
    # Aliased on import: this package already has a top-level `tasks`
    # module (Task 5's Celery task, imported below) and re-binding that name
    # here would shadow it.
    from fides.api.privacycare.api import tasks as api_tasks  # noqa: F401
    from fides.api.privacycare.api import assessments  # noqa: F401  (binds routes)

    # Importing the task module registers privacycare.generate_assessments
    # with celery_app. The API process needs it registered to queue a
    # message; the worker process gets it from
    # fides.api.privacycare.worker.
    from fides.api.privacycare import tasks  # noqa: F401

    app_setup.ROUTERS.append(privacycare_router)
    setattr(app_setup, _REGISTERED_FLAG, True)
