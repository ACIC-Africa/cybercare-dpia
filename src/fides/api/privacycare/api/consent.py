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

THE UNCONFIGURED-RULE STATE. `find_stale_consents` (consent/detector.py)
calls `materiality.active_rule`, which raises `ValueError` when
`privacycare_consent_rule` holds zero rows (Task 4's seed CLI has not run
yet — the live deployment's actual state today) or more than one (a second
writer bypassed the fixed-id seed). Both are configuration states an
operator can fix, not "there are no stale consents" and not a bug in the
request, so two answers are both wrong here: a bare 500 would bury an
operator-fixable gap behind an opaque stack trace, and a quiet 200 with an
empty list would misreport "the detector ran and found nothing" when the
truth is "the detector never ran at all" — the exact kind of false
"nothing to see here" this whole feature exists to prevent. This route
catches that ValueError and answers 503 Service Unavailable with the
message naming the cause, the same shape api/reports.py's
get_assessment_pdf already uses for a PDFRenderError: an operator-fixable
dependency being unavailable, not a defect in the caller's request.
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

    See this module's docstring for the 503 an unconfigured
    `privacycare_consent_rule` produces — that is deliberate, not an
    oversight left uncaught.
    """
    try:
        stale = find_stale_consents(db, notice_key=notice_key)
    except ValueError as exc:
        raise HTTPException(
            status_code=status_codes.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    return paginate([_response_from_stale_consent(item) for item in stale], params)
