# The daily walk over the register: for every obligation, decide whether an
# alert is due right now and, if so, deliver it. This module owns
# selection, delivery, and the run's summary — nothing else. Threshold
# logic (what "due" means) lives in dsr/alerts.py; how a message actually
# leaves the process (Teams, logging, null) lives in dsr/channels.py; this
# module is the thing that puts them together once a day. No HTTP: it is
# registered from asgi.py, never exposed as a route.
import math
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from loguru import logger
from sqlalchemy.orm import Session

from fides.api.privacycare.dsr.alerts import alert_due, alerts_sent_for, record_alert
from fides.api.privacycare.dsr.channels import (
    Alert,
    AlertChannel,
    channel_from_environment,
)
from fides.api.privacycare.dsr.delegation import fides_request_statuses
from fides.api.privacycare.dsr.register import list_requests
from fides.api.privacycare.dsr.timelines import timeline_days
from fides.api.schemas.privacy_request import PrivacyRequestStatus
from fides.api.tasks import DatabaseTask, celery_app
from fides.api.tasks.scheduled.scheduler import scheduler
from fides.config import CONFIG

# D-AL-5. Deliberately a DIFFERENT setting from PRIVACYCARE_DPO_EMAIL
# (register.py's resolve_owner fallback): "the DPO owns this" and "nobody
# owns this" are different operational facts, and collapsing them would
# hide the unassigned backlog inside the DPO's ordinary queue.
_ESCALATION_EMAIL_ENV = "PRIVACYCARE_ALERT_ESCALATION_EMAIL"

# D-AL-6. The one PrivacyRequestStatus that means "Fides is done with this,
# stop chasing the owner" — every other status (pending, approved,
# in_processing, denied, canceled, error, ...) is still a live obligation
# as far as this job is concerned.
_DISCHARGED_STATUS = PrivacyRequestStatus.complete.value

DSR_DEADLINE_ALERTS_JOB = "privacycare_dsr_deadline_alerts"


@dataclass(frozen=True)
class RunSummary:
    """One run's whole story, in seven numbers an operator can read without
    opening a log. `attempted` always equals `sent + failed` — every
    obligation the run actually tried to deliver an alert for lands in
    exactly one of those two. Everything else in the register that day
    either was not yet due (no counter — the common case, most of the
    register on most days) or is accounted for by exactly one of
    skipped_discharged / unclocked / vanished / unowned."""

    attempted: int
    sent: int
    failed: int
    skipped_discharged: int
    unclocked: int
    vanished: int
    unowned: int


def _days_left(deadline_at: Optional[datetime], now: datetime) -> Optional[int]:
    """The Alert payload's days_left, computed against the run's own `now`
    rather than a live clock read — same reason alert_due takes `now` as an
    argument instead of reading it: every boundary must be testable at a
    frozen instant. Deliberately not imported from api/dsr.py's near-
    identical `_days_left`: that one is part of an HTTP response shape and
    always reads the live clock; this module owns no HTTP and must not
    depend on a route module for its own internals.

    A deadline still ahead rounds UP (ceil): six hours from now is still
    meaningfully "1 day left", not "0". A deadline already passed rounds
    DOWN (floor), so it always comes back negative rather than `ceil`
    rounding the first 23h59m of lateness up to a misleading 0 ("due
    today" and "just breached" must read differently).
    """
    if deadline_at is None:
        return None
    aware_deadline = (
        deadline_at
        if deadline_at.tzinfo is not None
        else deadline_at.replace(tzinfo=timezone.utc)
    )
    remaining_days = (aware_deadline - now).total_seconds() / 86400
    if remaining_days >= 0:
        return math.ceil(remaining_days)
    return math.floor(remaining_days)


def run_deadline_alerts(
    db: Session, *, channel: AlertChannel, now: Optional[datetime] = None
) -> RunSummary:
    """Walk the whole register once and deliver every alert that is due.

    `now` defaults to the live clock but takes an explicit value in every
    test — same reason alert_due (dsr/alerts.py) takes it rather than
    reading it: every boundary becomes testable without freezing time.
    `channel` is likewise always passed in, never read from the
    environment here — that happens once, in the Celery wrapper below, so
    this function has no global state and no import-time side effect.

    Order of checks per row, and why it is this order: a delegating row's
    Fides status (discharged / vanished) is decided FIRST and
    unconditionally, before any deadline arithmetic runs at all — D-AL-6
    is about what Fides has already done, which overrides whether the
    register's own clock would otherwise call an alert due. Only a row
    that survives that check is asked "is an alert due", and only a due
    alert is resolved to a recipient and attempted.

    A channel failure (D-AL-7) is caught around exactly one call —
    `channel.send` — so it can never suppress the loop, and the ledger
    write (`record_alert`) only ever runs after a *successful* send, so a
    failed delivery leaves nothing for its own unique constraint to
    (wrongly) treat as "already sent" on the next run.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    rows = list_requests(db)

    # Residual R2 / plan-15 Ruling P1: every delegated id's status is read
    # in ONE query for the whole run, via the shared helper in
    # dsr/delegation.py — never one PrivacyRequest load per register row.
    # Task 4 imports the same helper for the DSR list endpoint's matching
    # fix, so there is exactly one implementation of this batch query, not
    # two that could drift apart.
    delegated_ids = [
        row["fides_privacy_request_id"]
        for row in rows
        if row["fides_privacy_request_id"]
    ]
    fides_statuses = fides_request_statuses(db, delegated_ids)

    attempted = sent = failed = 0
    skipped_discharged = unclocked = vanished = unowned = 0

    for row in rows:
        fides_id = row["fides_privacy_request_id"]
        if fides_id:
            status = fides_statuses.get(fides_id)
            if status is None:
                # D-AL-6 / acceptance 5: a stored id that no longer resolves
                # is a data-integrity problem, not a deadline problem —
                # reported, never silently alerted on and never silently
                # skipped.
                vanished += 1
                logger.error(
                    "privacycare DSR alert run: dsr_request={} right={} has "
                    "fides_privacy_request_id={} that no longer resolves to "
                    "any privacyrequest row",
                    row["id"],
                    row["right"],
                    fides_id,
                )
                continue
            if status == _DISCHARGED_STATUS:
                # D-AL-6 / acceptance 4: chasing an owner about work Fides
                # already finished is how a new channel earns being muted.
                skipped_discharged += 1
                continue

        if row["deadline_at"] is None:
            # D-AL-4 / acceptance 3: objection, until OQ-PRIVACY-02 is
            # answered. Reported, not passed over in silence — that is what
            # keeps the open question visible.
            unclocked += 1
            continue

        days_allowed = timeline_days(db, row["right"])
        already_sent = alerts_sent_for(db, row["id"])
        kind = alert_due(
            deadline_at=row["deadline_at"],
            days_allowed=days_allowed,
            now=now,
            already_sent=already_sent,
        )
        if kind is None:
            # Not yet due, or already sent for this kind — the ordinary
            # case for most of the register on most days. No counter: this
            # is not an exceptional outcome worth reporting.
            continue

        owner_email = row["owner_email"]
        if owner_email:
            recipient = owner_email
        else:
            # D-AL-5 / acceptance 6: an obligation nobody owns is the one
            # most likely to be missed, so it is counted here every time
            # this branch is reached — whether or not an escalation
            # address is configured to actually reach anyone — not only
            # when delivery has nowhere to go at all.
            unowned += 1
            recipient = os.environ.get(_ESCALATION_EMAIL_ENV)
            if recipient is None:
                logger.error(
                    "privacycare DSR alert run: dsr_request={} right={} "
                    "kind={} is unowned and {} is not configured — alert "
                    "not sent",
                    row["id"],
                    row["right"],
                    kind,
                    _ESCALATION_EMAIL_ENV,
                )
                continue

        alert = Alert(
            dsr_request_id=row["id"],
            right=row["right"],
            kind=kind,
            deadline_at=row["deadline_at"],
            days_left=_days_left(row["deadline_at"], now),
            owner_email=owner_email,
            recipient=recipient,
        )

        attempted += 1
        try:
            channel.send(alert)
        except Exception as exc:  # noqa: BLE001 — D-AL-7: one channel
            # failure degrades the run, it never kills it, and the alert
            # row below must NOT be written — the next run is the retry,
            # and that only works because nothing was recorded here.
            failed += 1
            logger.error(
                "privacycare DSR alert run: delivery failed for "
                "dsr_request={} kind={}: {}",
                row["id"],
                kind,
                exc,
            )
            continue

        sent += 1
        recorded = record_alert(
            db,
            dsr_request_id=row["id"],
            kind=kind,
            channel=channel.name,
            recipient=recipient,
        )
        if not recorded:
            # A concurrent run recorded this exact (id, kind) between this
            # run's alert_due check and this insert. Delivery already
            # happened (channel.send returned normally above), so this is
            # still counted as sent — the ledger's unique constraint
            # degraded the race to a harmless no-op rather than a second
            # row, which is precisely what it is for.
            logger.warning(
                "privacycare DSR alert run: dsr_request={} kind={} was "
                "already recorded by a concurrent run",
                row["id"],
                kind,
            )

    return RunSummary(
        attempted=attempted,
        sent=sent,
        failed=failed,
        skipped_discharged=skipped_discharged,
        unclocked=unclocked,
        vanished=vanished,
        unowned=unowned,
    )


@celery_app.task(base=DatabaseTask, bind=True)
def _scheduled_dsr_deadline_alerts(self: DatabaseTask) -> None:
    """The thin Celery-task-shaped wrapper APScheduler calls directly (not
    via `.delay()`) — the same pattern request_service.py uses for its own
    cron jobs (`remove_saved_dsr_data`, `poll_for_exited_privacy_request_
    tasks`, ...). All behaviour lives in run_deadline_alerts; this only
    opens a session, resolves the channel from the environment (the one
    place in this module that happens), runs it, and commits.

    Never called directly by a test: run_deadline_alerts is the whole
    testable surface, taking `db` and `channel` as plain arguments with no
    Celery, no scheduler and no environment read involved.
    """
    with self.get_new_session() as db:
        channel = channel_from_environment()
        summary = run_deadline_alerts(db, channel=channel)
        db.commit()
        logger.info(
            "privacycare DSR deadline alert run complete: attempted={} "
            "sent={} failed={} skipped_discharged={} unclocked={} "
            "vanished={} unowned={}",
            summary.attempted,
            summary.sent,
            summary.failed,
            summary.skipped_discharged,
            summary.unclocked,
            summary.vanished,
            summary.unowned,
        )


def initiate_scheduled_dsr_alerts() -> None:
    """Registers the daily job, mirroring request_service.py's own
    `initiate_scheduled_dsr_data_removal` (request_service.py:212): a
    no-op under test_mode (as `request_service.py:215` does — the whole
    suite runs with FIDES__TEST_MODE=true, so this must never assert the
    scheduler is running in a test process, which it is not), then an
    assertion that the scheduler IS running before registering against it
    in any other process.

    07:00 Africa/Nairobi, not Ethyca's US/Eastern default (D-AL-8): the
    start of the customer's working day, for a clock measured in days —
    anything more frequent would repeat the same information without
    making a deadline arrive any later.
    """
    if CONFIG.test_mode:
        return

    assert (
        scheduler.running
    ), "Scheduler is not running! Cannot add DSR deadline alert job."

    logger.info("Initiating scheduler for DSR deadline alerts")
    scheduler.add_job(
        func=_scheduled_dsr_deadline_alerts,
        kwargs={},
        id=DSR_DEADLINE_ALERTS_JOB,
        coalesce=True,
        replace_existing=True,
        trigger="cron",
        minute="0",
        hour="7",
        day="*",
        timezone="Africa/Nairobi",
    )
