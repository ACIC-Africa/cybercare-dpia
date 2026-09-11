"""The admin-ui ships assessment screens that call `plus/privacy-assessments/*`.

W1 has landed: `fides.api.privacycare.asgi` registers our own router onto
Fides' `app_setup.ROUTERS`, so the six READ paths below are now served.

This test used to assert the gap (routes absent) and said, in its own
docstring, to invert the assertion once W1 lands — making the gap closing
visible in the diff rather than implicit. That inversion happens here.

Only the six READ paths are asserted present. The remaining paths in the
original gap list (single-question fetch, questionnaire, questionnaire
reminders, PDF export, tasks, and config) are WRITE-adjacent or not part of
this read surface — they belong to plan 04 and are still correctly absent.
That absence is intentional, not a regression: see
`test_write_and_other_routes_still_absent` below.
"""
from fides.api.privacycare.asgi import app

EXPECTED_READ_ROUTES = [
    "plus/privacy-assessments",
    "plus/privacy-assessments/summary",
    "plus/privacy-assessments/templates",
    "plus/privacy-assessments/{assessment_id}",
    "plus/privacy-assessments/{assessment_id}/questions",
    "plus/privacy-assessments/{assessment_id}/evidence",
]

# Not part of this read surface (plan 04, write paths) — still absent, and
# that is correct. Kept here (with the original `{id}` naming from the gap
# list) so a later reader sees the absence was checked, not overlooked.
EXPECTED_STILL_ABSENT_ROUTES = [
    "plus/privacy-assessments/{id}/questions/{question_id}",
    "plus/privacy-assessments/{id}/questionnaire",
    "plus/privacy-assessments/{id}/questionnaire/reminders",
    "plus/privacy-assessments/{id}/pdf",
    "plus/privacy-assessments/tasks",
    "plus/privacy-assessments/tasks/{task_id}",
    "plus/privacy-assessments/config",
    "plus/privacy-assessments/config/defaults",
]


def _registered_paths():
    return {getattr(r, "path", "") for r in app.routes}


def test_assessment_routes_are_now_served_by_privacycare():
    paths = _registered_paths()
    missing = [r for r in EXPECTED_READ_ROUTES if not any(r in p for p in paths)]
    assert not missing, f"read routes not registered: {missing}"


def test_write_and_other_routes_still_absent():
    # Guard against mistaking this deliberate, still-pending scope (plan 04)
    # for a regression: these paths are NOT expected to exist yet.
    paths = _registered_paths()
    present = [r for r in EXPECTED_STILL_ABSENT_ROUTES if any(r in p for p in paths)]
    assert not present, (
        "Write/other assessment routes now exist — update this test's scope. "
        f"Found: {present}"
    )


def test_the_app_still_boots():
    """Guard: if the import itself breaks, the test above passes vacuously."""
    assert len(_registered_paths()) > 50
