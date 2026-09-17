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

# The Kenyan-taxonomy processing-grounds surface (D-KT-5): a FOURTH, separate
# prefix family. Not `plus/privacy-assessments` (no shipped UI calls it — the
# hook in Task 6 is new), and not `privacycare/business-processes` (grounds
# are not business processes) — its own namespace for the same reason
# privacycare_processes_router and privacycare_chat_router each got their
# own: a router is constructed with one fixed prefix.
PRIVACYCARE_GROUNDS_PREFIX = f"{V1_URL_PREFIX}/privacycare"

privacycare_grounds_router = APIRouter(prefix=PRIVACYCARE_GROUNDS_PREFIX, tags=["PrivacyCare"])

# The discovery-monitor configuration surface (plan 10): a FIFTH, separate
# prefix family, and — unlike privacycare_processes_router/
# privacycare_chat_router/privacycare_grounds_router — back in Ethyca's
# `plus` namespace, for the same reason PRIVACYCARE_PREFIX is: the shipped
# admin UI's discovery-monitor screen (clients/admin-ui/src/features/
# data-discovery-and-detection/discovery-detection.slice.ts) calls these
# exact `/plus/discovery-monitor*` paths, and the UI's path is the
# requirement. See api/monitors.py's module docstring for the fuller
# version of this argument.
PRIVACYCARE_MONITORS_PREFIX = f"{V1_URL_PREFIX}/plus/discovery-monitor"

privacycare_monitors_router = APIRouter(
    prefix=PRIVACYCARE_MONITORS_PREFIX, tags=["PrivacyCare Discovery"]
)

# The DSR register's HTTP surface (plan 14, task 4): a SIXTH, separate
# prefix family, and — like privacycare_processes_router/
# privacycare_chat_router/privacycare_grounds_router, and UNLIKE
# PRIVACYCARE_PREFIX/PRIVACYCARE_MONITORS_PREFIX — back in OUR OWN
# namespace, not Ethyca's `plus`. Nothing in the shipped admin UI calls
# these routes (Barbara's 2026-09-15 ruling gives PrivacyCare the register,
# with no Plus screen for it), so taking a `plus` path here would only risk
# colliding with a real Plus endpoint later. See api/dsr.py's module
# docstring for the fuller version of this argument.
PRIVACYCARE_DSR_PREFIX = f"{V1_URL_PREFIX}/privacycare/dsr-requests"

privacycare_dsr_router = APIRouter(prefix=PRIVACYCARE_DSR_PREFIX, tags=["PrivacyCare"])

# The stale-consent detector's HTTP surface (plan 16, task 3): a SEVENTH,
# separate prefix family, and — like privacycare_processes_router/
# privacycare_chat_router/privacycare_grounds_router/privacycare_dsr_router,
# and UNLIKE PRIVACYCARE_PREFIX/PRIVACYCARE_MONITORS_PREFIX — back in OUR
# OWN namespace, not Ethyca's `plus`. Nothing in the shipped admin UI calls
# this route (the detector is new, Kenyan-specific ground with no Plus
# analogue), so taking a `plus` path here would only risk colliding with a
# real Plus endpoint later. See api/consent.py's module docstring for the
# fuller version of this argument.
PRIVACYCARE_CONSENT_PREFIX = f"{V1_URL_PREFIX}/privacycare/consent"

privacycare_consent_router = APIRouter(
    prefix=PRIVACYCARE_CONSENT_PREFIX, tags=["PrivacyCare"]
)

# The DPIA risk register's HTTP surface (plan 17, task 5): an EIGHTH,
# separate prefix family, and — like privacycare_processes_router/
# privacycare_chat_router/privacycare_grounds_router/privacycare_dsr_router/
# privacycare_consent_router, and UNLIKE PRIVACYCARE_PREFIX/
# PRIVACYCARE_MONITORS_PREFIX — back in OUR OWN namespace, not Ethyca's
# `plus`. Nothing in the shipped admin UI calls these routes (the risk
# register is new, Kenyan-specific ground with no Plus analogue), so taking
# a `plus` path here would only risk colliding with a real Plus endpoint
# later. See api/risk.py's module docstring for the fuller version of this
# argument.
PRIVACYCARE_RISK_PREFIX = f"{V1_URL_PREFIX}/privacycare/risk"

privacycare_risk_router = APIRouter(prefix=PRIVACYCARE_RISK_PREFIX, tags=["PrivacyCare"])

# The screening gate's HTTP surface (plan 18, task 4): a NINTH, separate
# prefix family, and — like privacycare_processes_router/
# privacycare_chat_router/privacycare_grounds_router/privacycare_dsr_router/
# privacycare_consent_router/privacycare_risk_router, and UNLIKE
# PRIVACYCARE_PREFIX/PRIVACYCARE_MONITORS_PREFIX — back in OUR OWN
# namespace, not Ethyca's `plus`. Nothing in the shipped admin UI calls
# these routes (the screening gate is new, Kenyan-specific ground with no
# Plus analogue), so taking a `plus` path here would only risk colliding
# with a real Plus endpoint later. See api/screening.py's module docstring
# for the fuller version of this argument.
PRIVACYCARE_SCREENING_PREFIX = f"{V1_URL_PREFIX}/privacycare/screening"

privacycare_screening_router = APIRouter(
    prefix=PRIVACYCARE_SCREENING_PREFIX, tags=["PrivacyCare"]
)

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

    # THE ORDER OF THE NEXT THREE IMPORTS IS LOAD-BEARING. Do not sort them.
    #
    # All three route modules decorate the SHARED privacycare_router at
    # import time, so the order they are imported in IS the order their
    # routes are registered in — and Starlette matches in registration
    # order, stopping at the first match. api/tasks.py binds POST "", GET
    # "/tasks" and GET "/tasks/{task_id}"; api/config.py binds GET "/config",
    # PUT "/config" and GET "/config/defaults"; api/assessments.py binds GET
    # "/{assessment_id}" (and its PUT/DELETE siblings on the same path). If
    # assessments bound before either of the other two, a request for
    # "/tasks" or "/config" would match IT instead (assessment_id="tasks" /
    # "config") and that route would 404 forever against a screen that
    # demonstrably exists. test_the_tasks_route_is_matched_before_the_
    # assessment_id_route (test_api_tasks.py) and
    # test_the_config_routes_are_matched_before_the_assessment_id_route
    # (test_api_config.py) both fail if assessments is moved ahead of
    # either. tasks vs config have no ordering constraint between each
    # other — neither path can ever match the other's — so their relative
    # order here is free; assessments must simply come last of the three.
    #
    # importlib, not `from ... import ...`, precisely BECAUSE the order
    # matters: ruff's I001 alphabetises an import block and would silently
    # put assessments before config and config before tasks. An
    # `# isort: off` comment does not suppress it here (verified with
    # `ruff check --diff`). These imports exist only for their route-binding
    # side effect (they carried unused-import suppressions before), so
    # nothing is lost by making the side effect, and its order, explicit.
    #
    # This works only because the three route modules do not import each
    # other: the helper they share, _created_by_from_client, lives in
    # api/identity.py, which none of them depends on. An import of one
    # route module from another would bind ALL of that module's routes at
    # import time and defeat the ordering however these lines are written.
    # That is exactly what happened once already, and why identity.py
    # exists.
    importlib.import_module("fides.api.privacycare.api.tasks")
    importlib.import_module("fides.api.privacycare.api.config")
    importlib.import_module("fides.api.privacycare.api.assessments")
    importlib.import_module("fides.api.privacycare.api.processes")

    # grounds.py decorates its OWN router (privacycare_grounds_router, a
    # distinct prefix), so it carries none of the tasks-vs-assessments
    # matching-order hazard above either.
    importlib.import_module("fides.api.privacycare.api.grounds")

    # api/reports.py (task 3, config-and-pdf plan) binds GET
    # "/{assessment_id}/pdf" on this SAME privacycare_router — but as a
    # TWO-segment path. Starlette matches a route by its whole path
    # template, so a two-segment request path can never match "/tasks",
    # "/config" or "/{assessment_id}" (all one segment) and vice versa:
    # this import carries none of the ordering hazard the comment above
    # documents for tasks/config/assessments, and is placed after them only
    # so a reader sees the whole "/{assessment_id}*" family together.
    importlib.import_module("fides.api.privacycare.api.reports")

    # chat.py decorates its OWN router (privacycare_chat_router, a distinct
    # prefix), so it carries none of the tasks-vs-assessments matching-order
    # hazard above — nothing here is registered against privacycare_router.
    importlib.import_module("fides.api.privacycare.api.chat")

    # monitors.py (plan 10, task 3) decorates its OWN router
    # (privacycare_monitors_router, a distinct prefix), so — same as
    # grounds.py and chat.py above — it carries none of the
    # tasks-vs-assessments matching-order hazard either.
    importlib.import_module("fides.api.privacycare.api.monitors")

    # dsr.py (plan 14, task 4) decorates its OWN router
    # (privacycare_dsr_router, a distinct prefix), so — same as grounds.py,
    # chat.py and monitors.py above — it carries none of the
    # tasks-vs-assessments matching-order hazard either.
    importlib.import_module("fides.api.privacycare.api.dsr")

    # consent.py (plan 16, task 3) decorates its OWN router
    # (privacycare_consent_router, a distinct prefix), so — same as
    # grounds.py, chat.py, monitors.py and dsr.py above — it carries none
    # of the tasks-vs-assessments matching-order hazard either.
    importlib.import_module("fides.api.privacycare.api.consent")

    # risk.py (plan 17, task 5) decorates its OWN router
    # (privacycare_risk_router, a distinct prefix), so — same as grounds.py,
    # chat.py, monitors.py, dsr.py and consent.py above — it carries none of
    # the tasks-vs-assessments matching-order hazard either.
    importlib.import_module("fides.api.privacycare.api.risk")

    # screening.py (plan 18, task 4) decorates its OWN router
    # (privacycare_screening_router, a distinct prefix), so — same as
    # grounds.py, chat.py, monitors.py, dsr.py, consent.py and risk.py
    # above — it carries none of the tasks-vs-assessments matching-order
    # hazard either. screening.py's OWN internal ordering hazard (GET
    # "/triggers" must be registered ahead of GET "/{business_process_id}"
    # on ITS router) is local to that one file and documented there instead.
    importlib.import_module("fides.api.privacycare.api.screening")

    app_setup.ROUTERS.append(privacycare_router)
    app_setup.ROUTERS.append(privacycare_processes_router)
    app_setup.ROUTERS.append(privacycare_chat_router)
    app_setup.ROUTERS.append(privacycare_grounds_router)
    app_setup.ROUTERS.append(privacycare_monitors_router)
    app_setup.ROUTERS.append(privacycare_dsr_router)
    app_setup.ROUTERS.append(privacycare_consent_router)
    app_setup.ROUTERS.append(privacycare_risk_router)
    app_setup.ROUTERS.append(privacycare_screening_router)
    setattr(app_setup, _REGISTERED_FLAG, True)
