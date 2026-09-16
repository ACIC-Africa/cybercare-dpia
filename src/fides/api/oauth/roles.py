from enum import Enum
from typing import Dict, List, Optional

from fides.common.scope_registry import (
    CLI_OBJECTS_READ,
    CLIENT_READ,
    CONNECTION_READ,
    CONNECTION_TYPE_READ,
    CONNECTOR_TEMPLATE_REGISTER,
    CONSENT_READ,
    CONSENT_SETTINGS_READ,
    CTL_DATASET_READ,
    CTL_POLICY_READ,
    DATA_CATEGORY_READ,
    DATA_SUBJECT_READ,
    DATA_USE_READ,
    DATASET_READ,
    EVALUATION_READ,
    HEAP_DUMP_EXEC,
    MASKING_EXEC,
    MASKING_READ,
    MESSAGING_CREATE_OR_UPDATE,
    MESSAGING_DELETE,
    MESSAGING_READ,
    ORGANIZATION_READ,
    POLICY_READ,
    PRIVACY_EXPERIENCE_READ,
    PRIVACY_NOTICE_READ,
    PRIVACY_REQUEST_CALLBACK_RESUME,
    PRIVACY_REQUEST_CREATE,
    PRIVACY_REQUEST_DELETE,
    PRIVACY_REQUEST_EMAIL_INTEGRATIONS_SEND,
    PRIVACY_REQUEST_IMPORT,
    PRIVACY_REQUEST_MANUAL_STEPS_RESPOND,
    PRIVACY_REQUEST_MANUAL_STEPS_REVIEW,
    PRIVACY_REQUEST_NOTIFICATIONS_CREATE_OR_UPDATE,
    PRIVACY_REQUEST_NOTIFICATIONS_READ,
    PRIVACY_REQUEST_READ,
    PRIVACY_REQUEST_REVIEW,
    PRIVACY_REQUEST_UPLOAD_DATA,
    PRIVACY_REQUEST_VIEW_DATA,
    PRIVACYCARE_DISCOVERY_READ,
    PRIVACYCARE_RISK_READ,
    RULE_READ,
    SAAS_CONFIG_READ,
    SCOPE_READ,
    SCOPE_REGISTRY,
    STORAGE_CREATE_OR_UPDATE,
    STORAGE_DELETE,
    STORAGE_READ,
    SYSTEM_INTEGRATION_LINK_CREATE_OR_UPDATE,
    SYSTEM_INTEGRATION_LINK_DELETE,
    SYSTEM_INTEGRATION_LINK_READ,
    SYSTEM_MANAGER_READ,
    SYSTEM_READ,
    USER_PERMISSION_ASSIGN_OWNERS,
    USER_READ,
    USER_READ_OWN,
    WEBHOOK_READ,
)

APPROVER = "approver"
CONTRIBUTOR = "contributor"
DATA_STEWARD = "data_steward"
OWNER = "owner"
VIEWER = "viewer"
VIEWER_AND_APPROVER = "viewer_and_approver"
RESPONDENT = "respondent"
EXTERNAL_RESPONDENT = "external_respondent"


class RoleRegistryEnum(Enum):
    """Enum of available roles

    Owner - Full admin
    Viewer - Can view everything
    Approver - Limited viewer but can approve Privacy Requests
    Viewer + Approver = Full View and can approve Privacy Requests
    Contributor - Can't configure storage and messaging
    Data Steward - Viewer + can manage system-integration links
    Respondent - Internal user who can respond to manual steps
    External Respondent - External user who can only respond to assigned manual steps
    """

    owner = OWNER
    viewer_approver = VIEWER_AND_APPROVER
    viewer = VIEWER
    approver = APPROVER
    contributor = CONTRIBUTOR
    data_steward = DATA_STEWARD
    respondent = RESPONDENT
    external_respondent = EXTERNAL_RESPONDENT


approver_scopes = [
    PRIVACY_REQUEST_REVIEW,
    PRIVACY_REQUEST_READ,
    PRIVACY_REQUEST_CALLBACK_RESUME,
    PRIVACY_REQUEST_UPLOAD_DATA,
    PRIVACY_REQUEST_VIEW_DATA,
    PRIVACY_REQUEST_DELETE,
    PRIVACY_REQUEST_CREATE,  # allows approvers to create new privacy requests
    USER_READ,  # allows approver to view user management table and update their own password
    PRIVACY_REQUEST_MANUAL_STEPS_REVIEW,  # allows approvers to see all manual steps
]


viewer_scopes = [  # Intentionally omitted USER_PERMISSION_READ and PRIVACY_REQUEST_READ
    CLI_OBJECTS_READ,
    CLIENT_READ,
    CONNECTION_READ,
    CONSENT_READ,
    CONSENT_SETTINGS_READ,
    CONNECTION_TYPE_READ,
    CTL_DATASET_READ,
    DATA_CATEGORY_READ,
    CTL_POLICY_READ,
    DATASET_READ,
    DATA_SUBJECT_READ,
    DATA_USE_READ,
    EVALUATION_READ,
    MASKING_EXEC,
    MASKING_READ,
    ORGANIZATION_READ,
    POLICY_READ,
    PRIVACY_EXPERIENCE_READ,
    PRIVACY_NOTICE_READ,
    PRIVACY_REQUEST_NOTIFICATIONS_READ,
    # PrivacyCare (spec 2026-09-14 D-DM-1): a Viewer may see monitors and
    # their findings (D-DM-2); configuring one — aiming a scanner at a live
    # database — stays with Owner and Contributor, which derive from the
    # full registry.
    PRIVACYCARE_DISCOVERY_READ,
    # PrivacyCare (spec 2026-09-15 D-DSR-1, I3 final review): PRIVACYCARE_
    # DSR_READ is intentionally NOT here, unlike PRIVACYCARE_DISCOVERY_READ
    # above. Three lines above this block upstream itself omits
    # USER_PERMISSION_READ and PRIVACY_REQUEST_READ from Viewer for the
    # same reason: a privacy request carries data-subject identities. The
    # DSR register carries the same kind of sensitive content — a stored
    # subject_identifier, and a free-text outcome_grounds that is often the
    # DPO's own reasoning for refusing a Kenyan data subject's request — so
    # the same logic applies to it, not to the discovery-monitor precedent
    # this scope was originally modelled on (a monitor carries no subject
    # identities at all). PENDING A PRODUCT RULING: Owner and Contributor
    # still get PRIVACYCARE_DSR_READ (and PRIVACYCARE_DSR_UPDATE) by
    # registry derivation below — this omission narrows only Viewer.
    #
    # PrivacyCare (spec 2026-09-13 D-CON-1): PRIVACYCARE_CONSENT_READ is
    # likewise intentionally NOT here, for the same reasoning as
    # PRIVACYCARE_DSR_READ immediately above rather than the
    # PRIVACYCARE_DISCOVERY_READ precedent it was modelled on further up —
    # the stale-consent report names a data subject (email, device id, or
    # external id) and what they consented to, the same kind of sensitive,
    # subject-identifying content upstream withholds PRIVACY_REQUEST_READ
    # from Viewer for. Owner and Contributor still get
    # PRIVACYCARE_CONSENT_READ by registry derivation below (there is no
    # PRIVACYCARE_CONSENT_UPDATE to omit — the route it guards exposes no
    # write); this omission narrows only Viewer. Not imported into this
    # module at all: unlike PRIVACYCARE_DISCOVERY_READ and
    # PRIVACYCARE_DSR_READ (each named, even if only in a comment, next to
    # an explicit list above), PRIVACYCARE_CONSENT_READ is referenced by
    # name in no list here — Owner and Contributor pick it up purely
    # through SCOPE_REGISTRY / not_contributor_scopes below.
    #
    # PrivacyCare (spec 2026-09-16 D-W2-2): PRIVACYCARE_RISK_READ IS granted
    # to Viewer here, deliberately UNLIKE PRIVACYCARE_DSR_READ and
    # PRIVACYCARE_CONSENT_READ immediately above — this is the
    # PRIVACYCARE_DISCOVERY_READ precedent, not theirs. A DPIA risk register
    # row (risk/register.py's RiskEntry) carries a category, a free-text
    # description of the risk, a likelihood and a severity; it never names a
    # data subject, unlike a DSR register row (a subject_identifier and a
    # DPO's own refusal reasoning) or a stale-consent finding (an email,
    # device id or external id). There is nothing subject-identifying here
    # for Viewer to be withheld from, so — same as PRIVACYCARE_DISCOVERY_READ
    # further up — it is granted outright rather than left to registry
    # derivation. The WRITE scope, PRIVACYCARE_RISK_CREATE, is not listed
    # here and is not imported into this module at all: Owner and
    # Contributor still get it by registry derivation below, same as every
    # other write scope this list omits.
    #
    # Final whole-branch review, minor finding: "never names a data
    # subject" is a CONVENTION, not a guarantee — description is
    # unconstrained Text (models.py's dpia_risk_table) that a DPO types
    # prose into, unlike a DSR row's structurally-typed identifier, and
    # nothing stops someone from pasting a name into it. No escalation
    # exists today because of this (the PDF route that exposes the same
    # description text is separately gated behind SYSTEM_READ, which
    # Viewer already holds via this same list), but this grant should not
    # be read as a stronger promise than the data model actually enforces.
    PRIVACYCARE_RISK_READ,
    RULE_READ,
    SCOPE_READ,
    STORAGE_READ,
    SYSTEM_INTEGRATION_LINK_READ,
    SYSTEM_READ,
    MESSAGING_READ,
    WEBHOOK_READ,
    SYSTEM_MANAGER_READ,
    SAAS_CONFIG_READ,
    USER_READ,
]

respondent_scopes = [
    PRIVACY_REQUEST_MANUAL_STEPS_RESPOND,  # allows respondents to respond to assigned manual steps
    USER_READ_OWN,
]

external_respondent_scopes = [
    PRIVACY_REQUEST_MANUAL_STEPS_RESPOND,  # allows external respondents to respond to assigned manual steps
]

data_steward_scopes = viewer_scopes + [
    SYSTEM_INTEGRATION_LINK_CREATE_OR_UPDATE,
    SYSTEM_INTEGRATION_LINK_DELETE,
]

not_contributor_scopes = [
    CONNECTOR_TEMPLATE_REGISTER,
    STORAGE_CREATE_OR_UPDATE,
    STORAGE_DELETE,
    MESSAGING_CREATE_OR_UPDATE,
    MESSAGING_DELETE,
    PRIVACY_REQUEST_NOTIFICATIONS_CREATE_OR_UPDATE,
    PRIVACY_REQUEST_EMAIL_INTEGRATIONS_SEND,
    PRIVACY_REQUEST_IMPORT,
    USER_PERMISSION_ASSIGN_OWNERS,
    HEAP_DUMP_EXEC,
]

ROLES_TO_SCOPES_MAPPING: Dict[str, List] = {
    OWNER: sorted(SCOPE_REGISTRY),
    VIEWER_AND_APPROVER: sorted(list(set(viewer_scopes + approver_scopes))),
    VIEWER: sorted(viewer_scopes),
    APPROVER: sorted(approver_scopes),
    CONTRIBUTOR: sorted(list(set(SCOPE_REGISTRY) - set(not_contributor_scopes))),
    DATA_STEWARD: sorted(list(set(data_steward_scopes))),
    RESPONDENT: sorted(respondent_scopes),
    EXTERNAL_RESPONDENT: sorted(external_respondent_scopes),
}


def get_scopes_from_roles(roles: Optional[List[str]]) -> List[str]:
    """Return a list of all the scopes the user has via their role(s)"""
    if not roles:
        return []

    scope_list: List[str] = []
    for role in roles:
        scope_list += ROLES_TO_SCOPES_MAPPING.get(role, [])
    return [*set(scope_list)]
