"""How a DPIA's residual risk is computed. Spec D-W2-3, confirmed by Carol
2026-09-16 ("confirmed as drawn").

The overall band is the band of the HIGHEST individual risk, never the
average. Averaging lets a catalogue of trivial risks bury a single critical
one — which is the whole failure the maximum exists to prevent, and is
separately a smoothing move this repo forbids in ranking generally.

Pure by design: no database, no clock. This is the rule someone will one day
have to defend to a regulator, and it should be readable on its own.
"""
import pytest

from fides.api.privacycare.risk.banding import (
    CRITICAL,
    HIGH,
    LOW,
    MEDIUM,
    band,
    overall_band,
    projected_risk_level,
    score,
)


def test_score_is_likelihood_times_severity():
    assert score(4, 3) == 12
    assert score(1, 1) == 1
    assert score(5, 5) == 25


@pytest.mark.parametrize("bad", [0, 6, -1, 25])
def test_a_score_outside_one_to_five_is_rejected(bad):
    # A 5x5 matrix has no cell for a 6. Silently clamping would invent a
    # score nobody assessed.
    with pytest.raises(ValueError):
        score(bad, 3)
    with pytest.raises(ValueError):
        score(3, bad)


@pytest.mark.parametrize(
    "value,expected",
    [(1, LOW), (4, LOW), (5, MEDIUM), (9, MEDIUM),
     (10, HIGH), (16, HIGH), (17, CRITICAL), (25, CRITICAL)],
)
def test_the_band_boundaries_are_carols(value, expected):
    # Every boundary, both sides. These four ranges are the spec.
    assert band(value) == expected


def test_every_cell_of_the_five_by_five_lands_in_a_band():
    # No reachable score falls through. 1..25 are all produced by some cell.
    for likelihood in range(1, 6):
        for severity in range(1, 6):
            assert band(score(likelihood, severity)) in (LOW, MEDIUM, HIGH, CRITICAL)


def test_the_overall_band_is_the_maximum_not_the_average():
    # THE test. One critical risk among many trivial ones: the mean is 5.4
    # (medium); the maximum is 25 (critical). If this ever returns medium,
    # a DPIA that must go to the ODPC will not.
    scores = [1, 1, 1, 1, 25]
    assert overall_band(scores) == CRITICAL
    assert sum(scores) / len(scores) < 10  # the average would have said medium


def test_an_empty_register_is_low():
    # Spec D-W2-3: with no risks logged, overall defaults to low.
    assert overall_band([]) == LOW


def test_one_risk_bands_on_itself():
    assert overall_band([12]) == HIGH


def test_order_does_not_matter():
    assert overall_band([25, 1]) == overall_band([1, 25])


def test_critical_projects_down_to_high_for_ethycas_enum():
    # Ethyca's risklevel enum has three values. Our fourth has to land
    # somewhere on their screen, and high is the honest neighbour: the
    # alternative is a blank field for the most serious assessments.
    assert projected_risk_level(CRITICAL) == HIGH


@pytest.mark.parametrize("value", [LOW, MEDIUM, HIGH])
def test_the_other_three_project_unchanged(value):
    assert projected_risk_level(value) == value


def test_an_unknown_band_is_rejected_rather_than_passed_through():
    with pytest.raises(ValueError, match="nonsense"):
        projected_risk_level("nonsense")
