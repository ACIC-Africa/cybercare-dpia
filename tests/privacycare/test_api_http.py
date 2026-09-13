# Every other behavioural test in this package calls private helpers
# (_list_assessments, _summary, _questions_for, ...) directly, so nothing
# ever proves an unauthenticated HTTP request is actually rejected, and no
# `response_model` is ever exercised by FastAPI's own serialization path.
# These tests issue real HTTP requests through Starlette's TestClient against
# the real ASGI app (fides.api.privacycare.asgi:app — the module the server
# actually boots) and check what an unauthenticated caller gets back.
#
# `tests/fides/api/v1/endpoints/test_dataset_endpoints.py` establishes what
# Fides itself returns for an unauthenticated request
# (`test_create_dataset_not_authenticated` asserts `HTTP_401_UNAUTHORIZED`);
# this file matches that rather than assuming 401 is right.
import pytest
from starlette import status
from starlette.testclient import TestClient

from fides.api.privacycare.api.router import PRIVACYCARE_PREFIX
from fides.api.privacycare.asgi import app

# One representative id is enough here — these routes 401 before the
# path parameter or the database is ever consulted (verify_oauth_client
# raises before the endpoint function runs), so which id is passed is
# irrelevant to what's under test.
_ASSESSMENT_ID = "does-not-matter"

GET_ROUTE_SUFFIXES = [
    "",
    "/summary",
    "/templates",
    f"/{_ASSESSMENT_ID}",
    f"/{_ASSESSMENT_ID}/evidence",
    "/tasks",
    "/tasks/does-not-matter",
]


@pytest.fixture(scope="module")
def client():
    # `with TestClient(app) as c:` runs the app's real startup lifespan
    # (migrations, scheduler, etc.) — module-scoped so the five requests
    # below share one boot rather than paying for it five times.
    with TestClient(app) as c:
        yield c


@pytest.mark.parametrize("suffix", GET_ROUTE_SUFFIXES)
def test_unauthenticated_get_is_rejected(client, suffix):
    response = client.get(f"{PRIVACYCARE_PREFIX}{suffix}")
    assert response.status_code == status.HTTP_401_UNAUTHORIZED, (
        f"{PRIVACYCARE_PREFIX}{suffix} did not reject an unauthenticated "
        f"request: got {response.status_code} {response.text!r}"
    )


# The business-process ROPA surface, under its own namespace. Same bar as the
# assessment routes: a privacy product must not serve a record of processing
# to an unauthenticated caller.
PROCESS_GET_SUFFIXES = ["", "/bp_does_not_matter/ropa"]


@pytest.mark.parametrize("suffix", PROCESS_GET_SUFFIXES)
def test_unauthenticated_business_process_get_is_rejected(client, suffix):
    from fides.api.privacycare.api.router import PRIVACYCARE_PROCESSES_PREFIX

    response = client.get(f"{PRIVACYCARE_PROCESSES_PREFIX}{suffix}")
    assert response.status_code == status.HTTP_401_UNAUTHORIZED, (
        f"{PRIVACYCARE_PROCESSES_PREFIX}{suffix} did not reject an "
        f"unauthenticated caller (got {response.status_code})"
    )


def test_unauthenticated_business_process_write_is_rejected(client):
    from fides.api.privacycare.api.router import PRIVACYCARE_PROCESSES_PREFIX

    response = client.post(
        PRIVACYCARE_PROCESSES_PREFIX, json={"name": "Should not be created"}
    )
    assert response.status_code == status.HTTP_401_UNAUTHORIZED, (
        f"POST {PRIVACYCARE_PROCESSES_PREFIX} did not reject an "
        f"unauthenticated caller (got {response.status_code})"
    )


# The questionnaire chat's own router (task 4), a third namespace. Same bar
# as the two surfaces above: a privacy product must not serve a DPIA
# conversation's transcript to an anonymous caller.
def test_unauthenticated_chat_transcript_get_is_rejected(client):
    from fides.api.privacycare.api.router import PRIVACYCARE_CHAT_PREFIX

    response = client.get(f"{PRIVACYCARE_CHAT_PREFIX}/messages/does-not-matter")
    assert response.status_code == status.HTTP_401_UNAUTHORIZED, (
        f"{PRIVACYCARE_CHAT_PREFIX}/messages/does-not-matter did not reject "
        f"an unauthenticated caller (got {response.status_code})"
    )
