"""The assessments list reports a TRUE risk band, and degrades in exactly one
way.

Background. The assessment card used to render Ethyca's `risk_level`, a lossy
projection with three values where `critical` is written as `high`. So a card
said High and the detail page it opened said Critical — one click apart, about
the distinction ODPC prior consultation turns on. The list now carries the true
band instead.

That made list_assessments read a PrivacyCare table for the first time, and
PrivacyCare's migration chain is a separate step from Fides' own, so there is a
real deployment window where privacycare_dpia_risk does not exist yet. The list
tolerates that window rather than 500ing for every user.

This file pins the LIMIT of that tolerance. A missing table means zero risks
recorded, and the band of an empty register genuinely is LOW, so LOW is the
right answer and not a guess. Any other failure — a revoked GRANT, a column
renamed underneath us — means the table is there and may hold risks we failed
to read, and answering LOW would be a confident wrong answer about a compliance
record. Those must surface.
"""

import pytest
import sqlalchemy

from fides.api.privacycare.api.assessments import (
    _is_missing_risk_table,
    _risk_band_for,
    _risk_bands_by_assessment,
)
from fides.api.privacycare.risk.banding import LOW


class _Orig(Exception):
    """Stands in for the psycopg2 error SQLAlchemy wraps, which carries the
    Postgres SQLSTATE on .pgcode."""

    def __init__(self, pgcode: str) -> None:
        super().__init__(pgcode)
        self.pgcode = pgcode


def _programming_error(pgcode: str) -> sqlalchemy.exc.ProgrammingError:
    return sqlalchemy.exc.ProgrammingError(
        "select 1", {}, _Orig(pgcode)  # type: ignore[arg-type]
    )


class _FailingSession:
    """A session whose every execute() raises, and that records a rollback."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc
        self.rolled_back = False

    def execute(self, *_args, **_kwargs):
        raise self._exc

    def rollback(self) -> None:
        self.rolled_back = True


def test_undefined_table_is_the_one_tolerated_failure():
    assert _is_missing_risk_table(_programming_error("42P01"))


@pytest.mark.parametrize(
    "pgcode, what",
    [
        ("42501", "insufficient privilege — a revoked GRANT"),
        ("42703", "undefined column — renamed underneath us"),
        ("42P02", "undefined parameter"),
    ],
)
def test_every_other_programming_error_is_not_tolerated(pgcode, what):
    assert not _is_missing_risk_table(_programming_error(pgcode)), what


def test_a_missing_table_degrades_to_low_rather_than_failing_the_list():
    session = _FailingSession(_programming_error("42P01"))
    assert _risk_bands_by_assessment(session) == {}
    assert session.rolled_back, "the aborted transaction must be rolled back"

    session = _FailingSession(_programming_error("42P01"))
    assert _risk_band_for(session, "pa_fuel_card_issuance") == LOW


@pytest.mark.parametrize("pgcode", ["42501", "42703"])
def test_a_readable_table_we_failed_to_read_surfaces_instead_of_reporting_low(
    pgcode,
):
    """The regression this file exists for. Reporting LOW here would say
    'no risks recorded' about an assessment whose risks we simply could not
    read."""
    session = _FailingSession(_programming_error(pgcode))
    with pytest.raises(sqlalchemy.exc.ProgrammingError):
        _risk_bands_by_assessment(session)

    session = _FailingSession(_programming_error(pgcode))
    with pytest.raises(sqlalchemy.exc.ProgrammingError):
        _risk_band_for(session, "pa_fuel_card_issuance")
