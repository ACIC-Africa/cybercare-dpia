"""Envelopes for the DPIA screening gate's HTTP surface (spec 2026-09-16
D-W2-7, Task 4). No screening logic here — see screening/gate.py for that;
this module only shapes what goes over the wire.
"""
from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field


class TriggerResponse(BaseModel):
    # Mirrors gate.list_triggers' own dict shape (a straight
    # SELECT ... ORDER BY display_order) field-for-field.
    #
    # No TypeScript counterpart: the screening gate is PrivacyCare's own
    # (spec D-W2-7), and nothing in the shipped admin UI has a screen for
    # it — see test_response_model_ts_parity.py's ALLOWLIST entry for this
    # class.
    id: str
    trigger_key: str
    label: str
    description: str
    display_order: int


class TriggerListResponse(BaseModel):
    # A plain envelope, not fastapi_pagination.Page: the trigger set is
    # Carol's fixed six-question list (gate.py's module docstring), never a
    # growing register — same shape grounds.py's ProcessingGroundListResponse
    # takes for its own small, fixed set.
    triggers: List[TriggerResponse]


class ScreeningDecisionRequest(BaseModel):
    # NO dpia_required field, deliberately. gate.record_decision derives
    # dpia_required from whether triggered_keys is non-empty and does not
    # accept it as an argument — offering it here would let a caller tick
    # three triggers and also declare no DPIA needed, exactly the
    # invariant record_decision's own docstring says its interface
    # prevents. triggered_keys/justification are validated inside
    # record_decision (known trigger keys, justification required exactly
    # when screening out) — not re-checked here, same discipline
    # risk_schemas.py's RiskCreate applies to category/likelihood/severity.
    triggered_keys: List[str] = Field(default_factory=list)
    justification: Optional[str] = None


class ScreeningVerdictResponse(BaseModel):
    # Mirrors gate.ScreeningVerdict field-for-field, with ONE deliberate
    # omission: the dataclass carries no `id` (Task 2's own finding, carried
    # forward here rather than patched around — see this task's report for
    # the full note). A caller who needs to refer back to a specific
    # decision has no id to do it with; nothing in this response invents
    # one.
    #
    # No TypeScript counterpart: same reasoning as TriggerResponse above.
    declaration_id: str
    dpia_required: bool
    triggered_keys: List[str]
    justification: Optional[str]
    decided_by: str
    decided_at: datetime


class CurrentScreeningResponse(BaseModel):
    # THE DISTINCTION THIS TASK EXISTS TO PRESERVE. verdict is None for a
    # declaration that has never been screened — a legitimate 200, not a
    # 404 and not a fabricated screen-out — while an unknown declaration_id
    # never reaches this model at all (api/screening.py's own existence
    # check raises 404 first). declaration_id is echoed back even when
    # verdict is None so the body still names the resource it answered
    # about rather than being an ambiguous empty response.
    #
    # No TypeScript counterpart: same reasoning as TriggerResponse above.
    declaration_id: str
    verdict: Optional[ScreeningVerdictResponse]


class ScreeningHistoryResponse(BaseModel):
    # Every screening decision ever recorded for this declaration, newest
    # first (gate.decision_history's own ordering) — empty, not 404, for a
    # declaration that exists but has never been screened.
    #
    # No TypeScript counterpart: same reasoning as TriggerResponse above.
    declaration_id: str
    decisions: List[ScreeningVerdictResponse]
