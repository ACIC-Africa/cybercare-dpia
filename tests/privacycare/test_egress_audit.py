"""Every model call must leave a trace. PrivacyCare is itself a system that
processes personal data, so its own egress log is evidence for its own ROPA.

The probe text below contains a Kenyan KRA PIN (form A001234567Z) so the
redactor actually has something to redact — a probe with no personal data in
it would pass these tests even if redaction were completely broken.

Both tests skip when the gateway is unreachable, for local convenience. Set
PRIVACYCARE_INTEGRATION=1 to turn that skip into a hard failure instead —
use this in CI or whenever a green run must mean the gateway was actually
exercised, not silently bypassed.
"""
import os
import uuid

import pytest
import sqlalchemy

from fides.api.privacycare.llm import GatewayUnavailable, complete

pytestmark = pytest.mark.integration

# Kenya Revenue Authority PIN form: A/P + 9 digits + check letter. Matches
# the `kra_pin` rule in privacycare-genai-gateway/policies/privacycare.yaml.
PROBE_TEXT = "My KRA PIN is A001234567Z. Say OK."


def _gateway_engine():
    return sqlalchemy.create_engine(
        os.environ.get(
            "GATEWAY_DATABASE_URL",
            "postgresql://gateway:gateway@127.0.0.1:5443/privacycare_genai_gateway",
        )
    )


def _egress_count(engine) -> int:
    with engine.connect() as conn:
        return conn.execute(
            sqlalchemy.text("SELECT count(*) FROM gateway_egress_calls")
        ).scalar_one()


def _skip_or_fail(exc: GatewayUnavailable) -> None:
    message = f"gateway not reachable or no API key configured: {exc}"
    if os.environ.get("PRIVACYCARE_INTEGRATION") == "1":
        pytest.fail(message)
    pytest.skip(message)


def test_a_completion_writes_exactly_one_audit_row_with_redactions():
    engine = _gateway_engine()
    before = _egress_count(engine)
    marker = f"audit-probe-{uuid.uuid4().hex[:8]}"
    try:
        complete(caller=marker, messages=[{"role": "user", "content": PROBE_TEXT}])
    except GatewayUnavailable as exc:
        _skip_or_fail(exc)
        return
    assert _egress_count(engine) == before + 1
    with engine.connect() as conn:
        redactions_count = conn.execute(
            sqlalchemy.text(
                "SELECT redactions_count FROM gateway_egress_calls WHERE caller = :c"
            ),
            {"c": marker},
        ).scalar_one()
    assert redactions_count >= 1, (
        "the probe contains a KRA PIN; redactions_count == 0 means "
        "redaction did not actually happen"
    )


def test_the_caller_is_recorded():
    engine = _gateway_engine()
    marker = f"audit-probe-{uuid.uuid4().hex[:8]}"
    try:
        complete(caller=marker, messages=[{"role": "user", "content": PROBE_TEXT}])
    except GatewayUnavailable as exc:
        _skip_or_fail(exc)
        return
    with engine.connect() as conn:
        found = conn.execute(
            sqlalchemy.text("SELECT count(*) FROM gateway_egress_calls WHERE caller = :c"),
            {"c": marker},
        ).scalar_one()
    assert found == 1, "the egress row must attribute the call to its caller"
