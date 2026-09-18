/**
 * PrivacyCare (spec 2026-09-16 D-W2-7g)
 *
 * Hand-authored TS twins of the DPIA risk register's HTTP surface
 * (`src/fides/api/privacycare/api/risk_schemas.py`). Not generated into
 * `~/types/api` — this whole surface is PrivacyCare's own, with no
 * shipped-Fides screen depending on it, same discipline screening.types.ts
 * documents for its own sibling surface.
 *
 * Fix wave (Screen 2 review), finding 2: this file's own comment used to
 * say parity enforcement was DEFERRED until a screen existed to build
 * against ("this file, and this screen, is that"). The screen
 * (RiskRegisterSection.tsx and friends) now exists and reads every one of
 * these interfaces, so the precondition that comment was waiting on is met
 * — RiskResponse, RemoveRiskResponse and OdpcFindingResponse are no longer
 * exempted in test_response_model_ts_parity.py's ALLOWLIST, and field
 * parity against risk_schemas.py is asserted in
 * tests/privacycare/test_risk_ts_parity.py. A field renamed on either side
 * without the other now fails a test instead of rendering blank on this
 * screen with nothing red anywhere.
 */

// The four risk bands (risk/banding.py's LOW/MEDIUM/HIGH/CRITICAL
// constants), lowercase exactly as banding.band() returns them on the wire.
export enum RiskBand {
  LOW = "low",
  MEDIUM = "medium",
  HIGH = "high",
  CRITICAL = "critical",
}

// Carol's seven risk categories (register.py's _CATEGORIES tuple),
// unchanged from the DPIA methodology — sector-neutral, never
// customer-specific. Order matches _CATEGORIES so the Add-risk Select
// offers them in the same order the backend documents them in.
export enum RiskCategory {
  CONFIDENTIALITY = "confidentiality",
  INTEGRITY = "integrity",
  AVAILABILITY = "availability",
  DISCRIMINATION = "discrimination",
  LOSS_OF_AUTONOMY = "loss_of_autonomy",
  FINANCIAL_OR_REPUTATIONAL_HARM = "financial_or_reputational_harm",
  PHYSICAL_HARM = "physical_harm",
}

/** POST body. Mirrors risk_schemas.RiskCreate field-for-field. category/
 * likelihood/severity are validated server-side inside register.add_risk,
 * not re-checked here — the Select/inputs this type backs only ever offer
 * valid values, but a 400 from the server (e.g. a category the client's own
 * enum has gone stale against) is still handled at the call site. */
export interface RiskCreate {
  category: RiskCategory;
  description: string;
  likelihood: number;
  severity: number;
}

/** Mirrors risk_schemas.RiskResponse field-for-field. score and band are
 * always the server's own computed values (risk/banding.py), never
 * recomputed or trusted from a stale client copy — see risk.utils.ts for
 * the ONE place this screen predicts a score/band the server has not yet
 * confirmed (a live preview while a user is still choosing, before submit). */
export interface RiskResponse {
  id: string;
  assessment_id: string;
  category: string;
  description: string;
  likelihood: number;
  severity: number;
  score: number;
  band: RiskBand;
}

/** GET /privacycare/risk/{assessment_id} — fastapi_pagination's Page[]
 * envelope, same shape as the generated Page_*_ types elsewhere in this
 * app (items/total/page/size/pages). Hand-authored here because
 * RiskResponse itself has no generated TS counterpart to page over. */
export interface RiskListResponse {
  items: RiskResponse[];
  total: number;
  page: number;
  size: number;
  pages: number;
}

/** DELETE .../risk/{risk_id}. `removed` is always true on a successful
 * response — the route maps an unknown risk_id to 404, not to
 * removed: false (risk_schemas.RemoveRiskResponse's own comment). */
export interface RemoveRiskResponse {
  id: string;
  removed: boolean;
}

/** GET .../risk/{assessment_id}/odpc. Mirrors risk_schemas.
 * OdpcFindingResponse. highest_risk is only ever populated when
 * required is true (risk/odpc.py's evaluate: "naming a risk on a finding
 * that says 'not required' would misleadingly suggest that risk was still
 * a live concern") — this screen's own "highest risk, named" summary line
 * does NOT read this field for that reason; see RiskRegisterSection.tsx,
 * which instead names the top entry of the (already highest-score-first)
 * risk list regardless of band. */
export interface OdpcFindingResponse {
  required: boolean;
  band: RiskBand;
  window_days: number;
  reason: string;
  highest_risk: RiskResponse | null;
}
