# The admin-UI ships assessment screens that call plus/privacy-assessments/*.
# These tests prove our routes are registered, correctly pathed, and — in a
# privacy product, non-negotiably — authenticated with the right scope.
from fides.api.oauth.utils import verify_oauth_client
from fides.api.privacycare.api.router import PRIVACYCARE_PREFIX
from fides.common.scope_registry import SYSTEM_READ

# Suffixes, composed against the real prefix below. This file's job is "these
# routes exist and are authenticated"; proving the PREFIX itself is the one the
# shipped UI calls is test_ui_paths.py's job, deliberately kept separate — this
# inventory composing against our own constant is exactly why a wrong prefix
# went unnoticed for five plans.
EXPECTED_READ_SUFFIXES = {
    "",
    "/summary",
    "/templates",
    "/{assessment_id}",
    "/{assessment_id}/evidence",
    "/tasks",
    "/tasks/{task_id}",
}

PRIVACYCARE_PATH_PREFIX = PRIVACYCARE_PREFIX

EXPECTED_READ_PATHS = {
    f"{PRIVACYCARE_PATH_PREFIX}{suffix}" for suffix in EXPECTED_READ_SUFFIXES
}


def _app():
    import fides.api.privacycare.asgi as asgi

    return asgi.app


def _routes():
    return {getattr(r, "path", ""): r for r in _app().routes}


def _privacycare_routes():
    # Raw app.routes, NOT the path-collapsed dict `_routes()` builds — a
    # dict keyed by path silently drops every route but the last one
    # registered for a given path. Two routes can share a path while
    # differing only in HTTP method (e.g. a future PUT alongside this GET
    # on the same `plus/privacy-assessments/{assessment_id}`), and a
    # path-keyed lookup would check only one of them. Iterating app.routes
    # directly means a later method added to an existing path still gets
    # its own dependency check here.
    return [
        r
        for r in _app().routes
        if getattr(r, "path", "").startswith(PRIVACYCARE_PATH_PREFIX)
    ]


def test_read_routes_are_registered():
    missing = EXPECTED_READ_PATHS - set(_routes())
    assert not missing, f"routes not registered: {sorted(missing)}"


def test_every_assessment_route_requires_verify_oauth_client_with_system_read():
    # Fix round: the original version of this test only asserted
    # `route.dependencies` was non-empty — *any* dependency, including a
    # no-op one, would have passed. This resolves each route's security
    # dependency and checks it is actually `verify_oauth_client`, and that
    # `SYSTEM_READ` is among the scopes it was given — the two things that
    # make a route "authenticated" rather than merely "has a dependencies
    # list".
    routes = _privacycare_routes()
    assert routes, "no privacy-assessments routes found — registration itself is broken"

    for route in routes:
        path = route.path
        deps = getattr(route, "dependencies", [])
        assert deps, f"{path} has no security dependency — unauthenticated"

        oauth_deps = [d for d in deps if getattr(d, "dependency", None) is verify_oauth_client]
        assert oauth_deps, (
            f"{path} has dependencies {deps!r}, but none resolve to "
            "verify_oauth_client — it is not actually authenticated"
        )
        assert any(SYSTEM_READ in getattr(d, "scopes", []) for d in oauth_deps), (
            f"{path}'s verify_oauth_client dependency does not require "
            f"SYSTEM_READ ({SYSTEM_READ!r}) among its scopes"
        )


def test_every_route_has_a_response_model_declared_or_inferred():
    # Rename, fix round 2: this test's old name ("declares a response
    # model") overstated what it checks. FastAPI sets `route.response_model`
    # from an explicit `response_model=` kwarg OR, when that kwarg is
    # omitted, infers it from the endpoint function's return type annotation
    # (APIRoute.__init__ falls back to the return annotation). Either way
    # produces a non-None `route.response_model`, and this assertion cannot
    # tell the two apart — it only proves ONE of them happened. Every route
    # in this module currently declares response_model explicitly, so
    # behaviour is unchanged; only the name and this comment now say what is
    # actually being tested. In a plan about honest contracts, a test whose
    # name overstates what it checks is the wrong note.
    #
    # The defect-class fix this guards: a route with no response_model at
    # all (neither declared nor inferrable) has no schema, no validation,
    # and no contract test — which is exactly how GET /{assessment_id}/
    # questions drifted into existing with an invented shape that nothing
    # called. Every plus/privacy-assessments route, across every HTTP method
    # sharing a path, must end up with SOME response_model.
    offenders = [
        r.path
        for r in _app().routes
        if getattr(r, "path", "").startswith("/plus/privacy-assessments")
        and getattr(r, "response_model", None) is None
    ]
    assert not offenders, (
        f"routes without a response_model cannot be contract-tested: {offenders}"
    )


def test_fides_own_routes_still_present():
    # Guard: appending to ROUTERS must not displace anything.
    #
    # NOTE: this counts raw route objects (`app.routes`), not `_routes()`.
    # `_routes()` builds a dict keyed by path for the other tests' O(1)
    # lookup, which silently collapses routes that share a path across
    # different HTTP methods (e.g. GET+POST+PUT on the same
    # `/api/v1/data_category`). Fides OSS alone has 547 raw route objects
    # but only 374 unique paths, so a `len(_routes()) > 500` guard can never
    # pass regardless of implementation correctness. Counting raw routes is
    # what "must not displace anything" actually means.
    assert len(_app().routes) > 500
