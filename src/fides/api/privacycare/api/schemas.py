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


class EvidenceItem(BaseModel):
    # Mirrors EvidenceItem in
    # clients/admin-ui/src/features/privacy-assessments/types.ts.
    #
    # `answer_version.evidence` is JSONB, NOT NULL, server_default '{}' (an
    # empty *object*, not an array) — verified against the live migration
    # (xx_2026_02_05_1500_b2c3d4e5f6g7_add_privacy_assessment_schema.py).
    # No write path in this repo populates it yet (the writer lands in plan
    # 04) and the live `fides` database has zero answer_version rows, so
    # this shape is matched against the UI contract, not against observed
    # data — see task-5-report.md for what was actually checked.
    id: str
    type: str
    value: Optional[str] = None
    created_at: str
    field_name: Optional[str] = None
    source_key: Optional[str] = None
    source_type: Optional[str] = None
    # TS types this `number | null` with no `?` — the key is always present,
    # only its value may be null. No default: an omitted key must fail
    # validation the same way it would fail the UI's required-field parity.
    citation_number: Optional[int]
    data: Optional[Dict] = None


class QuestionEvidence(BaseModel):
    # Mirrors QuestionEvidence in the same feature types.ts file. Exists for
    # AssessmentEvidenceResponse.by_question field-for-field parity; nothing
    # in this plan populates it (see AssessmentEvidenceResponse below).
    question_id: str
    question_text: str
    evidence: List[EvidenceItem]


class AssessmentEvidenceResponse(BaseModel):
    # Mirrors AssessmentEvidenceResponse in the same feature types.ts file.
    # by_question/by_type (driven by GetAssessmentEvidenceParams.group_by)
    # are left unpopulated: useGetAssessmentEvidenceQuery is exported from
    # the RTK slice but no shipped UI component calls it yet (AssessmentDetail
    # derives evidence client-side from question_groups instead), so there is
    # no consumer to validate a grouping implementation against. Only the
    # flat `items` list — the shape every other EvidenceItem consumer
    # (EvidenceCardGroup, EvidenceDrawer) actually reads — is populated here.
    assessment_id: str
    total_count: int
    by_question: Optional[Dict[str, QuestionEvidence]] = None
    by_type: Optional[Dict[str, List[EvidenceItem]]] = None
    items: Optional[List[EvidenceItem]] = None


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
