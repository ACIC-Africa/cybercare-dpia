# Do the query parameters the shipped UI sends actually reach our routes?
#
# `?status=` was honoured by accident. The filter logic was tested by calling
# the private helper with status=..., so renaming the ROUTE's query parameter —
# `status` to `state`, say — left all 360 tests green while FastAPI quietly
# ignored the UI's `?status=` and returned an unfiltered list. A data protection
# officer filtering to "needs attention" would have been looking at every
# assessment in the estate and had no way to tell.
#
# Same species as the route-prefix bug: the expected value came from us, not
# from the client. So these tests derive the parameter names from the shipped
# RTK slice and drive the routes over real HTTP, where a rename actually shows.
import pathlib
import re

import pytest
from starlette.testclient import TestClient

from fides.api.oauth.utils import verify_oauth_client
from fides.api.privacycare.api.router import PRIVACYCARE_PREFIX
from fides.api.privacycare.asgi import app

SLICE = (
    pathlib.Path(__file__).parents[2]
    / "clients/admin-ui/src/features/privacy-assessments/privacy-assessments.slice.ts"
)

# endpoint name in the slice -> the route path it calls
LIST_ENDPOINTS = {
    "getPrivacyAssessments": "",
    "getAssessmentTasks": "/tasks",
}


def _query_params_the_ui_sends(endpoint: str) -> set[str]:
    """The query-arg field names declared on one slice endpoint.

    RTK Query passes that object straight through as the request's query
    string, so these names ARE the wire contract.
    """
    text = SLICE.read_text(encoding="utf-8")
    match = re.search(
        rf"{endpoint}: build\.query<.*?\{{(.*?)\}}\s*\|\s*void", text, re.S
    )
    assert match, f"could not parse {endpoint}'s query-arg type — the slice moved"
    names = set(re.findall(r"(\w+)\??:", match.group(1)))
    assert names, f"parsed no parameter names out of {endpoint}"
    return names



def _declared_query_params(route) -> set[str]:
    """Every query parameter the route binds, including sub-dependencies.

    Recursion is not incidental: `page` and `size` are declared by
    fastapi_pagination's `Params = Depends()`, not on the route signature, so
    a walk of route.dependant.query_params alone reports them missing and this
    test would fail against correct code.
    """
    names: set[str] = set()
    stack = [route.dependant]
    while stack:
        dependant = stack.pop()
        names.update(p.name for p in dependant.query_params)
        stack.extend(dependant.dependencies)
    return names


@pytest.fixture(scope="module")
def client():
    # Auth is bypassed deliberately: these tests are about query-parameter
    # BINDING, and test_api_http.py already proves every route rejects an
    # unauthenticated caller. Leaving auth on would mean minting a token and
    # would test the wrong thing.
    app.dependency_overrides[verify_oauth_client] = lambda: None
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.pop(verify_oauth_client, None)


@pytest.mark.parametrize(
    "endpoint,suffix", LIST_ENDPOINTS.items(), ids=list(LIST_ENDPOINTS)
)
def test_every_query_parameter_the_ui_sends_is_declared_by_the_route(
    endpoint, suffix
):
    route = next(
        r
        for r in app.routes
        if getattr(r, "path", "") == f"{PRIVACYCARE_PREFIX}{suffix}"
        and "GET" in getattr(r, "methods", set())
    )
    missing = _query_params_the_ui_sends(endpoint) - _declared_query_params(route)
    assert not missing, (
        f"{endpoint} sends {sorted(missing)}, which {route.path} does not "
        f"declare. FastAPI ignores unknown query parameters silently, so the "
        f"request succeeds and the filter is simply not applied."
    )


@pytest.mark.parametrize(
    "suffix", sorted(LIST_ENDPOINTS.values()), ids=["assessments", "tasks"]
)
def test_the_status_filter_actually_filters_over_http(client, suffix):
    # Over real HTTP with a real query string — the layer a parameter rename
    # breaks. Asking for a status nothing can have must return nothing; asking
    # for none must return everything. If the parameter stopped binding, the
    # first assertion fails because the list comes back unfiltered.
    unfiltered = client.get(f"{PRIVACYCARE_PREFIX}{suffix}")
    assert unfiltered.status_code == 200, unfiltered.text

    filtered = client.get(
        f"{PRIVACYCARE_PREFIX}{suffix}", params={"status": "no_such_status"}
    )
    assert filtered.status_code == 200, filtered.text
    assert filtered.json()["total"] == 0, (
        "filtering by a status nothing has returned rows, so ?status= is not "
        "reaching the query — the UI's filter is being ignored"
    )
