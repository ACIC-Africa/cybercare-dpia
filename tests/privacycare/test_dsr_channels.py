"""Delivery. Teams first; WhatsApp drops in against the same protocol when
Meta credentials exist (spec D-AL-1).

No test here performs real network I/O: TeamsWebhookChannel takes an injectable
transport precisely so the suite can prove its behaviour without a webhook.
"""
from datetime import datetime, timezone

import pytest

from fides.api.privacycare.dsr.channels import (
    Alert,
    LoggingChannel,
    NullChannel,
    TeamsWebhookChannel,
    channel_from_environment,
)

WEBHOOK = "https://example.invalid/webhook/super-secret-token"


def _alert(**kwargs) -> Alert:
    return Alert(
        dsr_request_id=kwargs.get("dsr_request_id", "dsr_1"),
        right=kwargs.get("right", "access"),
        kind=kwargs.get("kind", "approaching"),
        deadline_at=kwargs.get("deadline_at", datetime(2026, 9, 22, tzinfo=timezone.utc)),
        days_left=kwargs.get("days_left", 3),
        owner_email=kwargs.get("owner_email", "ops@customer.co.ke"),
        recipient=kwargs.get("recipient", "ops@customer.co.ke"),
    )


def test_the_teams_channel_posts_once_to_the_configured_url():
    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return type("R", (), {"status_code": 200, "text": "1"})()

    TeamsWebhookChannel(WEBHOOK, post=fake_post).send(_alert())

    assert len(calls) == 1
    url, kwargs = calls[0]
    assert url == WEBHOOK
    body = str(kwargs.get("json"))
    assert "access" in body and "3" in body


def test_a_non_2xx_response_raises_so_the_alert_is_not_recorded_as_sent():
    # D-AL-7: an undelivered alert must NOT be written to the ledger, or the
    # unique constraint would suppress the retry forever.
    def fake_post(url, **kwargs):
        return type("R", (), {"status_code": 500, "text": "upstream boom"})()

    with pytest.raises(RuntimeError):
        TeamsWebhookChannel(WEBHOOK, post=fake_post).send(_alert())


def test_a_transport_failure_propagates():
    def fake_post(url, **kwargs):
        raise OSError("connection refused")

    with pytest.raises(OSError):
        TeamsWebhookChannel(WEBHOOK, post=fake_post).send(_alert())


def test_the_webhook_url_never_appears_in_an_error_message():
    # The URL is a bearer credential: anyone holding it can post into the
    # customer's Teams channel. It must not reach a log or a traceback.
    def fake_post(url, **kwargs):
        return type("R", (), {"status_code": 403, "text": "forbidden"})()

    with pytest.raises(RuntimeError) as caught:
        TeamsWebhookChannel(WEBHOOK, post=fake_post).send(_alert())
    assert "super-secret-token" not in str(caught.value)


def test_the_null_channel_sends_nothing_and_the_logging_channel_records():
    NullChannel().send(_alert())            # must not raise
    logging_channel = LoggingChannel()
    logging_channel.send(_alert())
    assert logging_channel.sent[-1].right == "access"


def test_the_default_channel_is_logging(monkeypatch):
    monkeypatch.delenv("PRIVACYCARE_ALERT_CHANNEL", raising=False)
    assert channel_from_environment().name == "logging"


def test_selecting_teams_without_a_webhook_is_a_configuration_error(monkeypatch):
    # A deployment that believes it is alerting and is not is exactly the
    # failure this plan exists to prevent. Fail at startup, loudly.
    monkeypatch.setenv("PRIVACYCARE_ALERT_CHANNEL", "teams")
    monkeypatch.delenv("PRIVACYCARE_ALERT_TEAMS_WEBHOOK", raising=False)
    with pytest.raises(ValueError, match="PRIVACYCARE_ALERT_TEAMS_WEBHOOK"):
        channel_from_environment()
