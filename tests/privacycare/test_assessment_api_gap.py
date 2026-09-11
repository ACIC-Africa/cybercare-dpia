"""The admin-ui ships assessment screens that call `plus/privacy-assessments/*`.
Those routes are Fides Plus and absent from OSS. W1 implements them.

This test asserts the CURRENT state (absent) so that when W1 lands, the
assertion is inverted in the same commit that adds the routes — making the
gap closing visible in the diff rather than implicit."""
from fides.api.main import app

EXPECTED_ASSESSMENT_ROUTES = [
    "plus/privacy-assessments",
    "plus/privacy-assessments/summary",
    "plus/privacy-assessments/templates",
    "plus/privacy-assessments/{id}",
    "plus/privacy-assessments/{id}/questions",
    "plus/privacy-assessments/{id}/questions/{question_id}",
    "plus/privacy-assessments/{id}/evidence",
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


def test_assessment_routes_are_absent_in_oss():
    paths = _registered_paths()
    present = [r for r in EXPECTED_ASSESSMENT_ROUTES if any(r in p for p in paths)]
    assert not present, (
        "Assessment routes now exist — W1 has landed. Invert this assertion "
        f"to require them. Found: {present}"
    )


def test_the_app_still_boots():
    """Guard: if the import itself breaks, the test above passes vacuously."""
    assert len(_registered_paths()) > 50
