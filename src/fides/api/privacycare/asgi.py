# ASGI entrypoint for PrivacyCare.
#
# Start the server against `fides.api.privacycare.asgi:app` instead of
# `fides.api.main:app`. This module imports PrivacyCare first — installing the
# autogenerate guard and registering our routes — and only then imports Fides'
# app, so ordering is deterministic rather than hoped for. Nothing Ethyca owns
# is edited; the compose overlay repoints the command.
from fides.api.privacycare.api.router import register

register()

from fides.api.main import app  # noqa: E402  (import order is the point)

__all__ = ["app"]
