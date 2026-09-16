"""The stale-consent detector's HTTP surface (spec D-CON-1, Task 3).

NAMESPACE. Our own `/api/v1/privacycare/consent`, NOT `/api/v1/plus`.
Nothing in the shipped admin UI calls this route — the detector is new,
Kenyan-specific ground with no Plus analogue (see consent/detector.py's own
module docstring for what it computes and why) — so taking a path in
Plus's namespace would only risk colliding with a real Plus endpoint
later. Same reasoning api/dsr.py's module docstring records for the DSR
register, and api/processes.py for the business-process routes.

READ ONLY, NO WRITE SCOPE. This surface exposes exactly one route: list
the stale consents the detector finds right now. There is nothing here to
guard with a write scope, so plan 16 adds none — see scope_registry.py's
PRIVACYCARE_CONSENT_READ comment.

THE 503 IS SCOPED TO CONFIGURATION, AND ONLY CONFIGURATION (coordinator
ruling, Task 3 fix round 2). An earlier version of this route wrapped its
ENTIRE `find_stale_consents(...)` call in one `except ValueError`. That
caught three genuinely different configuration failures from
`materiality.py` (the rule table empty, the rule table ambiguous, the
configured rule value unrecognised) alongside whatever ELSE the detector
might raise mid-loop over a real result set (at the time, a
no-identity preference row) — collapsing four distinct causes into one
undifferentiated 503, and worse: because `find_stale_consents` builds its
whole list before returning any of it, one bad row anywhere in the result
set discarded every OTHER row's legitimate finding along with it. A single
corrupt row producing false UNAVAILABILITY, indistinguishable from "nobody
ran the seed script", is the same false reassurance this whole feature
exists to prevent, wearing a different hat.

So the `try` below covers ONLY `materiality.validate_active_rule(db)` —
resolve the configured rule and confirm it is one this module can
evaluate, nothing more — and NOTHING else in this route may produce a
503. Each of validate_active_rule's three failure modes carries its own
`ValueError` message naming which one it is and what an operator should
do (see materiality.py). Once that call succeeds, `find_stale_consents`
runs uncaught: a genuine bug in the detector must surface as an honest
500, never get laundered into an availability problem a caller would read
as "try again once someone runs the seed CLI". (`_subject`'s own
no-identity case no longer raises at all — see detector.py's docstring on
that function — so it is not a site this route needs to guard against
either way.)
"""
from typing import Optional

from fastapi import Depends, HTTPException, Security
from fastapi import status as status_codes
from fastapi_pagination import Page, Params, paginate
from sqlalchemy.orm import Session

from fides.api.deps import get_db
from fides.api.oauth.utils import verify_oauth_client
from fides.api.privacycare.api.consent_schemas import StaleConsentResponse
from fides.api.privacycare.api.router import privacycare_consent_router
from fides.api.privacycare.consent.detector import StaleConsent, find_stale_consents
from fides.api.privacycare.consent.materiality import validate_active_rule
from fides.common.scope_registry import PRIVACYCARE_CONSENT_READ


def _response_from_stale_consent(stale: StaleConsent) -> StaleConsentResponse:
    return StaleConsentResponse(
        subject=stale.subject,
        subject_kind=stale.subject_kind,
        notice_key=stale.notice_key,
        notice_name=stale.notice_name,
        consented_version=stale.consented_version,
        live_version=stale.live_version,
        added_uses=stale.added_uses,
        preference=stale.preference,
        received_at=stale.received_at,
    )


@privacycare_consent_router.get(
    "/stale",
    dependencies=[Security(verify_oauth_client, scopes=[PRIVACYCARE_CONSENT_READ])],
    response_model=Page[StaleConsentResponse],
)
def list_stale_consents(
    notice_key: Optional[str] = None,
    *,
    params: Params = Depends(),
    db: Session = Depends(get_db),
) -> Page[StaleConsentResponse]:
    """Every affirmative consent recorded against a notice version that has
    since gained a data use (spec D-CON-2/D-CON-4), computed fresh on every
    call. `notice_key` is optional and additive — an unfiltered call reports
    every notice at once, matching `find_stale_consents`' own default.

    See this module's docstring for exactly what turns into the 503 here
    (configuration only) and why nothing else does.
    """
    try:
        validate_active_rule(db)
    except ValueError as exc:
        raise HTTPException(
            status_code=status_codes.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    stale = find_stale_consents(db, notice_key=notice_key)
    return paginate([_response_from_stale_consent(item) for item in stale], params)
