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
from loguru import logger

# M5: `logging.getLogger(__name__)` (stdlib) used to sit here where every
# other PrivacyCare module uses loguru's `logger` — differently-shaped call
# sites (`%s` positional vs `{}`) for no reason tied to this module. Kept as
# a plain `import logging` below only for the C2 fix, which has to reach
# into urllib3's own stdlib logger by name — that is not this module's
# logger and must stay stdlib to address urllib3's.

_ENV_CHANNEL = "PRIVACYCARE_ALERT_CHANNEL"
_ENV_TEAMS_WEBHOOK = "PRIVACYCARE_ALERT_TEAMS_WEBHOOK"

# C2. `requests`/urllib3 log the full request line — scheme, host, port,
# method, URL — at DEBUG (`urllib3.connectionpool.HTTPConnectionPool.
# _make_request`), and for a Teams incoming webhook the URL PATH is the
# bearer credential. Fides installs `InterceptHandler` on the ROOT logger
# (`util/logger.py`), so a DEBUG-level deployment (`FIDES__DEV_MODE=True` —
# which is also exactly what an operator reaches for when a webhook looks
# broken) prints the token on every successful send unless this specific
# logger is muted around the call. Named by string, not imported: urllib3 is
# a `requests` dependency, not a PrivacyCare one, and importing it here only
# to reach its logger would be a strange reason to add the dependency.
_URLLIB3_CONNECTIONPOOL_LOGGER = "urllib3.connectionpool"

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
        reach the endpoint" for delivery.

        M4: any credential this implementation holds (a webhook URL, an API
        token, a WhatsApp access token — whatever a future channel is
        configured with) must never reach a log line or an exception
        message that `send` lets escape. This is not only
        `TeamsWebhookChannel`'s obligation to itself: the caller
        (alert_job.py) logs a failed send's exception verbatim, so an
        implementation that leaks its own credential through an unsanitised
        exception leaks it again here, one level up, regardless of what
        this docstring said before this line existed.
        """
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
            "privacycare DSR alert (logging channel): request={} right={} "
            "kind={} days_left={} recipient={}",
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
    exception message — two different routes, closed two different ways.

    The exception route: the non-2xx path only ever interpolates the
    response status code. The transport-failure path needs its own guard:
    `requests` characteristically embeds the request URL — webhook token
    included — in the string form of a connection error
    (`HTTPSConnectionPool(...): Max retries exceeded with url:
    /webhookb2/<guid>/IncomingWebhook/<token>/...`), so `send` catches the
    raw transport exception and re-raises carrying only its type name, never
    its message and never itself as `__cause__` (chaining would put the
    leaking text straight back into the traceback).

    The log route (C2, final review of plan 15): `requests`/urllib3 log the
    full request line — including that same URL — at DEBUG regardless of
    whether the exception path is ever reached, on every successful send as
    much as a failed one (`urllib3.connectionpool.HTTPConnectionPool.
    _make_request`). Fides installs its `InterceptHandler` on the root
    logger, so any process running at DEBUG (`FIDES__DEV_MODE=True` —
    itself exactly what an operator reaches for while diagnosing why a
    webhook is not working) prints the token to the container log the
    moment a real webhook URL is configured. `send` closes this by raising
    `urllib3.connectionpool`'s own logger to WARNING for the duration of the
    one call that can trigger it, then restoring whatever level it had
    before — never touching the root logger or any other logger, so nothing
    else's DEBUG output is affected.
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
        # M6: built outside the transport `try` below — a serialisation bug
        # in `_teams_card` has nothing to do with the transport and must not
        # be re-raised as "Teams webhook transport failure: TypeError",
        # which would send whoever triages it looking at the network
        # instead of the payload.
        card = _teams_card(alert)

        # C2: mute urllib3's own request-line logging for exactly the
        # duration of the call that can trigger it (see the class
        # docstring). Saved and restored, never hard-set to a fixed level —
        # an operator's own explicit level for this logger must survive a
        # send that happens to run while they're debugging something else.
        urllib3_logger = logging.getLogger(_URLLIB3_CONNECTIONPOOL_LOGGER)
        previous_urllib3_level = urllib3_logger.level
        urllib3_logger.setLevel(logging.WARNING)
        try:
            # A transport exception (timeout, DNS failure, refused
            # connection, TLS error) is caught here rather than left to
            # propagate: with the real transport, `requests`/urllib3 render
            # the target URL — webhook token included — into the
            # exception's own message, so letting it through unchanged
            # would write the credential into whatever catches it upstream
            # (logs, an error tracker, a traceback). Only the exception's
            # type name survives; `from None` stops the original (and its
            # message) from riding along as `__cause__`.
            try:
                response = self._post(self._webhook_url, json=card)
            except Exception as exc:
                raise RuntimeError(
                    f"Teams webhook transport failure: {type(exc).__name__}"
                ) from None
        finally:
            urllib3_logger.setLevel(previous_urllib3_level)

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
