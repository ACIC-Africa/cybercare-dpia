"""The only sanctioned route from PrivacyCare to a language model.

Everything this platform reasons about is personal data, so no caller may
address a model directly. All traffic goes through the GenAI gateway, which
redacts before egress, restores after, and writes an audit row per call.
"""
import os
import uuid

import httpx

GATEWAY_URL = os.environ.get("PRIVACYCARE_GATEWAY_URL", "http://127.0.0.1:8404")
TENANT_ID = os.environ.get("PRIVACYCARE_TENANT_ID", "privacycare")
DEFAULT_MODEL = os.environ.get("PRIVACYCARE_MODEL", "claude-sonnet-5")
TIMEOUT_SECONDS = float(os.environ.get("PRIVACYCARE_GATEWAY_TIMEOUT", "120"))


class GatewayUnavailable(RuntimeError):
    """The gateway did not return a completion.

    Raised for transport failures and for deliberate refusals alike (412
    prompt-injection, 451 response-DLP, 429 rate or budget). Callers must not
    fall back to a direct model call — that is the exact bypass this module exists
    to prevent.
    """


def complete(
    caller: str,
    messages: list[dict],
    *,
    model: str = DEFAULT_MODEL,
    max_tokens: int = 1024,
    system: str | None = None,
) -> str:
    """Send a completion request through the gateway and return the text.

    Args:
        caller: identifies the calling feature in the egress audit, e.g.
            "dpia-questionnaire" or "discovery-classifier".
        messages: Anthropic-shaped message list.
        model: model id; defaults to PRIVACYCARE_MODEL.
        max_tokens: completion cap.
        system: optional system prompt.

    Returns:
        The concatenated `text` fields of the response's content blocks
        whose `type` is `"text"`, in order. Non-text blocks (e.g.
        `tool_use`) are dropped.

    Raises:
        GatewayUnavailable: on any non-200 response or transport error.
    """
    payload = {
        "tenant_id": TENANT_ID,
        "caller": caller,
        "request_id": str(uuid.uuid4()),
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
    }
    if system is not None:
        payload["system"] = system

    try:
        response = httpx.post(
            f"{GATEWAY_URL}/complete", json=payload, timeout=TIMEOUT_SECONDS
        )
    except httpx.HTTPError as exc:
        raise GatewayUnavailable(f"gateway transport error: {exc}") from exc

    if response.status_code != 200:
        raise GatewayUnavailable(
            f"gateway returned {response.status_code}: {response.text[:200]}"
        )

    blocks = response.json()["content"]
    return "".join(
        block["text"] for block in blocks if block.get("type") == "text"
    )
