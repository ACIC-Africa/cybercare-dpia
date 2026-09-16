# The ODPC prior-consultation rule (Task 4, spec D-W2-6): does THIS
# assessment's risk register require prior consultation with Kenya's Office
# of the Data Protection Commissioner before processing begins, and if so,
# what drove that and by when must it be submitted?
#
# In Fides today "prior consultation" appears six times, all inside GDPR
# question TEXT — asked of the user, acted on by nobody. evaluate() is what
# makes it fire from the register instead of sitting there as a question
# nobody's answer changes anything about.
#
# Same fixture convention as test_risk_register.py: add_risk/sync_projection
# never call commit themselves, but the fixture still monkeypatches commit ->
# flush (never a no-op — base_class.persist_obj elsewhere does add/commit/
# refresh, and a no-op starves refresh()) so this file behaves identically
# whether or not a future helper here starts calling it.
import uuid

import pytest
import sqlalchemy
from sqlalchemy.orm import Session

from fides.api.privacycare.risk.banding import CRITICAL, HIGH, LOW, MEDIUM
from fides.api.privacycare.risk.odpc import CONSULTATION_WINDOW_DAYS, OdpcFinding, evaluate
from fides.api.privacycare.risk.register import add_risk

DB_URL = "postgresql://postgres:fides@127.0.0.1:5442/fides"


@pytest.fixture
def db(monkeypatch):
    engine = sqlalchemy.create_engine(DB_URL)
    with Session(engine) as session:
        monkeypatch.setattr(session, "commit", session.flush)
        yield session
        session.rollback()


def _seed_template(db) -> str:
    tid = f"tpl_{uuid.uuid4().hex[:8]}"
    db.execute(
        sqlalchemy.text(
            "INSERT INTO assessment_template "
            "(id, version, name, assessment_type, region, is_active) "
            "VALUES (:id, '1.0', 'Kenya DPA 2019 DPIA', :assessment_type, 'KE', true)"
        ),
        {"id": tid, "assessment_type": f"dpia_{uuid.uuid4().hex[:8]}"},
    )
    return tid


def _seed_assessment(db, template_id: str) -> str:
    aid = f"asmt_{uuid.uuid4().hex[:8]}"
    db.execute(
        sqlalchemy.text(
            "INSERT INTO privacy_assessment "
            "(id, template_id, name, status, system_fides_key) "
            "VALUES (:id, :tid, 'Fuel card DPIA', 'in_progress', 'sys_test')"
        ),
        {"id": aid, "tid": template_id},
    )
    return aid


@pytest.fixture
def assessment_id(db) -> str:
    return _seed_assessment(db, _seed_template(db))


def _assessment_row(db, assessment_id: str) -> dict:
    row = db.execute(
        sqlalchemy.text("SELECT * FROM privacy_assessment WHERE id = :id"),
        {"id": assessment_id},
    ).mappings().first()
    assert row is not None
    return dict(row)


def test_window_is_sixty_days():
    assert CONSULTATION_WINDOW_DAYS == 60


def test_an_empty_register_does_not_require_consultation(db, assessment_id):
    finding = evaluate(db, assessment_id)

    assert isinstance(finding, OdpcFinding)
    assert finding.required is False
    assert finding.band == LOW
    assert finding.window_days == CONSULTATION_WINDOW_DAYS
    assert finding.highest_risk is None


@pytest.mark.parametrize(
    "band, likelihood, severity",
    [(HIGH, 4, 3), (CRITICAL, 5, 5)],
)
def test_high_or_critical_requires_consultation(db, assessment_id, band, likelihood, severity):
    add_risk(
        db,
        assessment_id=assessment_id,
        category="confidentiality",
        description="the risk that drives escalation",
        likelihood=likelihood,
        severity=severity,
    )

    finding = evaluate(db, assessment_id)

    assert finding.required is True
    assert finding.band == band
    assert finding.window_days == CONSULTATION_WINDOW_DAYS


@pytest.mark.parametrize(
    "band, likelihood, severity",
    [(LOW, 1, 1), (MEDIUM, 3, 2)],
)
def test_medium_or_low_does_not_require_consultation(db, assessment_id, band, likelihood, severity):
    add_risk(
        db,
        assessment_id=assessment_id,
        category="availability",
        description="a modest risk",
        likelihood=likelihood,
        severity=severity,
    )

    finding = evaluate(db, assessment_id)

    assert finding.required is False
    assert finding.band == band
    assert finding.highest_risk is None


def test_the_finding_names_the_highest_scoring_risk(db, assessment_id):
    add_risk(db, assessment_id=assessment_id, category="availability",
              description="trivial", likelihood=1, severity=1)
    driver = add_risk(db, assessment_id=assessment_id, category="integrity",
                        description="the one that drives it", likelihood=5, severity=5)
    add_risk(db, assessment_id=assessment_id, category="physical_harm",
              description="middling", likelihood=3, severity=3)

    finding = evaluate(db, assessment_id)

    assert finding.required is True
    assert finding.highest_risk == driver


def test_the_reason_names_both_the_band_and_the_window(db, assessment_id):
    add_risk(db, assessment_id=assessment_id, category="confidentiality",
              description="critical risk", likelihood=5, severity=5)

    finding = evaluate(db, assessment_id)

    assert finding.band in finding.reason
    assert str(CONSULTATION_WINDOW_DAYS) in finding.reason


def test_the_reason_names_the_band_and_window_even_when_not_required(db, assessment_id):
    assert evaluate(db, assessment_id).band == LOW  # empty register

    finding = evaluate(db, assessment_id)

    assert finding.band in finding.reason
    assert str(CONSULTATION_WINDOW_DAYS) in finding.reason


def test_a_critical_register_reports_critical_not_high(db, assessment_id):
    # THE MISTAKE THIS TEST EXISTS TO CATCH: add_risk's own sync_projection
    # writes Ethyca's three-value risk_level column as "high" for a critical
    # register (CRITICAL collapses to "high" — banding.projected_risk_level).
    # A rule that read privacy_assessment.risk_level instead of
    # assessment_band(...) would still answer "is consultation required?"
    # correctly here (both high and high-that-was-critical trigger it) —
    # which is exactly the durable, invisible mistake the brief warns about.
    # This proves evaluate() reports the real, unprojected band.
    add_risk(db, assessment_id=assessment_id, category="integrity",
              description="catastrophic", likelihood=5, severity=5)

    row = _assessment_row(db, assessment_id)
    assert row["risk_level"] == "high", (
        "test setup assumption broken: the projection should read 'high' here"
    )

    finding = evaluate(db, assessment_id)

    assert finding.band == CRITICAL
    assert finding.band != row["risk_level"]
