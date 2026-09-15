# C1, final review of plan 15. Every other test that touches
# `initiate_scheduled_dsr_alerts` calls it directly (see
# test_dsr_alert_job.py's own `test_initiate_scheduled_dsr_alerts_is_a_noop_
# under_test_mode`) — which proves only that the function itself behaves
# under `CONFIG.test_mode`, and nothing about whether the process that
# actually boots ever calls it at all. That gap is exactly how C1 reached a
# final review undetected: `asgi.py` used to register the job with
# `app.add_event_handler("startup", initiate_scheduled_dsr_alerts)`, which
# Starlette silently never runs once a custom `lifespan` is supplied (see
# `asgi.py`'s own comment for the full mechanics).
#
# This file drives the real ASGI lifespan protocol — the same
# startup/shutdown messages uvicorn sends — against the real app object
# (`fides.api.privacycare.asgi.app`, the module the server actually boots),
# the same way `test_api_http.py` already does for its own purposes
# (`with TestClient(app) as c:`). Nothing here performs real network I/O:
# `TestClient` drives the ASGI protocol in-process.
from starlette.testclient import TestClient

import fides.api.privacycare.asgi as asgi_module


def test_the_alert_job_is_registered_through_the_real_asgi_lifespan(monkeypatch):
    calls = []
    monkeypatch.setattr(
        asgi_module, "initiate_scheduled_dsr_alerts", lambda: calls.append(True)
    )

    # Before the fix: this ran Fides' own `lifespan` (main.py) via
    # `app.router.lifespan_context` and never consulted `router.on_startup`
    # at all — `calls` stayed empty and this assertion failed. After the
    # fix: `app.router.lifespan_context` is `_privacycare_lifespan`, which
    # calls `initiate_scheduled_dsr_alerts()` itself, inside the wrapped
    # `async with`.
    with TestClient(asgi_module.app):
        pass

    assert calls, (
        "initiate_scheduled_dsr_alerts was never invoked when the real "
        "ASGI lifespan ran against fides.api.privacycare.asgi.app — the "
        "daily alert job is not wired into the process that actually boots."
    )


def test_the_wrapped_lifespan_still_leaves_a_working_app(monkeypatch):
    """The wrap must observe Fides' own lifespan and run one extra call in
    the middle, not replace or swallow it. If the wrap were broken —
    dropping `state`, not re-raising a startup failure, exiting the `async
    with` early — it would surface as every request 500ing or the app never
    coming up, not merely as a missing alert job. A single ordinary request
    succeeding through the wrapped app is a cheap, direct check that the
    rest of startup (routes, DB session wiring, middleware) still runs
    exactly as it did before this file's change.
    """
    monkeypatch.setattr(asgi_module, "initiate_scheduled_dsr_alerts", lambda: None)

    with TestClient(asgi_module.app) as client:
        response = client.get("/api/v1/privacycare/dsr-requests")

    # Unauthenticated — see test_api_http.py for why 401 is the established
    # contract for this surface. The only thing under test here is that the
    # request completes at all through the wrapped lifespan, not the exact
    # status code.
    assert response.status_code == 401
