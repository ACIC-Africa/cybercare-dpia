# Fix wave (Screen 2 review), finding 2. test_response_model_ts_parity.py's
# ALLOWLIST used to exempt RiskResponse, RemoveRiskResponse and
# OdpcFindingResponse with the reason "nothing in the shipped admin UI has a
# screen for it" — true when that plan landed, false now that the risk
# register screen exists (RiskRegisterSection.tsx, AddRiskModal.tsx,
# RemoveRiskModal.tsx all consume risk.types.ts, the hand-authored TS twin of
# risk_schemas.py). Removing the exemption without a real parity test would
# only replace one silent gap with another — the walk would fail loudly on a
# missing interface, but nothing would ever check that interface's FIELDS
# still match once it exists. This file is that check, following the same
# regex-parse-the-shipped-contract discipline test_api_schemas.py already
# applies to every other hand-authored feature interface.
import pathlib
import re

from fides.api.privacycare.api.risk_schemas import (
    OdpcFindingResponse,
    RemoveRiskResponse,
    RiskResponse,
)

RISK_TS_PATH = (
    pathlib.Path(__file__).parents[2]
    / "clients/admin-ui/src/features/privacy-assessments/risk.types.ts"
)


def _interface_field_specs(name: str) -> dict[str, bool]:
    # Same idea as test_api_schemas.py's _feature_interface_field_specs, but
    # against risk.types.ts rather than types.ts — kept as its own small
    # copy rather than imported cross-file, the same way
    # test_response_model_ts_parity.py's own helpers are not shared with
    # test_api_schemas.py's.
    text = RISK_TS_PATH.read_text()
    match = re.search(rf"export interface {name}\b[^{{]*\{{(.*?)\n\}}", text, re.S)
    assert match, f"{name} not found in {RISK_TS_PATH}"
    body = match.group(1)
    return {
        field: bool(optional_marker)
        for field, optional_marker in re.findall(r"^\s*([a-z_]+)(\??):", body, re.M)
    }


def _interface_fields(name: str) -> set[str]:
    return set(_interface_field_specs(name).keys())


def _assert_matches_the_shipped_contract(model, ts_name: str) -> None:
    specs = _interface_field_specs(ts_name)
    assert set(model.model_fields) == set(specs), (
        f"{model.__name__} fields do not match risk.types.ts's {ts_name!r} "
        f"interface: model={sorted(model.model_fields)} ts={sorted(specs)}"
    )
    for field, is_optional in specs.items():
        pydantic_required = model.model_fields[field].is_required()
        assert pydantic_required == (not is_optional), (
            f"{model.__name__}.{field}: TS optional={is_optional} but "
            f"Pydantic required={pydantic_required}"
        )


def test_risk_response_matches_the_shipped_contract():
    _assert_matches_the_shipped_contract(RiskResponse, "RiskResponse")


def test_remove_risk_response_matches_the_shipped_contract():
    _assert_matches_the_shipped_contract(RemoveRiskResponse, "RemoveRiskResponse")


def test_odpc_finding_response_matches_the_shipped_contract():
    _assert_matches_the_shipped_contract(OdpcFindingResponse, "OdpcFindingResponse")


def test_the_risk_ts_file_was_actually_read():
    # Guard: a bad path or a regex that silently matched nothing would make
    # every assertion above compare empty sets and pass vacuously — the
    # exact failure mode this whole fix wave item exists to close.
    assert len(_interface_fields("RiskResponse")) == 8
    assert len(_interface_fields("RemoveRiskResponse")) == 2
    assert len(_interface_fields("OdpcFindingResponse")) == 5
