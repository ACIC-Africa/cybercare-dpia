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
    # UPDATE (plan 20, Task 3): declaration_id -> business_process_id.
    # Screening is keyed to the customer's business process (86 of them),
    # not a processing activity (2 of them, both invented) — see
    # api/screening.py's module docstring for the full re-key rationale.
    #
    # No TypeScript counterpart: same reasoning as TriggerResponse above.
    business_process_id: str
    dpia_required: bool
    triggered_keys: List[str]
    justification: Optional[str]
    decided_by: str
    decided_at: datetime


class CurrentScreeningResponse(BaseModel):
    # THE DISTINCTION THIS TASK EXISTS TO PRESERVE. verdict is None for a
    # business process that has never been screened — a legitimate 200,
    # not a 404 and not a fabricated screen-out — while an unknown
    # business_process_id never reaches this model at all
    # (api/screening.py's own existence check raises 404 first).
    # business_process_id is echoed back even when verdict is None so the
    # body still names the resource it answered about rather than being an
    # ambiguous empty response.
    #
    # No TypeScript counterpart: same reasoning as TriggerResponse above.
    business_process_id: str
    verdict: Optional[ScreeningVerdictResponse]


class ScreeningHistoryResponse(BaseModel):
    # Every screening decision ever recorded for this business process,
    # newest first (gate.decision_history's own ordering) — empty, not
    # 404, for a business process that exists but has never been screened.
    #
    # No TypeScript counterpart: same reasoning as TriggerResponse above.
    business_process_id: str
    decisions: List[ScreeningVerdictResponse]


class ScreeningStatusResponse(BaseModel):
    # One row of GET /api/v1/privacycare/screening (plan 20, Task 3) — the
    # list Carol works through in a single session, not one business
    # process's detail. dpia_required, decided_by and decided_at are None
    # together, always: a business process that has never been screened
    # has no verdict, no decider and no timestamp, never any other
    # combination (mirrors CurrentScreeningResponse.verdict's own
    # None-means-unscreened contract, flattened onto one row instead of a
    # nested object because this response exists to be scanned across
    # 86+ rows at once, not read one at a time).
    #
    # has_mapping is true only when this business process links (through
    # privacycare_process_declaration) to a privacy_declaration_id that
    # ACTUALLY RESOLVES to a live privacydeclaration row — an orphan link
    # (a declaration deleted after the link was made; one exists in this
    # customer's live data) must not read as "mapped", and must not raise
    # trying to find out. See api/screening.py's _LIST_SCREENING_STATUS_SQL
    # for the join that makes that true.
    #
    # No TypeScript counterpart: same reasoning as TriggerResponse above.
    business_process_id: str
    name: str
    business_cycle: Optional[str]
    dpia_required: Optional[bool]
    decided_by: Optional[str]
    decided_at: Optional[datetime]
    has_mapping: bool


class DataMappingRequest(BaseModel):
    # POST .../mapping (plan 20, Task 4). Six fields from her own map, plus
    # the activity's own name — see screening/mapping.py's module docstring
    # for the full reasoning behind every one of these.
    #
    # name/data_categories are the only two that are always required: "a
    # name and at least one data category" is the brief's own minimum for a
    # valid activity. Every other field is Optional[...] = None, and None
    # means "not answered this call" — mapping.save_mapping's UPDATE path
    # leaves a column exactly as it was when the matching field here is
    # None, so a privacy officer who saves three answers today and the rest
    # next week never has today's answers erased by tomorrow's partial
    # resubmission. An explicit empty list for data_subjects (as opposed to
    # omitting the field, which defaults to None) DOES clear it — the one
    # place an empty value and a missing one mean different things here.
    #
    # ground is free text matching privacycare_processing_ground.ground
    # VERBATIM (e.g. "KYC Requirements") — her business situation, not the
    # legal basis. save_mapping derives fides_legal_basis from it and
    # writes THAT; there is no field here a caller could use to set the
    # legal basis directly, which is the whole point (screening/mapping.py:
    # "the lawful basis is derived, never accepted").
    #
    # No TypeScript counterpart yet: Screen 1's mapping modal
    # (docs/design/privacycare-screens/DESIGN.md, "Step 2 — the prompted
    # mapping") is built from this route in a later plan, per that design's
    # own "Not in this plan" list. Allowlisted in
    # test_response_model_ts_parity.py with that reason, same shape as
    # TriggerResponse's own allowlist entry in screening_schemas.py above.
    name: str = Field(min_length=1)
    data_categories: List[str] = Field(min_length=1)
    data_subjects: Optional[List[str]] = None
    ground: Optional[str] = None
    purpose: Optional[str] = None
    retention_period: Optional[str] = None
    third_parties: Optional[str] = None


class DataMappingResponse(BaseModel):
    # Mirrors screening/mapping.MappingResult field-for-field. `ground` here
    # echoes what THIS call was given (None if this call did not name one,
    # even when an earlier call already derived and persisted a legal
    # basis) — the ground's own text is not a stored column anywhere on
    # privacydeclaration, only its DERIVED fides_legal_basis is, and
    # fides_legal_basis always reflects the current persisted value
    # regardless of what this particular call supplied. See
    # screening/mapping.py's save_mapping docstring.
    #
    # `created` distinguishes "this call made the activity" from "this call
    # updated the one the route already owns for this process" — the
    # idempotency this task's ruling required (screening/mapping.py's own
    # module docstring: keyed to the activity this route created, never to
    # the process).
    #
    # No TypeScript counterpart: same reasoning as DataMappingRequest above.
    business_process_id: str
    privacy_declaration_id: str
    system_id: str
    name: str
    data_subjects: List[str]
    data_categories: List[str]
    ground: Optional[str]
    fides_legal_basis: Optional[str]
    purpose: Optional[str]
    retention_period: Optional[str]
    third_parties: Optional[str]
    processes_special_category_data: bool
    created: bool


class ScreeningListResponse(BaseModel):
    # A plain envelope, not fastapi_pagination.Page (contrast
    # processes.py's list_business_processes) — the whole point of this
    # route is fetching every business process's screening status in ONE
    # call so the screen can filter by business cycle client-side; a
    # paginated response would just move the "86 calls" problem into the
    # browser instead of removing it.
    #
    # No TypeScript counterpart: same reasoning as TriggerResponse above.
    processes: List[ScreeningStatusResponse]
