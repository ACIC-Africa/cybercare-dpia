/**
 * PrivacyCare (spec 2026-09-16 D-W2-7g)
 *
 * RTK Query slice for the DPIA screening gate's HTTP surface
 * (`src/fides/api/privacycare/api/screening.py`). Follows the same
 * `baseApi.injectEndpoints` pattern as the sibling
 * `~/features/privacy-assessments/privacy-assessments.slice.ts` and
 * `~/features/privacycare/processing-grounds.slice.ts` — no store.ts
 * registration needed, `injectEndpoints` only adds to the already-registered
 * `baseApi`.
 *
 * Routes are keyed to {business_process_id}, not a processing activity —
 * see screening.py's own module docstring for the re-key rationale
 * (86 real business processes vs. 2 invented activities).
 */
import { baseApi } from "~/features/common/api.slice";

import {
  DataMappingRequest,
  DataMappingResponse,
  MappingReadResponse,
  ScreeningDecisionRequest,
  ScreeningHistoryResponse,
  ScreeningListResponse,
  ScreeningVerdictResponse,
  TriggerListResponse,
} from "./screening.types";

const screeningApi = baseApi.injectEndpoints({
  overrideExisting: true,
  endpoints: (build) => ({
    // Every business process (86+) with its screening status, in ONE call —
    // screening.py's own module docstring: a per-row fetch here is a defect,
    // not an optimisation opportunity.
    getScreeningStatuses: build.query<ScreeningListResponse, void>({
      query: () => ({ url: "privacycare/screening" }),
      providesTags: ["PrivacyCare Screening"],
    }),

    // Carol's own six questions, fetched — never hardcoded. She revises
    // these; a copy in this component would drift the moment she does.
    getScreeningTriggers: build.query<TriggerListResponse, void>({
      query: () => ({ url: "privacycare/screening/triggers" }),
    }),

    // Every decision ever recorded for one business process, newest first.
    // Empty, not 404, for a real process that has never been screened; 404
    // for an unknown business_process_id — getErrorMessage/isAPIError at the
    // call site distinguish the two, same discipline the mapping read below
    // documents for its own null-vs-404 split.
    getScreeningHistory: build.query<ScreeningHistoryResponse, string>({
      query: (businessProcessId) => ({
        url: `privacycare/screening/${businessProcessId}/history`,
      }),
      providesTags: (_result, _error, businessProcessId) => [
        { type: "PrivacyCare Screening History", id: businessProcessId },
      ],
    }),

    // Re-screening APPENDS (gate.record_decision's own docstring) — there is
    // no update path. Never send dpia_required: the server derives it from
    // triggered_keys, and a client that could set it directly could tick
    // three questions and also declare no assessment needed.
    recordScreeningDecision: build.mutation<
      ScreeningVerdictResponse,
      { businessProcessId: string; body: ScreeningDecisionRequest }
    >({
      query: ({ businessProcessId, body }) => ({
        url: `privacycare/screening/${businessProcessId}`,
        method: "POST",
        body,
      }),
      invalidatesTags: (_result, _error, { businessProcessId }) => [
        "PrivacyCare Screening",
        { type: "PrivacyCare Screening History", id: businessProcessId },
      ],
    }),

    // Reads back ONLY the activity this route owns for this business
    // process, or an explicit null — never the activity of a pre-existing,
    // foreign-owned mapping. A null here alongside has_mapping=true on the
    // list is a real, different state the screen must render honestly.
    getDataMapping: build.query<MappingReadResponse, string>({
      query: (businessProcessId) => ({
        url: `privacycare/screening/${businessProcessId}/mapping`,
      }),
      providesTags: (_result, _error, businessProcessId) => [
        { type: "PrivacyCare Screening Mapping", id: businessProcessId },
      ],
    }),

    // Creates a processing activity (or updates the one this route already
    // owns) and links it to the business process. Saveable incomplete and
    // returned to — only name + at least one data category are required.
    saveDataMapping: build.mutation<
      DataMappingResponse,
      { businessProcessId: string; body: DataMappingRequest }
    >({
      query: ({ businessProcessId, body }) => ({
        url: `privacycare/screening/${businessProcessId}/mapping`,
        method: "POST",
        body,
      }),
      invalidatesTags: (_result, _error, { businessProcessId }) => [
        "PrivacyCare Screening",
        { type: "PrivacyCare Screening Mapping", id: businessProcessId },
      ],
    }),
  }),
});

export const {
  useGetScreeningStatusesQuery,
  useGetScreeningTriggersQuery,
  useGetScreeningHistoryQuery,
  useRecordScreeningDecisionMutation,
  useGetDataMappingQuery,
  useLazyGetDataMappingQuery,
  useSaveDataMappingMutation,
} = screeningApi;

export { screeningApi };
