"""Every model call must leave a trace. PrivacyCare is itself a system that
processes personal data, so its own egress log is evidence for its own ROPA."""
import os
import uuid
import pytest
import sqlalchemy

from fides.api.privacycare.llm import complete, GatewayUnavailable

pytestmark = pytest.mark.integration


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


def test_a_completion_writes_exactly_one_audit_row():
    engine = _gateway_engine()
    before = _egress_count(engine)
    marker = f"audit-probe-{uuid.uuid4().hex[:8]}"
    try:
        complete(caller=marker, messages=[{"role": "user", "content": "Say OK."}])
    except GatewayUnavailable as exc:
        pytest.skip(f"gateway not reachable or no API key configured: {exc}")
    assert _egress_count(engine) == before + 1


def test_the_caller_is_recorded():
    engine = _gateway_engine()
    marker = f"audit-probe-{uuid.uuid4().hex[:8]}"
    try:
        complete(caller=marker, messages=[{"role": "user", "content": "Say OK."}])
    except GatewayUnavailable as exc:
        pytest.skip(f"gateway not reachable or no API key configured: {exc}")
    with engine.connect() as conn:
        found = conn.execute(
            sqlalchemy.text("SELECT count(*) FROM gateway_egress_calls WHERE caller = :c"),
            {"c": marker},
        ).scalar_one()
    assert found == 1, "the egress row must attribute the call to its caller"
