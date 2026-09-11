# Collection-order guard: import fides.api.privacycare.asgi before anything
# else in this package.
#
# fides.api.privacycare.api.router.register() must run — and complete —
# before fides.api.main is imported anywhere in the process (see
# fides/api/privacycare/asgi.py for why: create_fides_app()'s router list is
# a mutable default bound at function-definition time, so an import of
# fides.api.main that lands first permanently caches a routeless `app` for
# the rest of the process). asgi.py used to note that
# test_api_registration.py happened to sort alphabetically first in this
# package and so got there first by accident. That stopped being true once
# test_api_assessments.py and test_api_schemas.py were added — both sort
# before test_api_registration.py — so the "happens to be first" guarantee
# was already broken and depended on nobody adding a file whose name sorts
# earlier still, or nobody running a single test file directly (pytest only
# loads *that* file's conftest chain, not sibling test modules).
#
# A conftest.py is collected before any test module in its directory
# regardless of file name or which specific test file pytest was asked to
# run, so doing the import here — rather than relying on alphabetical luck —
# is what actually guarantees registration happens first for every test in
# this package, every time.
import os
import socket


def _fides_db_hostname_resolves() -> bool:
    try:
        socket.gethostbyname("fides-db")
        return True
    except OSError:
        return False


def _configure_local_infra_override() -> None:
    """Point Fides' own DB/Redis config at the host-mapped ports declared in
    privacycare.ports.env when running outside the docker-compose network.

    CI (and any docker-compose-network run) resolves the `fides-db`/`redis`
    hostnames directly per .fides/fides.toml and needs no override — this is
    a no-op there. Locally, on a developer's host, those hostnames don't
    resolve, and Fides' own default config would otherwise be unreachable
    for TestClient-based tests (see test_api_http.py) that boot the real
    ASGI app rather than calling private helpers directly.
    """
    if _fides_db_hostname_resolves():
        return
    os.environ.setdefault("FIDES__DATABASE__SERVER", "127.0.0.1")
    os.environ.setdefault("FIDES__DATABASE__PORT", "5442")
    os.environ.setdefault("FIDES__REDIS__HOST", "127.0.0.1")
    os.environ.setdefault("FIDES__REDIS__PORT", "6479")


_configure_local_infra_override()

# Only now import the real app — after any env override above, and before
# any other module in this package can import fides.api.main first.
import fides.api.privacycare.asgi  # noqa: F401,E402  (import order is the point)
