/**
 * PrivacyCare — Record of processing activities (ROPA), Screen 3 of
 * docs/design/privacycare-screens/DESIGN.md.
 *
 * Hand-authored TS twins of `src/fides/api/privacycare/api/processes.py`'s
 * response models, the same discipline `screening.types.ts` and
 * `risk.types.ts` document for their own sibling surfaces: this whole
 * surface is PrivacyCare's own, generated into no OpenAPI client, so a
 * field renamed on the Python side has nothing on this side to fail loudly
 * — matched by hand, field for field, against processes.py as read at
 * build time.
 */

/** processes.py's BusinessProcessResponse, field for field. Every field
 * that is Optional[...] in Python is `| null` here, not `?:` — the API
 * always sends the key, with an explicit null when unset, never omits it. */
export interface BusinessProcessResponse {
  id: string;
  name: string;
  description: string | null;
  business_cycle: string | null;
  owner_name: string | null;
  owner_email: string | null;
  is_critical: boolean;
  criticality_note: string | null;
  external_ref: string | null;
  last_attested_at: string | null;
  created_at: string | null;
  updated_at: string | null;
}

/** GET /privacycare/business-processes — fastapi_pagination's Page[]
 * envelope (items/total/page/size/pages), the same shape risk.types.ts'
 * own RiskListResponse documents for its sibling list route. */
export interface BusinessProcessListResponse {
  items: BusinessProcessResponse[];
  total: number;
  page: number;
  size: number;
  pages: number;
}

/** processes.py's RopaDeclarationResponse. Every field here is a raw
 * taxonomy key or id — data_use/data_categories/data_subjects are NEVER
 * shown to a person without going through useTaxonomies' display-name
 * lookup first (DESIGN.md, Screen 3: "Never a raw fides key"). */
export interface RopaDeclarationResponse {
  id: string;
  name: string | null;
  data_use: string;
  data_categories: string[];
  data_subjects: string[];
  legal_basis: string | null;
  retention_period: string | null;
  system_id: string | null;
  system_name: string | null;
}

/** GET /privacycare/business-processes/{process_id}/ropa — processes.py's
 * RopaEntryResponse. `missing_declarations` is a list of
 * privacy_declaration_ids that this process links to but that no longer
 * resolve to a live `privacydeclaration` row — ropa.py's own module
 * docstring: rendered as a named gap, never dropped silently. */
export interface RopaEntryResponse {
  process: BusinessProcessResponse;
  declarations: RopaDeclarationResponse[];
  missing_declarations: string[];
}
