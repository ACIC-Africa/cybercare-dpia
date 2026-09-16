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
    # No TypeScript counterpart: the DPIA risk register is PrivacyCare's
    # own (spec D-W2-2), and nothing in the shipped admin UI has a screen
    # for it — see tests/privacycare/test_response_model_ts_parity.py's
    # ALLOWLIST entry for this class.
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
    id: str
    removed: bool


class OdpcFindingResponse(BaseModel):
    # Mirrors risk/odpc.py's OdpcFinding field-for-field. highest_risk is
    # RiskResponse rather than a nested id/score pair — the same computed
    # score/band a caller would need to explain why consultation is
    # required lives on RiskResponse already, so there is nothing to gain
    # from a second, narrower shape here.
    #
    # No TypeScript counterpart: same reasoning as RiskResponse above — see
    # test_response_model_ts_parity.py's ALLOWLIST entry for this class.
    required: bool
    band: str
    window_days: int
    reason: str
    highest_risk: Optional[RiskResponse]
