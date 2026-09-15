"""Envelopes for the DSR register's HTTP surface. No register or delegation
logic here — see dsr/register.py and dsr/delegation.py for that; this module
only shapes what goes over the wire.
"""
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class DsrRequestCreate(BaseModel):
    # `right` is validated by the core (timeline_days, called from inside
    # record_request), not here — a Literal of the six Kenyan rights would
    # duplicate that check and could drift from KENYAN_RIGHTS. See
    # dsr.py's create_dsr_request for how the core's ValueError surfaces.
    right: str
    subject_identifier: str = Field(min_length=1)
    owner_email: Optional[str] = None
    business_process_id: Optional[str] = None
    # I4 (final review): optional and defaulting to now (register.py's
    # record_request), not defaulted here, same reasoning as
    # DsrNotificationRequest.notified_at below — the "now" that matters is
    # when the route runs. Set this when the obligation arrived by paper or
    # email before anyone typed it in: the statutory clock must start on
    # receipt, not on data entry, or a request recorded three days late
    # silently grants the controller three extra days. dsr.py's route
    # rejects a future value with 400 before this ever reaches the core.
    received_at: Optional[datetime] = None


class DsrDecisionRequest(BaseModel):
    # outcome and grounds are likewise validated inside record_decision
    # (valid outcome, non-blank grounds) — not re-checked here.
    outcome: str
    grounds: str


class DsrNotificationRequest(BaseModel):
    # Optional and defaulting to now (dsr.py's record_dsr_notification),
    # not defaulted here: the "now" that matters is when the route runs,
    # not when this object happened to be constructed by a caller who held
    # onto it.
    notified_at: Optional[datetime] = None


class DsrRequestResponse(BaseModel):
    # Every privacycare_dsr_request column, plus one computed field the
    # table does not carry:
    #   - owner_source: D-DSR-8's fallback chain (explicit |
    #     business_process | configured_dpo | unassigned), STORED by
    #     record_request at creation (register.py) and read straight off
    #     the row here — not re-derived. Fix round 1 on task 4: an earlier
    #     version inferred this at read time from owner_email alone
    #     ("explicit" if set, else "unassigned"), which could report
    #     "explicit" for a row the business-process or DPO fallback had
    #     actually produced — a false claim, not merely an imprecise one,
    #     about how a regulatory obligation's owner was determined.
    #     Optional because the column is nullable (a hand-written INSERT
    #     bypassing record_request, or a future write path that never
    #     calls resolve_owner, must still be a legal row) — see the
    #     05b1920196e4_dsr_owner_source migration.
    #   - days_left: computed from deadline_at at response time, None when
    #     the right is unclocked (objection, OQ-PRIVACY-02) — never 0, which
    #     would read as "due today" rather than "no deadline exists".
    #   - business_process_id: STORED by record_request (final review minor
    #     finding), not merely consulted to resolve the owner and then
    #     dropped — half of D-DSR-8's auditable claim ("who owns this, and
    #     which process is that owner attached to") was previously missing.
    #     None both when no process was given and for any row written
    #     before the column existed.
    #   - fides_privacy_request_status (I5, final review, narrowed per
    #     ruling): the delegated `privacyrequest`'s CURRENT status, read
    #     live at response time — not mirrored/stored, and not the same
    #     thing as fides_privacy_request_id, which only ever records that a
    #     delegation happened. None when the right never delegated
    #     (fides_privacy_request_id is also None). The literal string
    #     "vanished" — never a real PrivacyRequestStatus value — when
    #     fides_privacy_request_id IS set but the row it names no longer
    #     resolves, so a caller can tell "there is nowhere to look" apart
    #     from an ordinary in-progress status. Full status *mirroring*
    #     (syncing register.status from Fides' pipeline) is out of scope
    #     for this plan; this only answers "where is this obligation right
    #     now" on the one screen that already exists.
    #
    # No TypeScript counterpart: see tests/privacycare/
    # test_response_model_ts_parity.py's ALLOWLIST entry for this class.
    id: str
    right: str
    subject_identifier: str
    received_at: datetime
    deadline_at: Optional[datetime]
    owner_email: Optional[str]
    owner_source: Optional[str]
    business_process_id: Optional[str]
    status: str
    outcome: Optional[str]
    outcome_grounds: Optional[str]
    decided_by: Optional[str]
    decided_at: Optional[datetime]
    subject_notified_at: Optional[datetime]
    fides_privacy_request_id: Optional[str]
    fides_privacy_request_status: Optional[str]
    created_at: Optional[datetime]
    updated_at: Optional[datetime]
    days_left: Optional[int]
