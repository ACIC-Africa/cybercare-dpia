"""Envelope for the stale-consent detector's HTTP surface (spec D-CON-1,
Task 3). No detection logic here — see consent/detector.py for that; this
module only shapes what goes over the wire.
"""
from datetime import datetime

from pydantic import BaseModel


class StaleConsentResponse(BaseModel):
    # Every field of consent/detector.py's StaleConsent dataclass, verbatim
    # — none of them are optional there and none are made optional here,
    # and this schema adds no computed field of its own.
    #
    # No TypeScript counterpart: the detector is new, Kenyan-specific
    # ground with no Plus analogue, so no shipped admin-UI screen calls
    # this route — see tests/privacycare/test_response_model_ts_parity.py's
    # ALLOWLIST entry for this class.
    subject: str
    subject_kind: str
    notice_key: str
    notice_name: str
    consented_version: float
    live_version: float
    added_uses: list[str]
    preference: str
    received_at: datetime
