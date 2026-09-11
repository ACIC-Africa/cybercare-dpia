# Response schemas for the DPIA read surface.
#
# Field names and optionality are copied from the generated TypeScript types in
# clients/admin-ui/src/types/api/models/. The shipped admin-UI is compiled
# against those types, so this file does not get to choose its own shape.
import re
from typing import Dict, List, Optional

from pydantic import BaseModel


class AssessmentResponse(BaseModel):
    id: str
    template_id: str
    template_name: Optional[str] = None
    name: str
    status: str
    completeness: Optional[float] = 0.0
    risk_level: Optional[str] = None
    system_fides_key: Optional[str] = None
    system_name: Optional[str] = None
    declaration_id: Optional[str] = None
    declaration_name: Optional[str] = None
    data_use: Optional[str] = None
    data_use_name: Optional[str] = None
    data_categories: Optional[List[str]] = None
    created_by: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class TemplateResponse(BaseModel):
    # `key` has NO column in assessment_template (verified against the live
    # schema). The commercial API must derive it. We derive a stable slug from
    # the template name; see template_key() below.
    id: str
    key: str
    version: str
    name: str
    assessment_type: Optional[str] = None
    region: Optional[str] = None
    authority: Optional[str] = None
    legal_reference: Optional[str] = None
    description: Optional[str] = None
    is_active: Optional[bool] = True


class AssessmentSummaryBlockedGroup(BaseModel):
    # Mirrors AssessmentSummaryBlockedGroup in
    # clients/admin-ui/src/features/privacy-assessments/types.ts. This type is
    # hand-authored in the admin-UI feature module, NOT generated into
    # clients/admin-ui/src/types/api/models/ — do not look for it there.
    name: str
    outdated_count: int
    high_risk_count: int
    total_count: int


class AssessmentSummaryOwner(BaseModel):
    # Mirrors AssessmentSummaryOwner in the same feature types.ts file.
    owner: str
    open_count: int
    outdated_count: int


class AssessmentSummaryResponse(BaseModel):
    # Mirrors AssessmentSummaryResponse in the same feature types.ts file.
    # Fix-round-1 finding: an earlier draft of this endpoint invented a
    # {total, by_status, by_risk_level} shape instead of reading this
    # contract. by_segment buckets into exactly the 4
    # AssessmentSummarySegment values ("completed" | "pending" | "open" |
    # "risk"); see _summary() in api/assessments.py for the derivation,
    # which follows clients/admin-ui/src/mocks/privacy-assessments/
    # compute-summary.ts (the shipped reference implementation).
    total: int
    by_segment: Dict[str, int]
    blocked_groups: List[AssessmentSummaryBlockedGroup]
    owners: List[AssessmentSummaryOwner]


def template_key(name: str, id: Optional[str] = None) -> str:
    # Derive the `key` the UI contract requires but the table does not store.
    # Lowercase, non-alphanumerics collapsed to underscores, trimmed.
    #
    # A name that is empty or made entirely of punctuation (`""`, `"!!!"`,
    # `"---"`) collapses to "" here. An empty string satisfies Pydantic's bare
    # `str`, so it would pass silently and every such template would collide
    # on the same key. Guarantee a non-empty, stable result instead: fall
    # back to a slug of `id` (always present, always unique), and only fall
    # back further to a fixed placeholder if even that yields nothing.
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    if slug:
        return slug
    if id:
        id_slug = re.sub(r"[^a-z0-9]+", "_", str(id).lower()).strip("_")
        if id_slug:
            return id_slug
    return "template"
