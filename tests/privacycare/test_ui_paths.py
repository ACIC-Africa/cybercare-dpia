# Does the shipped admin UI's URL actually reach one of our routes?
#
# Nothing asked that for five plans. tests/privacycare/test_api_registration.py
# and test_api_http.py both build their URLs from PRIVACYCARE_PREFIX — our own
# constant — so they proved the routes exist where we put them, and never that
# where we put them is where the client looks. They did not: the prefix was a
# bare "/plus/privacy-assessments", mounted at the root of the app beside only
# "/" and "/health", while all 196 of Fides' own routes live under /api/v1 and
# the UI resolves its calls against that base. Every screen would have 404'd.
#
# This file deliberately takes NOTHING from our own source. It reads the
# shipped TypeScript — the RTK slice's `url:` values and the UI's configured
# API base — turns them into the absolute paths a browser will request, and
# asserts each one matches a registered route.
import pathlib
import re

import pytest

ADMIN_UI = pathlib.Path(__file__).parents[2] / "clients" / "admin-ui"
SLICE = ADMIN_UI / "src/features/privacy-assessments/privacy-assessments.slice.ts"
API_BASE_ENV = ADMIN_UI / ".env.test"


def _api_base() -> str:
    """The base the UI resolves its relative endpoint URLs against."""
    text = API_BASE_ENV.read_text(encoding="utf-8")
    match = re.search(r"^NEXT_PUBLIC_FIDESCTL_API=(\S+)", text, re.M)
    assert match, f"no NEXT_PUBLIC_FIDESCTL_API in {API_BASE_ENV}"
    return match.group(1).rstrip("/")


def _slice_urls() -> list[str]:
    """Every `url:` the privacy-assessments slice requests, as a path pattern.

    Template expressions (`${id}`) become a FastAPI path parameter so the two
    can be compared; a query string is dropped, since routing ignores it.
    """
    text = SLICE.read_text(encoding="utf-8")
    urls = re.findall(r"url:\s*[`\"']([^`\"']+)[`\"']", text)
    assert urls, "parsed no url: values — the slice's shape changed"
    patterns = []
    for url in urls:
        url = url.split("?", 1)[0]
        url = re.sub(r"\$\{[^}]+\}", "{param}", url)
        patterns.append(url.strip("/"))
    return sorted(set(patterns))


def _registered_paths() -> set[str]:
    import fides.api.privacycare.asgi as asgi

    return {
        re.sub(r"\{[^}]+\}", "{param}", getattr(route, "path", ""))
        for route in asgi.app.routes
    }


# Endpoints a later plan still owns — genuinely not built yet, so this test
# must fail for a WRONG path, not for an unbuilt one.
#
# This used to be a substring match on ("questionnaire", "chat/", "config",
# "pdf"), which would have kept silently skipping plus/chat/questionnaire/
# start and plus/chat/questionnaire/reply even after task 3 built and
# registered both — exactly the kind of stale skip masking real coverage
# this plan's router-prefix mistake (test_response_model_ts_parity.py's
# PRIVACYCARE_PATH_PREFIXES) already happened twice for. An exact set of the
# URLs still unbuilt does not silently widen to swallow a route a later task
# ships.
#
# plus/chat/questionnaire/messages/{param} came out here in task 4: the
# transcript route now exists and is registered, so this becomes a live
# assertion rather than a skip.
#
# plus/privacy-assessments/config and .../config/defaults came out here in
# task 1 of the config-and-pdf plan (fides/api/privacycare/api/config.py):
# both are built and registered, so — same as the chat transcript route
# above — this becomes a live assertion rather than a skip.
NOT_BUILT_YET = frozenset(
    {
        "plus/privacy-assessments/{param}/pdf",
        "plus/privacy-assessments/{param}/questionnaire",
        "plus/privacy-assessments/{param}/questionnaire/reminders",
    }
)


@pytest.mark.parametrize("url", _slice_urls())
def test_every_url_the_shipped_ui_calls_reaches_a_registered_route(url):
    if url in NOT_BUILT_YET:
        pytest.skip(f"{url} belongs to a workstream that is not built yet")

    expected = f"{_api_base()}/{url}"
    assert expected in _registered_paths(), (
        f"the shipped admin UI calls {expected!r}, which is not a registered "
        f"route. The UI's path is the requirement; ours is the bug."
    )


def test_the_api_base_is_the_one_fides_itself_serves_under():
    # A sanity check on the premise: if the UI's base ever stopped matching
    # Fides' own V1 prefix, the test above would start asserting a path no
    # Fides route uses either, and would be measuring nothing.
    from fides.common.urn_registry import V1_URL_PREFIX

    assert _api_base() == V1_URL_PREFIX
