# The admin-UI ships assessment screens that call plus/privacy-assessments/*.
# These tests prove our routes are registered, correctly pathed, and — in a
# privacy product, non-negotiably — authenticated.
import pytest

EXPECTED_READ_PATHS = {
    "/plus/privacy-assessments",
    "/plus/privacy-assessments/summary",
    "/plus/privacy-assessments/templates",
    "/plus/privacy-assessments/{assessment_id}",
    "/plus/privacy-assessments/{assessment_id}/questions",
    "/plus/privacy-assessments/{assessment_id}/evidence",
}


def _app():
    import fides.api.privacycare.asgi as asgi

    return asgi.app


def _routes():
    return {getattr(r, "path", ""): r for r in _app().routes}


def test_read_routes_are_registered():
    missing = EXPECTED_READ_PATHS - set(_routes())
    assert not missing, f"routes not registered: {sorted(missing)}"


def test_every_assessment_route_is_authenticated():
    for path, route in _routes().items():
        if not path.startswith("/plus/privacy-assessments"):
            continue
        deps = getattr(route, "dependencies", [])
        assert deps, f"{path} has no security dependency — unauthenticated"


def test_fides_own_routes_still_present():
    # Guard: appending to ROUTERS must not displace anything.
    #
    # NOTE: this counts raw route objects (`app.routes`), not `_routes()`.
    # `_routes()` builds a dict keyed by path for the other two tests' O(1)
    # lookup, which silently collapses routes that share a path across
    # different HTTP methods (e.g. GET+POST+PUT on the same
    # `/api/v1/data_category`). Fides OSS alone has 547 raw route objects
    # but only 374 unique paths, so a `len(_routes()) > 500` guard can never
    # pass regardless of implementation correctness. Counting raw routes is
    # what "must not displace anything" actually means.
    assert len(_app().routes) > 500
