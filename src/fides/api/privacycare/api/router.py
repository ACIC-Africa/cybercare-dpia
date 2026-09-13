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
from fides.common.urn_registry import V1_URL_PREFIX

# The path the SHIPPED admin UI calls, not a path of our choosing.
#
# clients/admin-ui/.env.test sets NEXT_PUBLIC_FIDESCTL_API=/api/v1 and
# next.config.js rewrites /api/v1/:path to the API server, so the RTK slice's
# `url: "plus/privacy-assessments"` reaches us as
# /api/v1/plus/privacy-assessments. Every one of Fides' own 196 routes lives
# under that prefix too.
#
# This used to be a bare "/plus/privacy-assessments" — mounted at the root of
# the app, alongside only "/" and "/health". Every route we shipped 404'd for
# the UI, and nothing caught it for five plans: tests/privacycare/
# test_api_http.py built its URLs from THIS CONSTANT, so it proved the routes
# exist where we put them and never that where we put them is where the
# client looks. V1_URL_PREFIX is imported rather than hardcoded so the two
# cannot drift.
PRIVACYCARE_PREFIX = f"{V1_URL_PREFIX}/plus/privacy-assessments"

privacycare_router = APIRouter(prefix=PRIVACYCARE_PREFIX, tags=["PrivacyCare"])

# The business-process ROPA surface gets its own router and its own namespace.
# PRIVACYCARE_PREFIX squats Ethyca's `plus` namespace because the shipped admin
# UI calls those exact paths and the UI's path is the requirement. Nothing in
# the shipped UI calls these, so taking a `plus` path here would only risk
# colliding with a real Plus endpoint later.
PRIVACYCARE_PROCESSES_PREFIX = f"{V1_URL_PREFIX}/privacycare/business-processes"

privacycare_processes_router = APIRouter(
    prefix=PRIVACYCARE_PROCESSES_PREFIX, tags=["PrivacyCare"]
)

# The questionnaire chat surface. A THIRD, separate prefix family — neither
# the shipped UI's own `plus/privacy-assessments` path nor our own
# `privacycare/business-processes` namespace, but a third shipped path the
# admin UI's chat slice calls directly (StartChatRequest/ChatReplyRequest in
# clients/admin-ui/src/features/privacy-assessments/types.ts). It gets its
# own router for the same reason privacycare_processes_router does: a
# different URL prefix cannot live on an APIRouter already constructed with
# a different one.
PRIVACYCARE_CHAT_PREFIX = f"{V1_URL_PREFIX}/plus/chat/questionnaire"

privacycare_chat_router = APIRouter(prefix=PRIVACYCARE_CHAT_PREFIX, tags=["PrivacyCare"])

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

    # Registers privacycare.generate_assessments with celery_app: the API
    # process needs it registered to queue a message, while the worker
    # process gets it from fides.api.privacycare.worker.
    importlib.import_module("fides.api.privacycare.tasks")

    # THE ORDER OF THE NEXT TWO IMPORTS IS LOAD-BEARING. Do not sort them.
    #
    # Both route modules decorate the SHARED privacycare_router at import
    # time, so the order they are imported in IS the order their routes are
    # registered in — and Starlette matches in registration order, stopping
    # at the first match. api/tasks.py binds POST "", GET "/tasks" and GET
    # "/tasks/{task_id}"; api/assessments.py binds GET "/{assessment_id}".
    # If assessments bound first, a request for "/tasks" would match IT
    # (assessment_id="tasks") and the progress bar would 404 forever against
    # a route that demonstrably exists.
    # test_the_tasks_route_is_matched_before_the_assessment_id_route fails
    # if these two lines are swapped.
    #
    # importlib, not `from ... import ...`, precisely BECAUSE the order
    # matters: ruff's I001 alphabetises an import block and would silently
    # put assessments first. An `# isort: off` comment does not suppress it
    # here (verified with `ruff check --diff`). These two imports exist only
    # for their route-binding side effect (they carried unused-import
    # suppressions before), so nothing is lost by making the side effect,
    # and its order, explicit.
    #
    # This works only because the two route modules do not import each
    # other: the helper they share, _created_by_from_client, lives in
    # api/identity.py, which neither depends on. An import of one route
    # module from the other would bind ALL of that module's routes at import
    # time and defeat the ordering however these lines are written. That is
    # exactly what happened once already, and why identity.py exists.
    importlib.import_module("fides.api.privacycare.api.tasks")
    importlib.import_module("fides.api.privacycare.api.assessments")
    importlib.import_module("fides.api.privacycare.api.processes")

    # chat.py decorates its OWN router (privacycare_chat_router, a distinct
    # prefix), so it carries none of the tasks-vs-assessments matching-order
    # hazard above — nothing here is registered against privacycare_router.
    importlib.import_module("fides.api.privacycare.api.chat")

    app_setup.ROUTERS.append(privacycare_router)
    app_setup.ROUTERS.append(privacycare_processes_router)
    app_setup.ROUTERS.append(privacycare_chat_router)
    setattr(app_setup, _REGISTERED_FLAG, True)
