"""Envelopes for the DPIA risk register's HTTP surface (spec 2026-09-16
D-W2-2, Task 5). No banding or ODPC logic here — see risk/register.py and
risk/odpc.py for that; this module only shapes what goes over the wire.
"""
from typing import Optional

from pydantic import BaseModel, Field


class RiskCreate(BaseModel):
    # category/likelihood/severity are validated inside register.add_risk
    # (one of Carol's seven categories, likelihood/severity each 1..5) — not
    # re-checked here, same discipline dsr_schemas.py's DsrRequestCreate
    # applies to `right`.
    category: str
    description: str = Field(min_length=1)
    likelihood: int
    severity: int


class RiskResponse(BaseModel):
    # Mirrors register.RiskEntry field-for-field. score and band are
    # computed by register.py through banding.py, never stored (see
    # register.py's own module docstring on why storing either would be a
    # liability) — reading them back here proves the route surfaces the
    # computed value, not a raw column.
    #
    # TypeScript counterpart: risk.types.ts's RiskResponse interface, hand-
    # authored for the admin-UI's risk register screen
    # (RiskRegisterSection.tsx and friends). Field parity is asserted in
    # tests/privacycare/test_risk_ts_parity.py and enforced by
    # test_response_model_ts_parity.py's response-model walk (fix wave,
    # Screen 2 review, finding 2 — this class's ALLOWLIST exemption there
    # was removed once the screen landed; it used to say "nothing in the
    # shipped admin UI has a screen for it", which this screen made false).
    id: str
    assessment_id: str
    category: str
    description: str
    likelihood: int
    severity: int
    score: int
    band: str


class RemoveRiskResponse(BaseModel):
    # api/risk.py's remove_assessment_risk route only ever returns this on
    # an actual removal (register.remove_risk returning False is mapped to
    # 404 by the route, not to removed=False here) — `removed` is always
    # true on a successful response; the field exists so the body still
    # names the resource it acted on rather than being an empty 200.
    #
    # TypeScript counterpart: risk.types.ts's RemoveRiskResponse interface —
    # same fix-wave finding-2 history as RiskResponse above.
    id: str
    removed: bool


class OdpcFindingResponse(BaseModel):
    # Mirrors risk/odpc.py's OdpcFinding field-for-field. highest_risk is
    # RiskResponse rather than a nested id/score pair — the same computed
    # score/band a caller would need to explain why consultation is
    # required lives on RiskResponse already, so there is nothing to gain
    # from a second, narrower shape here.
    #
    # TypeScript counterpart: risk.types.ts's OdpcFindingResponse interface,
    # same reasoning and same fix-wave finding-2 history as RiskResponse
    # above — field parity asserted in test_risk_ts_parity.py.
    required: bool
    band: str
    window_days: int
    reason: str
    highest_risk: Optional[RiskResponse]
