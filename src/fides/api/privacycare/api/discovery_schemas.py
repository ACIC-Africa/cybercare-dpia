"""Envelopes for the discovery-findings HTTP surface (2026-09-18
discovery-findings-API brief). No reconciliation logic here — see
discovery/findings.py for that; this module only shapes what goes over the
wire.
"""
from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field


class FindingResponse(BaseModel):
    # One discovered TABLE (never a Field — see findings.py's module
    # docstring for why). `urn` is this finding's own identifier: there is
    # no separate row id to give a caller, because a finding IS a
    # stagedresource row, identified the same way every other route in
    # this codebase that touches one (api/monitors.py's deletion-impact
    # route, reconcile.py itself) already does.
    #
    # state is one of "mapped" / "ignored" / "needs_review" — the third
    # is never stored, only ever the absence of a reconciliation row (see
    # findings.py's _state_for). system_id/system_name/reason/decided_by/
    # decided_at are all None together for a "needs_review" finding —
    # same "None means never decided" contract
    # ScreeningStatusResponse.dpia_required keeps for an unscreened
    # business process.
    #
    # No TypeScript counterpart: this surface has no shipped admin-UI
    # screen calling it yet (this task builds the API half only — see
    # test_response_model_ts_parity.py's PRIVACYCARE_PATH_PREFIXES, which
    # this router's prefix is deliberately not among).
    urn: str
    table_name: str
    schema_name: str
    monitor_key: str
    field_count: int
    table_type: Optional[str]  # "view" | "materialized_view" | None
    diff_status: Optional[str]
    state: str
    system_id: Optional[str]
    system_name: Optional[str]
    reason: Optional[str]
    decided_by: Optional[str]
    decided_at: Optional[datetime]


class FindingListResponse(BaseModel):
    # A plain envelope, not fastapi_pagination.Page: today's single
    # connection stages 176 tables, a working session's worth to review at
    # once — same shape api/screening.py's ScreeningListResponse takes for
    # its own 86+ row list, and for the same reason (the list route exists
    # precisely so a caller does not paginate through per-row fetches).
    findings: List[FindingResponse]


class ReconcileFindingRequest(BaseModel):
    # state/system_id/reason are validated inside
    # discovery.findings.reconcile_finding (mapped requires system_id and
    # forbids reason; ignored requires a non-blank reason and forbids
    # system_id) — not re-checked here, same discipline risk_schemas.py's
    # RiskCreate and screening_schemas.py's ScreeningDecisionRequest apply
    # to their own core-validated fields.
    #
    # TWO WAYS TO NAME THE SYSTEM (2026-09-18 fix). `system_id` is
    # ctl_systems.id, the internal primary key discovery.findings.
    # _system_exists has always validated against — but NO read route in
    # this codebase (not Ethyca's own System endpoints, not SystemSelect,
    # not PrivacyCare's own ROPA/screening surfaces) ever puts that id on
    # the wire; every other place a system is identified here uses
    # `fides_key` (see discoverySystemCandidates.ts's retired "REAL GAP"
    # docstring for the fuller history — the picker this field once
    # starved is now built on system_fides_key). `system_fides_key` is
    # resolved to the internal id server-side by
    # api/discovery.py's _resolve_system_id, which rejects an unknown key
    # by name (ValueError -> 400), the same way
    # screening/mapping.py rejects an unknown data category. Give at most
    # one; a request naming a system gives system_fides_key, never both.
    #
    # `system_id` stays accepted — not removed — purely for backward
    # compatibility with existing internal callers
    # (tests/privacycare/test_discovery_findings.py,
    # test_api_discovery.py) that already construct a request with the
    # real internal id directly; nothing on the wire has ever depended on
    # it, so a new caller should always send system_fides_key.
    state: str
    system_id: Optional[str] = None
    system_fides_key: Optional[str] = None
    reason: Optional[str] = Field(default=None)


class ReconciliationResponse(BaseModel):
    # Mirrors discovery.findings.Reconciliation field-for-field. Never
    # state="needs_review" — that value is only ever the absence of a row,
    # and this model always describes a row that was actually written.
    id: str
    urn: str
    state: str
    system_id: Optional[str]
    system_name: Optional[str]
    reason: Optional[str]
    decided_by: str
    decided_at: datetime


class FindingHistoryResponse(BaseModel):
    # Every reconciliation ever recorded for one urn, newest first — empty
    # for a real, currently-discovered table that has never been
    # reconciled (needs_review), same "empty list is a legitimate answer"
    # contract ScreeningHistoryResponse already keeps for a business
    # process nobody has screened yet.
    urn: str
    reconciliations: List[ReconciliationResponse]
