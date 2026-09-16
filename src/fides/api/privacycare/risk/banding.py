"""How a DPIA's residual risk is computed. Spec D-W2-3, confirmed by Carol
2026-09-16 ("confirmed as drawn").

The overall band is the band of the HIGHEST individual risk, never the
average. Averaging lets a catalogue of trivial risks bury a single critical
one — which is the whole failure the maximum exists to prevent, and is
separately a smoothing move this repo forbids in ranking generally (Doc 10
§X.10, forbidden move 2: averaging or smoothing uplift values).

Pure by design: no database, no clock. This is the rule someone will one day
have to defend to a regulator, and it should be readable on its own. The
banding is the arithmetic that decides whether a DPIA is critical enough to
escalate to the ODPC (Office of the Data Protection Commissioner).
"""
from typing import Iterable

# The four risk bands, as string constants.
LOW = "low"
MEDIUM = "medium"
HIGH = "high"
CRITICAL = "critical"

# The four risk bands mapped to score ranges (inclusive on both ends).
# A 5×5 likelihood/severity matrix produces scores 1..25.
BANDS = (
    (1, 4, LOW),
    (5, 9, MEDIUM),
    (10, 16, HIGH),
    (17, 25, CRITICAL),
)


def score(likelihood: int, severity: int) -> int:
    """Compute residual risk as likelihood × severity.

    Both parameters must be integers in the range 1..5 (representing a cell
    in a 5×5 risk matrix). Values outside this range are rejected with
    ValueError rather than silently clamped, because silently clamping would
    invent a score nobody assessed.

    Args:
        likelihood: An integer 1..5
        severity: An integer 1..5

    Returns:
        The product likelihood × severity (1..25)

    Raises:
        ValueError: If either parameter is outside 1..5
    """
    if not (1 <= likelihood <= 5):
        raise ValueError(
            f"likelihood must be 1..5, got {likelihood}"
        )
    if not (1 <= severity <= 5):
        raise ValueError(
            f"severity must be 1..5, got {severity}"
        )
    return likelihood * severity


def band(score_value: int) -> str:
    """Map a risk score to its band.

    The four bands are defined by the spec (D-W2-3): 1-4 is LOW, 5-9 is
    MEDIUM, 10-16 is HIGH, 17-25 is CRITICAL. Every score 1..25 produced
    by a cell in the 5×5 matrix has a band.

    Args:
        score_value: An integer 1..25

    Returns:
        One of LOW, MEDIUM, HIGH, CRITICAL
    """
    for min_score, max_score, band_name in BANDS:
        if min_score <= score_value <= max_score:
            return band_name
    # This should never happen for valid inputs (1..25), but catch it rather
    # than silently returning None.
    raise ValueError(f"score {score_value} does not fall into any band")


def overall_band(scores: Iterable[int]) -> str:
    """The band of the highest risk, not the average.

    When multiple risks are recorded in a DPIA, the overall risk level is
    determined by the most serious one. Averaging would let a catalogue of
    trivial risks bury a single critical one — which is the whole reason
    the maximum exists.

    An empty register (no risks logged) defaults to LOW, as per spec D-W2-3.

    Args:
        scores: An iterable of risk scores (integers 1..25)

    Returns:
        One of LOW, MEDIUM, HIGH, CRITICAL (or LOW if scores is empty)
    """
    scores_list = list(scores)
    if not scores_list:
        return LOW
    max_score = max(scores_list)
    return band(max_score)


def projected_risk_level(band_value: str) -> str:
    """Map a four-value band to Ethyca's three-value risklevel enum.

    Ethyca's risklevel enum has exactly three values: high, medium, low.
    Our banding system has four: CRITICAL, HIGH, MEDIUM, LOW.

    The mapping is lossy by necessity:
    - CRITICAL → HIGH (the most serious assessments must not show as blank
      on Ethyca's admin screen; HIGH is the honest neighbour, and a blank
      field would be worse than an approximate one)
    - HIGH → HIGH
    - MEDIUM → MEDIUM
    - LOW → LOW

    Unknown band values are rejected with ValueError rather than silently
    passed through, which would hide a caller bug.

    Args:
        band_value: One of LOW, MEDIUM, HIGH, CRITICAL

    Returns:
        One of low, medium, high (Ethyca's enum values)

    Raises:
        ValueError: If band_value is not a known band
    """
    if band_value == CRITICAL:
        return HIGH
    elif band_value in (HIGH, MEDIUM, LOW):
        return band_value
    else:
        raise ValueError(
            f"projected_risk_level: unknown band {band_value!r} (nonsense); "
            f"must be one of {LOW!r}, {MEDIUM!r}, {HIGH!r}, {CRITICAL!r}"
        )
