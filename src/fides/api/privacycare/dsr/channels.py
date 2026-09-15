# Delivery only: who gets told an alert is due, and how the message leaves
# the process. Nothing here decides *whether* an alert is due (dsr/alerts.py
# owns that) and nothing here talks to the database (the caller in the
# scheduler task owns the ledger write, and only after send() returns
# normally — see the module docstring in dsr/alerts.py on why a swallowed
# failure would silently mute the channel forever).
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
    that is is not this module's job, only delivering to them is."""

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
        """Raises on any failure to deliver. The caller (the scheduler task
        in plan 15's Task 3) records the ledger row only when this returns
        normally — see dsr/alerts.py's D-AL-7 note on why a swallowed
        failure would suppress every future retry."""
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
    the customer's Teams channel. It is captured in a closure over `send`
    and deliberately never interpolated into a log line or an exception
    message — only the response's status code is, which identifies nothing
    about the destination.
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
        # A transport exception (timeout, DNS failure, refused connection)
        # is allowed to propagate as-is rather than being caught and
        # rewrapped: it comes from `requests`, not from us, and it never
        # carries the URL — only the caller's own logging decides what, if
        # anything, gets recorded about it.
        response = self._post(self._webhook_url, json=_teams_card(alert))

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
