"""W6 classification and W1's questionnaire both call a model over personal
data. This client is the only sanctioned route, and it must be impossible to
use it without the gateway."""
import pytest
import respx
import httpx

from fides.api.privacycare.llm import complete, GatewayUnavailable, GATEWAY_URL


@respx.mock
def test_complete_posts_through_the_gateway():
    route = respx.post(f"{GATEWAY_URL}/complete").mock(
        return_value=httpx.Response(200, json={"content": "redacted answer"})
    )
    out = complete(caller="dpia-questionnaire", messages=[{"role": "user", "content": "hi"}])
    assert out == "redacted answer"
    assert route.called
    body = route.calls[0].request.content.decode()
    assert '"tenant_id"' in body and '"caller"' in body and '"request_id"' in body


@respx.mock
def test_gateway_failure_raises_rather_than_falling_back():
    respx.post(f"{GATEWAY_URL}/complete").mock(return_value=httpx.Response(503))
    with pytest.raises(GatewayUnavailable):
        complete(caller="dpia-questionnaire", messages=[{"role": "user", "content": "hi"}])


@respx.mock
def test_blocked_by_dlp_raises():
    """451 means the gateway refused on data-loss grounds. That is a correct
    outcome, not a transport error, and must not be retried blindly."""
    respx.post(f"{GATEWAY_URL}/complete").mock(return_value=httpx.Response(451))
    with pytest.raises(GatewayUnavailable) as exc:
        complete(caller="dpia-questionnaire", messages=[{"role": "user", "content": "hi"}])
    assert "451" in str(exc.value)


def test_module_never_imports_an_llm_sdk():
    """The structural guarantee: this package cannot reach a model directly."""
    import pathlib
    src = pathlib.Path(__file__).parents[2] / "src" / "fides" / "api" / "privacycare"
    for path in src.rglob("*.py"):
        text = path.read_text()
        assert "import anthropic" not in text, f"{path} imports an LLM SDK directly"
        assert "api.anthropic.com" not in text, f"{path} addresses a model host directly"
