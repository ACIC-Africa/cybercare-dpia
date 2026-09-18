/**
 * PrivacyCare (spec 2026-09-16 D-W2-7g)
 *
 * RTK Query slice for the DPIA risk register's HTTP surface
 * (`src/fides/api/privacycare/api/risk.py`). Follows the same
 * `baseApi.injectEndpoints` pattern as the sibling `screening.slice.ts` and
 * `~/features/privacy-assessments/privacy-assessments.slice.ts` — no
 * store.ts registration needed, injectEndpoints only adds to the
 * already-registered baseApi.
 *
 * list/getOdpcFinding both 404 on an unknown assessment_id rather than
 * silently reporting an empty/low register (risk.py's own module
 * docstring, "THE PARKED TASK-3 FINDING") — this slice does not
 * special-case that; it is a real error and the call site renders it
 * through the same inline-error-with-retry path any other query failure
 * gets, never a silently blank register.
 */
import { baseApi } from "~/features/common/api.slice";

import {
  OdpcFindingResponse,
  RemoveRiskResponse,
  RiskCreate,
  RiskListResponse,
  RiskResponse,
} from "./risk.types";

const riskApi = baseApi.injectEndpoints({
  overrideExisting: true,
  endpoints: (build) => ({
    // register.list_risks' own ordering — highest score first — is not
    // re-sorted client-side (DESIGN.md: "the risk that set the band sits
    // where the eye goes"). size: 100 because a DPIA's register is a
    // handful of rows, not a paginated dataset (same "one call, not a
    // round trip per row" reasoning screening.slice.ts documents for its
    // own list route, just at a much smaller scale here).
    listRisks: build.query<RiskListResponse, string>({
      query: (assessmentId) => ({
        url: `privacycare/risk/${assessmentId}`,
        params: { size: 100 },
      }),
      providesTags: (_result, _error, assessmentId) => [
        { type: "PrivacyCare Risk", id: assessmentId },
      ],
    }),

    addRisk: build.mutation<
      RiskResponse,
      { assessmentId: string; body: RiskCreate }
    >({
      query: ({ assessmentId, body }) => ({
        url: `privacycare/risk/${assessmentId}`,
        method: "POST",
        body,
      }),
      invalidatesTags: (_result, _error, { assessmentId }) => [
        { type: "PrivacyCare Risk", id: assessmentId },
        { type: "PrivacyCare Risk ODPC", id: assessmentId },
      ],
    }),

    // risk.py's DELETE route is keyed by risk_id alone; assessmentId is
    // carried here purely so this slice knows which register/finding cache
    // entries to invalidate.
    removeRisk: build.mutation<
      RemoveRiskResponse,
      { riskId: string; assessmentId: string }
    >({
      query: ({ riskId }) => ({
        url: `privacycare/risk/${riskId}`,
        method: "DELETE",
      }),
      invalidatesTags: (_result, _error, { assessmentId }) => [
        { type: "PrivacyCare Risk", id: assessmentId },
        { type: "PrivacyCare Risk ODPC", id: assessmentId },
      ],
    }),

    // Only for required/window_days/reason — NOT the source the section's
    // own band tag reads (see RiskRegisterSection.tsx). highest_risk here
    // is null whenever required is false (risk/odpc.py's evaluate), by
    // design; the section's "highest risk, named" line is sourced from
    // listRisks instead, for exactly that reason.
    getOdpcFinding: build.query<OdpcFindingResponse, string>({
      query: (assessmentId) => ({
        url: `privacycare/risk/${assessmentId}/odpc`,
      }),
      providesTags: (_result, _error, assessmentId) => [
        { type: "PrivacyCare Risk ODPC", id: assessmentId },
      ],
    }),
  }),
});

export const {
  useListRisksQuery,
  useAddRiskMutation,
  useRemoveRiskMutation,
  useGetOdpcFindingQuery,
} = riskApi;

export { riskApi };
