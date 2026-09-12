"""Who an authenticated request acts as.

Lives in its own module for a structural reason, not a stylistic one.
Both route modules need it, and if either imported it from the other, that
import would drag the whole exporting module in — binding ALL of its routes
— the instant the importing module loaded. api/router.py's register() must
import api.tasks before api.assessments so that GET /tasks is registered
ahead of GET /{assessment_id}; otherwise FastAPI matches "tasks" as an
assessment id and the progress bar 404s forever against a route that
exists. A cross-import between the two route modules silently defeats that
ordering.

This was first patched with a lazy import inside a route function. That
worked and was tested, but it was load-bearing and invisible: nothing stops
a future reader hoisting it back to the top, and the symptom is a 404 on a
route that demonstrably exists. A module neither route module depends on
removes the hazard instead of detecting it.
"""
from fides.api.models.client import ClientDetail


def _created_by_from_client(client: ClientDetail) -> str:
    """Every answer_version must name an author — NEVER NULL. An answer
    version with no author is worthless as the evidence this versioned
    design exists to produce: a regulator asking "who wrote this" must
    never get NULL back.

    client.user_id is None on two LEGITIMATE, ordinary paths — not just a
    hypothetical edge case — so this falls back rather than rejecting the
    write (fix round 1, MAJOR finding 2):

    1. The root/admin login. user_endpoints.py's user_login, for the root
       username/password branch, fetches
       ClientDetail.get(db, object_id=CONFIG.security.oauth_root_client_id,
       ...) directly — it never calls create_client_and_secret/
       perform_login, so the root client is never linked to a FidesUser.
       (Ethyca's own oauth/utils.py _populate_request_context_from_client
       already treats this client as needing a substitute actor id for its
       own audit-context purposes, for exactly this reason.)
    2. Any standalone machine-to-machine API client. POST
       /api/v1/oauth/client (oauth_endpoints.py's create_client) calls
       ClientDetail.create_client_and_secret(..., scopes=...) with no
       user_id argument at all — a fully ordinary, documented way to call
       this API, not merely possible in theory.

    Both are legitimate ways to reach this route, so a NULL user_id must
    not be rejected. Falling back to the client's own id, prefixed to mark
    it as a CLIENT rather than a USER, keeps the two id spaces
    unambiguous in the audit trail while still naming an author.
    """
    if client.user_id:
        return client.user_id
    return f"client:{client.id}"
