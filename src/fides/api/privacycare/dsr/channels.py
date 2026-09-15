# Delivery only: who gets told an alert is due, and how the message leaves
# the process. Whether an alert is due at all is decided elsewhere (see
# dsr/alerts.py, which this module does not import); this module owns no
# database access. Context worth knowing even though this file never touches
# it: the caller (the scheduler task in plan 15's Task 3) is expected to
# record the ledger row only after send() returns normally, because a
# swallowed failure here would let that ledger's unique constraint suppress
# every future retry — which is exactly why send() must raise rather than
# absorb a failure, including a transport failure (see TeamsWebhookChannel
# below).
#
# Teams first; WhatsApp drops in later against the same AlertChannel
# protocol once Meta credentials exist (spec D-AL-1). We do not reach for
# Fides' own `messagingconfig` machinery: it ships mailgun/twilio/ses/
# mailchimp, no Teams and no WhatsApp, and the table holds zero configured
# rows in this deployment — adopting it would mean extending an
# Ethyca-authored enum for a provider it doesn't support and still only
# reaching email. A Teams incoming webhook is just a URL read from the
# environment.
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, List, Optional, Protocol

import requests

logger = logging.getLogger(__name__)

_ENV_CHANNEL = "PRIVACYCARE_ALERT_CHANNEL"
_ENV_TEAMS_WEBHOOK = "PRIVACYCARE_ALERT_TEAMS_WEBHOOK"

_TEAMS = "teams"
_LOGGING = "logging"
_NULL = "null"


@dataclass(frozen=True)
class Alert:
    """What a human needs to act, and nothing a channel would need to look
    up itself. `recipient` is the already-resolved destination (an email,
    a Teams user, whatever the future WhatsApp number is) — resolving who
    that is is not this module's job, only delivering to them is.
    `TeamsWebhookChannel` does not read it: a Teams incoming webhook is
    scoped to a channel, not addressed to a person, so there is nothing for
    it to do with a recipient. It stays on the dataclass because
    `LoggingChannel` reports it and a future user-addressable channel
    (WhatsApp) will need it."""

    dsr_request_id: str
    right: str
    kind: str
    deadline_at: Optional[datetime]
    days_left: Optional[int]
    owner_email: Optional[str]
    recipient: Optional[str]


class AlertChannel(Protocol):
    name: str

    def send(self, alert: Alert) -> None:
        """Raises on any failure to deliver — including a transport failure,
        not only a rejected request — so that a caller which only records
        the alert as sent on normal return never mistakes "could not even
        reach the endpoint" for delivery."""
        ...


class LoggingChannel:
    """Default channel: no external delivery configured, so alerts land in
    the log and in an in-process list a test (or an operator inspecting a
    running process) can read back. Never raises — there is no transport to
    fail."""

    name = _LOGGING

    def __init__(self) -> None:
        self.sent: List[Alert] = []

    def send(self, alert: Alert) -> None:
        self.sent.append(alert)
        logger.info(
            "privacycare DSR alert (logging channel): request=%s right=%s "
            "kind=%s days_left=%s recipient=%s",
            alert.dsr_request_id,
            alert.right,
            alert.kind,
            alert.days_left,
            alert.recipient,
        )


class NullChannel:
    """Deliberately sends nothing and never raises — for tests and any
    environment that wants the alerting machinery to run without any
    delivery at all, configured rather than accidental."""

    name = _NULL

    def send(self, alert: Alert) -> None:
        return None


def _teams_card(alert: Alert) -> dict:
    """A minimal MessageCard body. The webhook URL carries the
    authorization; the payload itself holds no secret, so nothing here
    needs redacting."""
    deadline = alert.deadline_at.isoformat() if alert.deadline_at else "unknown"
    return {
        "text": (
            f"DSR alert ({alert.kind}): {alert.right} request "
            f"{alert.dsr_request_id} — {alert.days_left} day(s) left, "
            f"deadline {deadline}. Owner: {alert.owner_email or 'unassigned'}."
        )
    }


class TeamsWebhookChannel:
    """Posts one message to a Teams incoming webhook. `post` defaults to
    `requests.post` but is always injected through the constructor — the
    only way the test suite proves this channel's behaviour (a non-2xx
    response, a transport exception, a well-formed body) without ever
    reaching the network.

    The webhook URL is a bearer credential: anyone holding it can post into
    the customer's Teams channel, so it must never reach a log line or an
    exception message. The non-2xx path only ever interpolates the response
    status code. The transport-failure path needs its own guard: `requests`
    characteristically embeds the request URL — webhook token included — in
    the string form of a connection error (`HTTPSConnectionPool(...):
    Max retries exceeded with url: /webhookb2/<guid>/IncomingWebhook/<token>/
    ...`), so `send` catches the raw transport exception and re-raises
    carrying only its type name, never its message and never itself as
    `__cause__` (chaining would put the leaking text straight back into the
    traceback).
    """

    name = _TEAMS

    def __init__(
        self,
        webhook_url: str,
        *,
        post: Optional[Callable[..., requests.Response]] = None,
    ) -> None:
        self._webhook_url = webhook_url
        self._post = post or requests.post

    def send(self, alert: Alert) -> None:
        # A transport exception (timeout, DNS failure, refused connection,
        # TLS error) is caught here rather than left to propagate: with the
        # real transport, `requests`/urllib3 render the target URL — webhook
        # token included — into the exception's own message, so letting it
        # through unchanged would write the credential into whatever catches
        # it upstream (logs, an error tracker, a traceback). Only the
        # exception's type name survives; `from None` stops the original
        # (and its message) from riding along as `__cause__`.
        try:
            response = self._post(self._webhook_url, json=_teams_card(alert))
        except Exception as exc:
            raise RuntimeError(
                f"Teams webhook transport failure: {type(exc).__name__}"
            ) from None

        if response.status_code < 200 or response.status_code >= 300:
            # Named: the status code, never the URL. A caller triaging a
            # delivery failure needs "it was a 500", not the credential.
            raise RuntimeError(
                f"Teams webhook delivery failed with status {response.status_code}"
            )


def channel_from_environment() -> AlertChannel:
    """Reads PRIVACYCARE_ALERT_CHANNEL (teams | logging | null, default
    logging) and, for teams, PRIVACYCARE_ALERT_TEAMS_WEBHOOK. A `teams`
    selection with no webhook URL raises immediately rather than quietly
    handing back a channel that sends nothing — a deployment that believes
    it is alerting and is not is exactly the failure plan 15 exists to
    prevent, so this fails at startup instead of at the first missed
    deadline."""
    selection = os.environ.get(_ENV_CHANNEL, _LOGGING).strip().lower()

    if selection == _TEAMS:
        webhook_url = os.environ.get(_ENV_TEAMS_WEBHOOK)
        if not webhook_url:
            raise ValueError(
                f"{_ENV_CHANNEL}=teams requires {_ENV_TEAMS_WEBHOOK} to be set"
            )
        return TeamsWebhookChannel(webhook_url)

    if selection == _NULL:
        return NullChannel()

    if selection == _LOGGING:
        return LoggingChannel()

    raise ValueError(
        f"{_ENV_CHANNEL}={selection!r} is not recognised: expected one of "
        f"{_TEAMS!r}, {_LOGGING!r}, {_NULL!r}"
    )
