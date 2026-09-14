"""The two scopes that make discovery authorisable and its navigation visible.

D-DM-1: named for PrivacyCare, not borrowed from Ethyca's discovery_monitor:*.
The capability is ours — plan 13 classifies into Josephine's Kenyan taxonomy,
which upstream has no concept of — and a future upstream take must not be able
to grant authority over our routes by a string match on their scope name.
"""
import pathlib

from fides.api.oauth.roles import (
    APPROVER,
    CONTRIBUTOR,
    DATA_STEWARD,
    OWNER,
    ROLES_TO_SCOPES_MAPPING,
    VIEWER,
)
from fides.common.scope_registry import (
    PRIVACYCARE_DISCOVERY_READ,
    PRIVACYCARE_DISCOVERY_UPDATE,
    SCOPE_DOCS,
    SCOPE_REGISTRY,
)


def test_both_scopes_are_registered_and_documented():
    assert PRIVACYCARE_DISCOVERY_READ == "privacycare_discovery:read"
    assert PRIVACYCARE_DISCOVERY_UPDATE == "privacycare_discovery:update"
    for scope in (PRIVACYCARE_DISCOVERY_READ, PRIVACYCARE_DISCOVERY_UPDATE):
        assert scope in SCOPE_REGISTRY, f"{scope} is not in the registry"
        assert SCOPE_DOCS[scope], f"{scope} has no documentation string"


def test_we_did_not_borrow_ethycas_scope_names():
    # D-DM-1: the divergence is deliberate and must stay legible.
    assert "discovery_monitor:read" not in SCOPE_REGISTRY
    assert "discovery_monitor:update" not in SCOPE_REGISTRY


def test_viewer_reads_but_cannot_configure():
    # D-DM-2: reading a record of processing is routine; aiming a scanner at a
    # live database is privileged and must not arrive by inheritance.
    assert PRIVACYCARE_DISCOVERY_READ in ROLES_TO_SCOPES_MAPPING[VIEWER]
    assert PRIVACYCARE_DISCOVERY_UPDATE not in ROLES_TO_SCOPES_MAPPING[VIEWER]


def test_owner_and_contributor_configure():
    for role in (OWNER, CONTRIBUTOR):
        assert PRIVACYCARE_DISCOVERY_READ in ROLES_TO_SCOPES_MAPPING[role]
        assert PRIVACYCARE_DISCOVERY_UPDATE in ROLES_TO_SCOPES_MAPPING[role]


def test_data_steward_inherits_read_only():
    # data_steward_scopes derives from viewer_scopes, so this follows from
    # D-DM-2 rather than being a separate decision. Pinned so it stays true.
    assert PRIVACYCARE_DISCOVERY_READ in ROLES_TO_SCOPES_MAPPING[DATA_STEWARD]
    assert PRIVACYCARE_DISCOVERY_UPDATE not in ROLES_TO_SCOPES_MAPPING[DATA_STEWARD]


def test_approver_gets_neither():
    assert PRIVACYCARE_DISCOVERY_READ not in ROLES_TO_SCOPES_MAPPING[APPROVER]
    assert PRIVACYCARE_DISCOVERY_UPDATE not in ROLES_TO_SCOPES_MAPPING[APPROVER]


def _nav_config_source() -> str:
    path = (
        pathlib.Path(__file__).resolve().parents[2]
        / "clients/admin-ui/src/features/common/nav/nav-config.tsx"
    )
    return path.read_text()


def test_the_navigation_is_gated_on_our_scope_not_ethycas():
    # The spike's Q5 finding: nav-config.tsx gates the Data Discovery nav items
    # themselves, so without this re-gating the feature is invisible rather
    # than merely empty — no amount of correct backend work would show it.
    source = _nav_config_source()
    assert "PRIVACYCARE_DISCOVERY_READ" in source
    assert "DISCOVERY_MONITOR_READ" not in source


def test_every_ethyca_edit_carries_its_marker():
    # Spec §6: the discipline that replaced "never edit an Ethyca file" is that
    # every edit is recorded and greppable.
    marker = "PrivacyCare (spec 2026-09-14 D-DM-1)"
    root = pathlib.Path(__file__).resolve().parents[2]
    for relative in (
        "src/fides/common/scope_registry.py",
        "src/fides/api/oauth/roles.py",
        "clients/admin-ui/src/features/common/nav/nav-config.tsx",
        "clients/admin-ui/src/types/api/models/ScopeRegistryEnum.ts",
    ):
        assert marker in (root / relative).read_text(), f"{relative} lacks the marker"
