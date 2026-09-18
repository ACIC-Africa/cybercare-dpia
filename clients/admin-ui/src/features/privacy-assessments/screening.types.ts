/**
 * PrivacyCare (spec 2026-09-16 D-W2-7g)
 *
 * Hand-authored TS twins of the screening gate's HTTP surface
 * (`src/fides/api/privacycare/api/screening_schemas.py`). Not generated from
 * the OpenAPI schema into `~/types/api` — screening_schemas.py itself notes
 * "No TypeScript counterpart" on every one of these models and points at
 * `tests/privacycare/test_response_model_ts_parity.py`'s ALLOWLIST for the
 * reason: this whole surface is PrivacyCare's own (spec D-W2-7), with no
 * shipped-Fides screen depending on it, so parity enforcement was deferred
 * until a screen existed to build against. This file, and this task, is
 * that screen — the interfaces below match the Pydantic models field for
 * field, on purpose, the same discipline
 * `~/features/privacycare/processing-grounds.slice.ts` documents for its own
 * hand-authored types.
 */

/** gate.list_triggers' own row shape. Fetched, never hardcoded — these are
 * the privacy SME's own words and she revises them. */
export interface ScreeningTrigger {
  id: string;
  trigger_key: string;
  label: string;
  description: string;
  display_order: number;
}

export interface TriggerListResponse {
  triggers: ScreeningTrigger[];
}

/** POST body for recording a decision. No dpia_required field, deliberately
 * — the server derives it from whether triggered_keys is non-empty. */
export interface ScreeningDecisionRequest {
  triggered_keys: string[];
  justification?: string | null;
}

/** Mirrors gate.ScreeningVerdict. No `id` — a caller has nothing to refer
 * back to a specific decision by other than business_process_id + decided_at.
 *
 * decided_by / decided_by_display (screening_schemas.py's own comment):
 * decided_by is the stored audit identifier — the compliance artifact a
 * regulator asks for — and is NEVER a name; decided_by_display is resolved
 * server-side at read time (a fidesuser's name, or an honest label for a
 * system client / a since-deleted user) purely for showing to a person.
 * Render decided_by_display, keep decided_by discoverable (tooltip / the
 * expanded row), never hide the raw identifier entirely. */
export interface ScreeningVerdictResponse {
  business_process_id: string;
  dpia_required: boolean;
  triggered_keys: string[];
  justification: string | null;
  decided_by: string;
  decided_by_display: string;
  decided_at: string;
}

export interface ScreeningHistoryResponse {
  business_process_id: string;
  decisions: ScreeningVerdictResponse[];
}

/** One row of GET /api/v1/privacycare/screening. dpia_required, decided_by
 * and decided_at are None together, always: a process never screened has no
 * verdict, no decider, no timestamp. has_mapping is true only when the link
 * resolves to a LIVE privacydeclaration row. */
export interface ScreeningStatusResponse {
  business_process_id: string;
  name: string;
  business_cycle: string | null;
  dpia_required: boolean | null;
  // decided_by / decided_by_display: same audit-vs-display split as
  // ScreeningVerdictResponse above — null together, always, for a process
  // that has never been screened.
  decided_by: string | null;
  decided_by_display: string | null;
  decided_at: string | null;
  has_mapping: boolean;
}

export interface ScreeningListResponse {
  processes: ScreeningStatusResponse[];
}

/** POST .../mapping. name/data_categories are the only two always-required
 * fields; every other field is optional and means "not answered this call",
 * not "clear it" — an UPDATE only overwrites a field that is actually sent.
 * `ground` is the business situation's own text (e.g. "KYC Requirements"),
 * matching privacycare_processing_ground.ground verbatim — never the legal
 * basis, which the server derives and never accepts directly. `purpose` is a
 * ctl_data_uses fides_key, validated and rejected by name like the other
 * three vocabularies — never free text (fix wave, item C-2). Never send
 * dpia_required or processes_special_category_data: both are derived. */
export interface DataMappingRequest {
  name: string;
  data_categories: string[];
  data_subjects?: string[] | null;
  ground?: string | null;
  purpose?: string | null;
  retention_period?: string | null;
  third_parties?: string | null;
}

/** Mirrors screening/mapping.MappingResult. `ground` echoes only what THIS
 * call was given (null if this call did not name one, even if an earlier
 * call already derived and persisted a legal basis) — fides_legal_basis
 * always reflects the current persisted value. `created` distinguishes "this
 * call made the activity" from "this call updated the one this route already
 * owns". */
export interface DataMappingResponse {
  business_process_id: string;
  privacy_declaration_id: string;
  system_id: string;
  name: string;
  data_subjects: string[];
  data_categories: string[];
  ground: string | null;
  fides_legal_basis: string | null;
  purpose: string | null;
  retention_period: string | null;
  third_parties: string | null;
  processes_special_category_data: boolean;
  created: boolean;
}

/** GET .../mapping. mapping is null for a business process with no activity
 * THIS ROUTE owns — never screened, or mapped only through an activity this
 * route did not create (has_mapping=true on the list, but this call still
 * comes back null). The screen must render that as a distinct, honest third
 * state, never as an empty form: an officer who re-types a mapping they
 * cannot see creates a THIRD activity and overwrites nothing visible. */
export interface MappingReadResponse {
  business_process_id: string;
  mapping: DataMappingResponse | null;
}

/**
 * PrivacyCare (spec 2026-09-13 D-KT-5): a processing ground, echoed here
 * from `~/features/privacycare/processing-grounds.slice`'s own
 * `ProcessingGroundResponse` so this module does not need to import a
 * sibling slice's response shape just to type a prop.
 */
export interface ProcessingGround {
  id: string;
  ground: string;
  fides_legal_basis: string;
}

/** Computed, display-only status derived from ScreeningStatusResponse —
 * never a wire type. "applicable" / "not applicable" are the SME's own
 * words (Carol, 2026-09-17); never "screened out". */
export type ScreeningDisplayStatus =
  | "not_screened"
  | "applicable"
  | "not_applicable";

export const screeningDisplayStatus = (
  row: Pick<ScreeningStatusResponse, "dpia_required">,
): ScreeningDisplayStatus => {
  if (row.dpia_required === null) {
    return "not_screened";
  }
  return row.dpia_required ? "applicable" : "not_applicable";
};
