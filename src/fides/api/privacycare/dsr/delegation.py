# The seam between the register and Fides: for the three Kenyan rights that
# actually move data, this module makes sure a matching Fides Policy exists
# with the *register's* timeframe on it (D-DSR-7 — Ethyca's own screens must
# not be able to show a different number than ours), and creates the
# `privacyrequest` row that does the moving. Rectification, restriction and
# objection are out of scope here on purpose: rectification has no Fides
# analogue defined yet, and restriction/objection move no data at all — see
# module docstring in register.py and timelines.py for why.
#
# Unlike register.py and timelines.py, this module writes through Fides' own
# ORM models (Policy, Rule, PrivacyRequest) rather than raw SQL, because
# those models carry validation (Rule.save/_validate_rule, key/name
# uniqueness) that a hand-written INSERT would silently skip. The one piece
# of raw SQL here is the UPDATE back onto our own privacycare_dsr_request
# table, which is ours to write to directly.
from typing import Optional

import sqlalchemy
from sqlalchemy.orm import Session

from fides.api.models.policy import Policy, Rule
from fides.api.models.privacy_request import PrivacyRequest
from fides.api.privacycare.dsr.register import get_request
from fides.api.privacycare.dsr.timelines import timeline_days
from fides.api.schemas.policy import ActionType
from fides.api.schemas.redis_cache import Identity
from fides.api.service.privacy_request.request_service import (
    build_required_privacy_request_kwargs,
)

# right -> Fides policy key. Only the three rights that move data get one.
# Portability is deliberately absent an entry of its own action type here —
# see _ACTION_TYPE below — because D-DSR-3 says it differs from access in
# format obligation and clock, not in the action the runner performs.
DELEGATING_RIGHTS: dict[str, str] = {
    "access": "privacycare_kenya_access",
    "erasure": "privacycare_kenya_erasure",
    "portability": "privacycare_kenya_portability",
}

# right -> the Fides action a delegating policy's rule executes. Portability
# maps to ActionType.access per D-DSR-3 — Fides has no separate "portability"
# action, and the runner does the same access-shaped work either way.
_ACTION_TYPE: dict[str, ActionType] = {
    "access": ActionType.access,
    "erasure": ActionType.erasure,
    "portability": ActionType.access,
}

# The shipped default policies/rules we copy Rule obligations from, rather
# than inventing a storage destination or a masking strategy (a policy whose
# rule pointed at the wrong storage or masking config would execute wrongly
# against customer data). If these keys are ever absent, ensure_kenyan_policies
# raises rather than guessing.
_DEFAULT_ACCESS_POLICY_RULE_KEY = "default_access_policy_rule"
_DEFAULT_ERASURE_POLICY_RULE_KEY = "default_erasure_policy_rule"

_SET_FIDES_REQUEST_ID_SQL = sqlalchemy.text(
    "UPDATE privacycare_dsr_request SET fides_privacy_request_id = :fides_id "
    "WHERE id = :id"
)


def kenyan_policy_key(right: str) -> Optional[str]:
    """None for any right that doesn't delegate — rectification (no Fides
    shape yet) and restriction/objection (no data moves, so no shape ever)."""
    return DELEGATING_RIGHTS.get(right)


def ensure_kenyan_policies(db: Session) -> dict[str, int]:
    """Upserts one Policy + Rule per delegating right, every call, so a
    changed Kenyan timeline (e.g. a future statutory amendment) can never be
    left stale on Fides' side. Returns policy key -> execution_timeframe
    written, which is also the brief's D-DSR-7 control: that number must
    equal timeline_days(db, right) for every delegating right.

    Reuses the storage_destination_id / masking_strategy already carried by
    the shipped default_access_policy_rule / default_erasure_policy_rule
    rather than inventing either — see module docstring.
    """
    default_access_rule = Rule.get_by(
        db, field="key", value=_DEFAULT_ACCESS_POLICY_RULE_KEY
    )
    default_erasure_rule = Rule.get_by(
        db, field="key", value=_DEFAULT_ERASURE_POLICY_RULE_KEY
    )
    if default_access_rule is None or default_erasure_rule is None:
        raise ValueError(
            "no shipped default access/erasure rule to copy Rule obligations "
            "from (expected keys "
            f"{_DEFAULT_ACCESS_POLICY_RULE_KEY!r} and "
            f"{_DEFAULT_ERASURE_POLICY_RULE_KEY!r} in `rule`) — refusing to "
            "invent a storage destination or masking strategy"
        )

    written: dict[str, int] = {}
    for right, policy_key in DELEGATING_RIGHTS.items():
        days = timeline_days(db, right)
        if days is None:
            # Can't happen for today's three delegating rights (none of them
            # is objection), but a future timeline change must fail loudly
            # here rather than write a policy with no timeframe.
            raise ValueError(
                f"{right!r} delegates to Fides but has no Kenyan timeline — "
                "refusing to create a policy with an undefined execution_timeframe"
            )

        policy = Policy.create_or_update(
            db,
            data={
                "name": f"PrivacyCare Kenya {right.title()} Policy",
                "key": policy_key,
                "execution_timeframe": days,
            },
        )
        written[policy_key] = days

        action_type = _ACTION_TYPE[right]
        rule_data: dict = {
            "name": f"PrivacyCare Kenya {right.title()} Rule",
            "key": f"{policy_key}_rule",
            "policy_id": policy.id,
            "action_type": action_type.value,
        }
        if action_type == ActionType.access:
            rule_data["storage_destination_id"] = (
                default_access_rule.storage_destination_id
            )
        else:
            rule_data["masking_strategy"] = default_erasure_rule.masking_strategy

        Rule.create_or_update(db, data=rule_data)

    return written


def delegate(db: Session, *, request_id: str) -> Optional[str]:
    """Creates (or returns the existing) Fides privacyrequest id for a
    register row, or None when the right doesn't delegate at all.

    Order matters: the non-delegating check runs before anything touches
    `privacyrequest` (restriction/objection must never produce a row there,
    not even transiently); the idempotency check runs before the D-DSR-7
    drift check and before creating a new one (delegating twice must return
    the same id, not a second row, and must not be blocked by a clock that
    has since moved — the request was already delegated on whatever clock
    was live at the time).

    I5 (final review): a *stored* fides_privacy_request_id whose
    `privacyrequest` row has since vanished (deleted directly against
    Ethyca's own tables, out from under the register) is not "existing" for
    idempotency purposes — returning it would hand back a dead id as though
    it were live. That case falls through to create a fresh one, the same
    as a row that was never delegated at all.

    I2 (final review, D-DSR-7). The timeline table is deliberately editable
    without a deploy (see timelines.py's own docstring), so the moment an
    SME answers an open question with a new number, new register rows get
    the new clock while any already-provisioned Fides policy keeps the old
    one until ensure_kenyan_policies() is re-run — and a hand-edit of the
    policy in Ethyca's admin UI has the identical effect. Before creating a
    request on a policy, this checks the policy's persisted
    execution_timeframe against the *current* timeline_days(db, right) and
    refuses rather than silently delegating onto a stale clock. The message
    names both numbers so an operator can tell at a glance which side is
    wrong and that re-running ensure_kenyan_policies() is the fix.

    I1 (partial, per ruling). This function creates the PrivacyRequest and
    persists its identity and masking secrets, but deliberately skips most
    of Fides' own post-create contract for a privacy request (see
    drp_endpoints.py's create_drp_privacy_request, lines ~112-135, for the
    reference shape this is compared against):

      - `cache_data` (writes the identity/encryption keys Fides' own
        execution pipeline reads out of Redis) — skipped because nothing
        here queues the request for execution (see below), so there is
        nothing yet for that cache to serve.
      - error-notification dispatch (`check_and_dispatch_error_notifications`)
        — skipped for the same reason: it exists to flush a backlog of
        already-failed requests' error emails, and nothing here has run the
        pipeline that could produce one.
      - `queue_privacy_request` — DELIBERATELY NOT CALLED, and this is not
        an oversight. Queueing executes a DSR against the customer's
        connected systems; this register exists precisely so a human
        decides and records an outcome (see register.record_decision)
        before that happens. Auto-queuing on create would remove the
        approval step a regulatory tool of this kind must have. The
        created PrivacyRequest sits in Fides' own pending-approval state
        until a person (outside this module, via Fides' normal review
        flow) approves it for execution.
      - duplicate detection (`check_for_duplicates`,
        fides.api.service.privacy_request.duplication_detection) — skipped
        because it exists to de-duplicate *unqueued* submissions competing
        for the same execution slot; with nothing queued here, there is
        nothing yet to de-duplicate against.

    masking secrets ARE persisted (`policy.generate_masking_secrets()` +
    `privacy_request.persist_masking_secrets`), unlike the four skips
    above: without them an erasure request, when it eventually IS queued
    and runs, logs "Secret type ... expected but was not present" — the
    Kenyan erasure rule copies the shipped hmac masking strategy
    (ensure_kenyan_policies), which needs them, and they cost nothing to
    generate now versus at approval time.
    """
    row = get_request(db, request_id)
    policy_key = kenyan_policy_key(row["right"])
    if policy_key is None:
        return None

    existing = row["fides_privacy_request_id"]
    if existing and PrivacyRequest.get_by(db, field="id", value=existing) is not None:
        return existing

    policy = Policy.get_by(db, field="key", value=policy_key)
    if policy is None:
        raise ValueError(
            f"no Fides policy {policy_key!r} — call ensure_kenyan_policies() "
            "before delegating"
        )

    current_days = timeline_days(db, row["right"])
    if policy.execution_timeframe != current_days:
        raise ValueError(
            f"D-DSR-7 drift: Fides policy {policy_key!r} still carries "
            f"execution_timeframe={policy.execution_timeframe!r}, but the "
            f"Kenyan timeline for {row['right']!r} is now "
            f"{current_days!r} days — refusing to delegate on a stale "
            "clock; re-run ensure_kenyan_policies() to sync the policy "
            "before delegating"
        )

    privacy_request = PrivacyRequest.create(
        db=db,
        data=build_required_privacy_request_kwargs(
            row["received_at"],
            policy.id,
            verification_required=False,
            authenticated=True,
        ),
    )
    # The register's subject_identifier carries no guaranteed format (email,
    # phone, national ID, ...), so it goes on as external_id rather than
    # Identity.email — assuming email shape would raise on anything that
    # isn't one. persist_identity writes a plain ProvidedIdentity row via
    # this same session; it does not touch Redis (that's cache_identity, a
    # different method, which nothing here has asked for).
    privacy_request.persist_identity(
        db=db, identity=Identity(external_id=row["subject_identifier"])
    )

    # See the module-level docstring above: masking secrets are the one
    # piece of Fides' post-create contract this function does replicate.
    if masking_secrets := policy.generate_masking_secrets():
        privacy_request.persist_masking_secrets(masking_secrets)

    db.execute(
        _SET_FIDES_REQUEST_ID_SQL,
        {"fides_id": privacy_request.id, "id": request_id},
    )
    return privacy_request.id
