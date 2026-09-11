# PrivacyCare's router, and its registration into Fides' router list.
#
# Fides has no plugin hook, so registration works by appending to
# `app_setup.ROUTERS`. That list was verified to have exactly one consumer —
# the default argument of `create_fides_app` (app_setup.py:84) — before this
# approach was chosen. An earlier PrivacyCare change mutated a different Fides
# collection, `EXCLUDED_TABLES`, which turned out to be dual-purpose and made
# `fides db reset` drop our tables. Check the consumers before mutating anything
# of Ethyca's.
from fides.api.util.api_router import APIRouter

PRIVACYCARE_PREFIX = "/plus/privacy-assessments"

privacycare_router = APIRouter(prefix=PRIVACYCARE_PREFIX, tags=["PrivacyCare"])

_REGISTERED_FLAG = "__privacycare_router_registered__"


def register() -> None:
    # Idempotent: importing twice must not register the router twice.
    from fides.api import app_setup

    if getattr(app_setup, _REGISTERED_FLAG, False):
        return
    from fides.api.privacycare.api import assessments  # noqa: F401  (binds routes)

    app_setup.ROUTERS.append(privacycare_router)
    setattr(app_setup, _REGISTERED_FLAG, True)
