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
        return_value=httpx.Response(
            200,
            json={
                "request_id": "r1",
                "model": "claude-sonnet-5",
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": "redacted answer"}],
                "usage": {"input_tokens": 10, "output_tokens": 2},
            },
        )
    )
    out = complete(caller="dpia-questionnaire", messages=[{"role": "user", "content": "hi"}])
    assert out == "redacted answer"
    assert route.called
    body = route.calls[0].request.content.decode()
    assert '"tenant_id"' in body and '"caller"' in body and '"request_id"' in body


@respx.mock
def test_complete_concatenates_text_blocks_and_ignores_tool_use():
    """The gateway's CompleteResponse.content is a list of Anthropic content
    blocks, not a bare string. Multiple text blocks must concatenate in
    order, and non-text blocks (e.g. tool_use) must be ignored rather than
    crashing."""
    respx.post(f"{GATEWAY_URL}/complete").mock(
        return_value=httpx.Response(
            200,
            json={
                "request_id": "r2",
                "model": "claude-sonnet-5",
                "stop_reason": "end_turn",
                "content": [
                    {"type": "text", "text": "redacted "},
                    {"type": "tool_use", "id": "t1", "name": "lookup", "input": {}},
                    {"type": "text", "text": "answer"},
                ],
                "usage": {"input_tokens": 10, "output_tokens": 2},
            },
        )
    )
    out = complete(caller="dpia-questionnaire", messages=[{"role": "user", "content": "hi"}])
    assert out == "redacted answer"


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


FORBIDDEN_IMPORTS = (
    "import anthropic",
    "from anthropic",
    "import openai",
    "from openai",
    "google.generativeai",
    "import cohere",
    "import mistralai",
    "bedrock-runtime",
)

FORBIDDEN_HOSTS = (
    "api.anthropic.com",
    "api.openai.com",
    "generativelanguage.googleapis.com",
    "bedrock-runtime",
)


def test_module_never_imports_an_llm_sdk():
    """The structural guarantee: this package cannot reach a model directly.

    This is a substring scan, not a static-analysis tool, so it is honest
    about what it does NOT catch: a host string assembled from fragments
    (e.g. "api." + "anthropic" + ".com"), a dynamic
    ``importlib.import_module("anthropic")`` call, or any provider/host not
    listed in FORBIDDEN_IMPORTS / FORBIDDEN_HOSTS above. It is a tripwire for
    the obvious bypass, not a proof that no bypass exists.
    """
    import pathlib

    src = pathlib.Path(__file__).parents[2] / "src" / "fides" / "api" / "privacycare"
    assert src.is_dir(), (
        f"{src} does not exist — if the package moved, this guard is "
        "scanning nothing and passing vacuously; update the path"
    )
    scanned = 0
    for path in src.rglob("*.py"):
        scanned += 1
        text = path.read_text()
        for token in FORBIDDEN_IMPORTS:
            assert token not in text, f"{path} imports an LLM SDK directly (matched {token!r})"
        for token in FORBIDDEN_HOSTS:
            assert token not in text, f"{path} addresses a model host directly (matched {token!r})"
    assert scanned > 0, f"{src} contains no .py files — the guard scanned nothing"
