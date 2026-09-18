/**
 * PrivacyCare — Discovery, Screen 4 of docs/design/privacycare-screens/DESIGN.md.
 *
 * RTK Query slice for the discovery-FINDINGS surface
 * (`src/fides/api/privacycare/api/discovery.py`), mounted at PrivacyCare's
 * own `/api/v1/privacycare/discovery` — deliberately a SEPARATE slice from
 * `discovery.slice.ts`, which stays scoped to Ethyca's own
 * `/plus/discovery-monitor*` monitor-CONFIGURATION surface. The two are
 * different HTTP namespaces for different jobs (configuring/running a scan
 * vs. listing and deciding on what a scan found) — see
 * discovery-findings-api-report.md's own "NAMESPACE" section for why this
 * task's routes deliberately do not squat Plus's path.
 *
 * ONE CALL FOR EVERY DISCOVERED TABLE, same discipline screening.slice.ts's
 * getScreeningStatuses documents for its own 86+-row list: with 176 tables
 * already staged, a per-row fetch here would be a defect, not an
 * optimisation.
 */
import { baseApi } from "~/features/common/api.slice";

import {
  FindingHistoryResponse,
  FindingListResponse,
  FindingState,
  ReconcileFindingRequest,
  ReconciliationResponse,
} from "./discovery-findings.types";

const discoveryFindingsApi = baseApi.injectEndpoints({
  overrideExisting: true,
  endpoints: (build) => ({
    // Every discovered table, one row each, with its reconciliation state,
    // in ONE call. `state`, when given, filters server-side to exactly one
    // of "mapped" / "ignored" / "needs_review" — DESIGN.md: "Default the
    // list to Needs review, because that is the work."
    getDiscoveryFindings: build.query<
      FindingListResponse,
      { state?: FindingState | "all"; monitorKey?: string } | void
    >({
      query: (args) => {
        const state = args && args.state !== "all" ? args.state : undefined;
        return {
          url: "privacycare/discovery",
          params: {
            state,
            monitor_key: args?.monitorKey,
          },
        };
      },
      providesTags: ["PrivacyCare Discovery Findings"],
    }),

    // Every reconciliation ever recorded for one table, newest first. Empty
    // reconciliations for a real, currently-discovered table nobody has
    // reconciled yet (needs_review) — a genuine 404 for an unknown urn is a
    // different, real failure, told apart at the call site the same way
    // ScreeningHistoryPanel already does for its own history route.
    getDiscoveryFindingHistory: build.query<FindingHistoryResponse, string>({
      query: (urn) => ({
        url: `privacycare/discovery/${encodeURIComponent(urn)}/history`,
      }),
      providesTags: (_result, _error, urn) => [
        { type: "PrivacyCare Discovery Finding History", id: urn },
      ],
    }),

    // Records one reconciliation: mapped-to-a-system or ignored-with-a-
    // reason. APPENDS — findings.reconcile_finding's own docstring, no
    // update path exists to call. Invalidates the whole findings list
    // (this urn's state/decided_by/decided_at all change) and this urn's
    // own history.
    reconcileDiscoveryFinding: build.mutation<
      ReconciliationResponse,
      { urn: string; body: ReconcileFindingRequest }
    >({
      query: ({ urn, body }) => ({
        url: `privacycare/discovery/${encodeURIComponent(urn)}/reconcile`,
        method: "POST",
        body,
      }),
      invalidatesTags: (_result, _error, { urn }) => [
        "PrivacyCare Discovery Findings",
        { type: "PrivacyCare Discovery Finding History", id: urn },
      ],
    }),
  }),
});

export const {
  useGetDiscoveryFindingsQuery,
  useGetDiscoveryFindingHistoryQuery,
  useReconcileDiscoveryFindingMutation,
} = discoveryFindingsApi;

export { discoveryFindingsApi };
