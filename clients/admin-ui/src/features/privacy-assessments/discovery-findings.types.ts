/**
 * PrivacyCare — Discovery, Screen 4 of docs/design/privacycare-screens/DESIGN.md.
 *
 * Hand-authored TS twins of `src/fides/api/privacycare/api/discovery_schemas.py`,
 * the same discipline `screening.types.ts` and `ropa.types.ts` document for
 * their own sibling surfaces: this whole surface is PrivacyCare's own,
 * generated into no OpenAPI client (discovery-findings-api-report.md's own
 * words: "no shipped screen calls this yet"), so a field renamed on the
 * Python side has nothing on this side to fail loudly — matched by hand,
 * field for field, against discovery_schemas.py as read at build time.
 */

/** discovery/findings.py's own three states. "needs_review" is never a
 * stored value on the server — it is the absence of a reconciliation row,
 * turned into this name by list_findings/_state_for. The client never
 * writes this value: ReconcileFindingRequest.state is restricted to the
 * other two. */
export type FindingState = "mapped" | "ignored" | "needs_review";

/** discovery_schemas.py's FindingResponse. One discovered TABLE (never a
 * Field — 1703 of those is not a work queue; see the Python module's own
 * docstring). system_id/system_name/reason/decided_by/decided_at are all
 * null together for a "needs_review" finding — same "null means never
 * decided" contract ScreeningStatusResponse keeps for dpia_required. */
export interface FindingResponse {
  urn: string;
  table_name: string;
  schema_name: string;
  monitor_key: string;
  field_count: number;
  table_type: string | null;
  diff_status: string | null;
  state: FindingState;
  system_id: string | null;
  system_name: string | null;
  reason: string | null;
  decided_by: string | null;
  decided_at: string | null;
}

/** GET /privacycare/discovery — one call for every discovered table,
 * same "not 176 round trips" discipline discovery/findings.py's own module
 * docstring documents for _LIST_FINDINGS_SQL. A plain envelope, not a
 * fastapi_pagination Page — discovery_schemas.py's own FindingListResponse
 * comment: this list exists precisely so a caller does not paginate
 * through per-row fetches. */
export interface FindingListResponse {
  findings: FindingResponse[];
}

/** POST /privacycare/discovery/{urn}/reconcile body. Validated inside
 * discovery.findings.reconcile_finding and api/discovery.py's own
 * _resolve_system_id, not re-checked here — same discipline
 * ScreeningDecisionRequest documents for its own server-validated fields.
 *
 * `system_fides_key` is what this screen's picker sends (2026-09-18 fix):
 * the identifier every other system picker in this codebase (SystemSelect,
 * GET /system) actually exposes, resolved to the internal
 * `ctl_systems.id` server-side. `system_id` is that internal id directly
 * — kept on the wire for API back-compat, but nothing in the admin UI
 * sends it; give at most one. */
export interface ReconcileFindingRequest {
  state: "mapped" | "ignored";
  system_id?: string | null;
  system_fides_key?: string | null;
  reason?: string | null;
}

/** discovery_schemas.py's ReconciliationResponse. Mirrors
 * discovery.findings.Reconciliation field for field. Never
 * state="needs_review" — this always describes a row that was actually
 * written. */
export interface ReconciliationResponse {
  id: string;
  urn: string;
  state: "mapped" | "ignored";
  system_id: string | null;
  system_name: string | null;
  reason: string | null;
  decided_by: string;
  decided_at: string;
}

/** GET /privacycare/discovery/{urn}/history. Empty reconciliations for a
 * real, currently-discovered table nobody has reconciled yet — the 404 for
 * an unknown urn happens ahead of this response, at the HTTP layer. */
export interface FindingHistoryResponse {
  urn: string;
  reconciliations: ReconciliationResponse[];
}
