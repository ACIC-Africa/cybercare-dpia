# The ODPC prior-consultation rule. Spec D-W2-6.
#
# Kenya's Data Protection Act, 2019 requires a controller to consult the
# Office of the Data Protection Commissioner (ODPC) BEFORE starting a
# processing activity that carries high risk. In Fides today "prior
# consultation" appears six times, all inside GDPR question TEXT — it is
# asked of the user and nothing acts on the answer. This module is what
# makes it fire from the register instead: it reads the DPIA's computed
# residual-risk band (Task 1's banding.py, wired up by register.py's
# assessment_band) and turns it into a finding a report can print.
#
# Pure decision logic over what register.py already computes — no writes,
# no new table, no touching privacy_assessment at all.
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from sqlalchemy.orm import Session

from fides.api.privacycare.risk.banding import CRITICAL, HIGH
from fides.api.privacycare.risk.register import RiskEntry, assessment_band, list_risks

# Josephine's brief, not the prototype: the prototype's banding arithmetic
# says nothing about a submission deadline, so this constant is the one
# place that number is encoded.
#
# THE ANCHOR (fix round 1, Important finding 2). Josephine's brief
# (01_brief_for_dpia.md, line 46) says, verbatim: "noting the 60-day
# submission window ahead of processing". "Ahead of processing" is the
# anchor: the 60 days count backward from the START of the processing
# activity, not forward from today and not from the date of the
# assessment. evaluate()'s reason text below is written as a single
# clause — "at least N days before processing begins" — specifically so
# it cannot be read as a grace period measured from filing: a DPO reading
# the earlier, unanchored wording ("submitted within 60 days") could wait
# 55 days and then start processing the next day, believing themself
# compliant. See test_the_reason_anchors_the_window_to_before_processing_
# begins in tests/privacycare/test_risk_odpc.py, which pins the exact
# phrase so this cannot silently regress back into that ambiguity.
CONSULTATION_WINDOW_DAYS = 60


@dataclass(frozen=True)
class OdpcFinding:
    required: bool
    band: str  # OUR four-value band (LOW/MEDIUM/HIGH/CRITICAL) — see evaluate()
    window_days: int  # CONSULTATION_WINDOW_DAYS, carried on the finding so a
    # caller (report.py, pdf.py) never has to import the constant separately
    # to print it next to the verdict it belongs to.
    reason: str  # a sentence a person reads in a report — names the band
    # and the window, whether or not consultation is required (silence
    # reads as "not assessed", which is wrong in both directions).
    highest_risk: Optional[RiskEntry]  # what drove the finding; None when
    # consultation is not required — "prior consultation required" without
    # the risk that caused it is not actionable.


def evaluate(db: Session, assessment_id: str) -> OdpcFinding:
    """Does this assessment's risk register require prior consultation with
    the ODPC, and if so, what drove it and by when must it be filed?

    Required when the band is HIGH or CRITICAL. LOW/MEDIUM do not require
    it — and the finding says so explicitly rather than being silent.

    THE MISTAKE THIS FUNCTION IS MOST LIKELY TO MAKE, and why it doesn't:
    the band is read through register.assessment_band(db, assessment_id) —
    OUR OWN four-value band — and never through
    privacy_assessment.risk_level. That column is a deliberately lossy
    projection: register.sync_projection writes CRITICAL there as "high",
    because Ethyca's risklevel enum has only three values (see
    banding.projected_risk_level). A rule that read the column instead of
    assessment_band would still answer THIS PARTICULAR question — is
    consultation required? — correctly today, by luck: both HIGH and a
    CRITICAL-projected-to-"high" trigger it. That is exactly what would
    make the mistake durable and invisible: it would pass every test here
    until the day something needs to tell CRITICAL apart from HIGH (a
    stricter deadline, a different filing category, an escalation to a
    named officer), and by then nobody would remember to go looking for
    it. assessment_band is the one function in this codebase that is
    allowed to answer "what band is this assessment" — see its own
    docstring in register.py — so this function calls that, not the
    column, even though only one of the two happens to matter here today.
    See test_a_critical_register_reports_critical_not_high in
    tests/privacycare/test_risk_odpc.py, which exists solely to catch a
    future "simplification" back to the column.
    """
    band = assessment_band(db, assessment_id)
    required = band in (HIGH, CRITICAL)

    # The highest-scoring risk is what drove the finding, needed whether or
    # not consultation is required to build the reason/finding — but only
    # ever carried on the finding when required (see the field's own
    # docstring): "prior consultation required" without naming the risk
    # that caused it is not actionable, and naming a risk on a finding that
    # says "not required" would misleadingly suggest that risk was still a
    # live concern.
    risks = list_risks(db, assessment_id)
    # list_risks orders by (-score, id) (register.py), so a tie at the top
    # score is broken by the smaller uuid — an accident of insertion order,
    # not a judgement that one tied risk "drives" the finding more than the
    # other. Two risks tied at 25/25 will name whichever happens to sort
    # first as "the driving risk" in the reason/report text; both are
    # equally the reason consultation is required either way, so this is a
    # presentation choice, not a wrong answer.
    highest_risk = risks[0] if risks else None

    if required:
        # One clause, one anchor: "at least N days BEFORE processing
        # BEGINS" — see the CONSULTATION_WINDOW_DAYS comment above for why
        # this exact phrasing, and why the earlier "submitted within N
        # days" wording was ambiguous/wrong.
        reason = (
            f"Residual risk band is {band} — the Data Protection Act, 2019 "
            "requires prior consultation with the Office of the Data "
            "Protection Commissioner (ODPC), submitted at least "
            f"{CONSULTATION_WINDOW_DAYS} days before this processing "
            "activity begins."
        )
    else:
        reason = (
            f"Residual risk band is {band} — prior consultation with the "
            "ODPC is NOT required. Only a high or critical residual risk "
            "triggers the requirement to submit at least "
            f"{CONSULTATION_WINDOW_DAYS} days before processing begins."
        )

    return OdpcFinding(
        required=required,
        band=band,
        window_days=CONSULTATION_WINDOW_DAYS,
        reason=reason,
        highest_risk=highest_risk if required else None,
    )
