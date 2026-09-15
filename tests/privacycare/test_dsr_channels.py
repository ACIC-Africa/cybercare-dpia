"""Delivery. Teams first; WhatsApp drops in against the same protocol when
Meta credentials exist (spec D-AL-1).

No test here performs real network I/O: TeamsWebhookChannel takes an injectable
transport precisely so the suite can prove its behaviour without a webhook.
"""
import logging
import traceback
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
    # It must still raise -- the caller only records the alert as sent on a
    # normal return -- but not as the original OSError: see the leak test
    # below for why the transport exception is caught and replaced rather
    # than let through unchanged.
    def fake_post(url, **kwargs):
        raise OSError("connection refused")

    with pytest.raises(RuntimeError):
        TeamsWebhookChannel(WEBHOOK, post=fake_post).send(_alert())


def test_the_webhook_url_never_appears_in_an_error_message():
    # The URL is a bearer credential: anyone holding it can post into the
    # customer's Teams channel. It must not reach a log or a traceback.
    def fake_post(url, **kwargs):
        return type("R", (), {"status_code": 403, "text": "forbidden"})()

    with pytest.raises(RuntimeError) as caught:
        TeamsWebhookChannel(WEBHOOK, post=fake_post).send(_alert())
    assert "super-secret-token" not in str(caught.value)


def test_the_webhook_url_never_appears_in_a_transport_exception_message():
    # requests/urllib3 characteristically embed the request URL -- webhook
    # token included -- in a connection error's own message (e.g.
    # "HTTPSConnectionPool(...): Max retries exceeded with url:
    # /webhookb2/<guid>/IncomingWebhook/<token>/..."). Simulate that shape
    # directly: the fake transport's exception message itself carries the
    # fixture token, and send() must not let it escape verbatim.
    def fake_post(url, **kwargs):
        raise ConnectionError(f"Max retries exceeded with url: {url}")

    with pytest.raises(RuntimeError) as caught:
        TeamsWebhookChannel(WEBHOOK, post=fake_post).send(_alert())
    assert "super-secret-token" not in str(caught.value)
    assert "super-secret-token" not in repr(caught.value)
    # `__cause__` is what a chained `raise ... from <exc>` would surface in
    # a rendered traceback; `from None` (the fix) keeps it unset so the
    # original ConnectionError's message never rides along into whatever
    # renders this exception (logger.exception, an error tracker, a bare
    # traceback.format_exception).
    assert caught.value.__cause__ is None
    rendered = "".join(
        traceback.format_exception(
            type(caught.value), caught.value, caught.value.__traceback__
        )
    )
    assert "super-secret-token" not in rendered


def test_urllib3_request_logging_never_prints_the_webhook_token(caplog):
    # C2, final review of plan 15. The docstring's other half: even a
    # SUCCESSFUL send must not leak the token, because it is the real
    # transport (`requests`/urllib3), not this module, that would print it
    # -- at DEBUG, via `urllib3.connectionpool.HTTPConnectionPool.
    # _make_request`'s own `log.debug('%s://%s:%s "%s %s %s" %s %s', ...,
    # url, ...)` call (connectionpool.py, around line 545), where `url` is
    # the request PATH -- webhook token included. Fides runs
    # `InterceptHandler` on the root logger at DEBUG in this deployment
    # (FIDES__DEV_MODE=True), so that call alone is the whole exposure.
    #
    # No real network I/O: the fake transport below does not open a socket,
    # but it DOES call the real "urllib3.connectionpool" logger the same
    # way urllib3's own code does, with the fixture token embedded in the
    # message the same way a real request would embed it in the URL --
    # standing in for the real transport without needing one, per the
    # "drive a fake transport that makes the real urllib3 log call"
    # instruction this test exists to satisfy.
    urllib3_logger = logging.getLogger("urllib3.connectionpool")

    def fake_post_that_logs_like_the_real_transport(url, **kwargs):
        urllib3_logger.debug(
            '%s://%s:%s "%s %s %s" %s %s',
            "https",
            "example.invalid",
            443,
            "POST",
            url,
            "HTTP/1.1",
            200,
            0,
        )
        return type("R", (), {"status_code": 200, "text": "1"})()

    with caplog.at_level(logging.DEBUG, logger="urllib3.connectionpool"):
        TeamsWebhookChannel(
            WEBHOOK, post=fake_post_that_logs_like_the_real_transport
        ).send(_alert())

    for record in caplog.records:
        assert "super-secret-token" not in record.getMessage(), (
            f"the webhook token appeared in a captured log record: "
            f"{record.getMessage()!r}"
        )


def test_urllib3_logging_is_restored_to_its_previous_level_after_send():
    # The suppression must be temporary and must restore whatever level the
    # logger had before -- not hard-code a level -- so an operator's own
    # explicit configuration for this logger survives a send that happens
    # to run while they're debugging something unrelated.
    urllib3_logger = logging.getLogger("urllib3.connectionpool")
    urllib3_logger.setLevel(logging.INFO)
    try:

        def fake_post(url, **kwargs):
            # Mid-send, the level must be suppressed (raised to WARNING or
            # above), not left at whatever it was before this call.
            assert urllib3_logger.level >= logging.WARNING
            return type("R", (), {"status_code": 200, "text": "1"})()

        TeamsWebhookChannel(WEBHOOK, post=fake_post).send(_alert())
        assert urllib3_logger.level == logging.INFO
    finally:
        urllib3_logger.setLevel(logging.NOTSET)


def test_a_serialisation_bug_is_not_reported_as_a_transport_failure():
    # M6. `_teams_card` sat inside the transport `try` before this fix, so a
    # bug in it (unserialisable field, whatever) would be caught by the
    # same `except Exception` as a real transport failure and re-raised as
    # "Teams webhook transport failure: <type>" -- sending whoever triages
    # it looking at the network instead of the payload. Force such a bug by
    # handing `send` an Alert with a `days_left` that breaks the card's own
    # f-string interpolation is hard to arrange without breaking the
    # dataclass's typing, so instead this monkeypatches `_teams_card`
    # itself to raise, and asserts the ORIGINAL exception type propagates
    # unwrapped -- proof the transport `try` no longer covers it.
    import fides.api.privacycare.dsr.channels as channels_module

    original_teams_card = channels_module._teams_card

    def _boom(alert):
        raise TypeError("not a card")

    channels_module._teams_card = _boom
    try:
        with pytest.raises(TypeError, match="not a card"):
            TeamsWebhookChannel(WEBHOOK, post=lambda url, **kw: None).send(_alert())
    finally:
        channels_module._teams_card = original_teams_card


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
