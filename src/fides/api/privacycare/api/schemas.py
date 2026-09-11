# Response schemas for the DPIA read surface.
#
# Field names and optionality are copied from the generated TypeScript types in
# clients/admin-ui/src/types/api/models/. The shipped admin-UI is compiled
# against those types, so this file does not get to choose its own shape.
import re
from typing import List, Optional

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


def template_key(name: str) -> str:
    # Derive the `key` the UI contract requires but the table does not store.
    # Lowercase, non-alphanumerics collapsed to underscores, trimmed.
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
