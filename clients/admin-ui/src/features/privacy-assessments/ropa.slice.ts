/**
 * PrivacyCare — Record of processing activities (ROPA), Screen 3 of
 * docs/design/privacycare-screens/DESIGN.md.
 *
 * RTK Query slice for `src/fides/api/privacycare/api/processes.py`'s HTTP
 * surface. Follows the same `baseApi.injectEndpoints` pattern as the
 * sibling `screening.slice.ts` and `risk.slice.ts` — no store.ts
 * registration needed.
 *
 * SCOPE. Both routes below are gated by Fides' own SYSTEM_READ in
 * processes.py, not by any PRIVACYCARE_* scope — DESIGN.md's own words for
 * this screen ("Read-only for everyone who can read screening") describe
 * the *intent*, not the literal scope the API enforces. In practice this
 * makes no difference: roles.py grants SYSTEM_READ and
 * PRIVACYCARE_SCREENING_READ to Viewer side by side (and Contributor/Owner
 * hold both by registry derivation), so every account that can read
 * screening can also read this. Named here, and in the build report, as a
 * design/codebase mismatch rather than silently resolved either way.
 *
 * size: 100 on getBusinessProcesses — same "one call, not 87 round trips"
 * reasoning screening.slice.ts documents for its own list route. 100 is a
 * deliberate margin over the customer's real 87.
 */
import { baseApi } from "~/features/common/api.slice";

import { BusinessProcessListResponse, RopaEntryResponse } from "./ropa.types";

const ropaApi = baseApi.injectEndpoints({
  overrideExisting: true,
  endpoints: (build) => ({
    // Every business process — the register itself — in one call. Provides
    // the entity this screen lists; the "has this been screened" column is
    // enriched from screening.slice.ts's own getScreeningStatuses, a
    // second, independent call this screen tolerates failing (see
    // RopaList.tsx) rather than depend on for its own core listing.
    getBusinessProcesses: build.query<BusinessProcessListResponse, void>({
      query: () => ({
        url: "privacycare/business-processes",
        params: { size: 100 },
      }),
      providesTags: ["PrivacyCare Ropa Processes"],
    }),

    // One business process's assembled ROPA entry — the process record
    // plus every linked processing activity, and any dangling link
    // (missing_declarations) reported rather than dropped. Used both by
    // RopaEntry.tsx (one call, already on screen) and, per-row, by
    // RopaList.tsx to show an accurate processing-activity count bounded
    // to the rows currently visible on the table's own page (never all 87
    // at once — see RopaProcessingActivityCount.tsx).
    getProcessRopa: build.query<RopaEntryResponse, string>({
      query: (businessProcessId) => ({
        url: `privacycare/business-processes/${businessProcessId}/ropa`,
      }),
      providesTags: (_result, _error, businessProcessId) => [
        { type: "PrivacyCare Ropa Entry", id: businessProcessId },
      ],
    }),
  }),
});

export const { useGetBusinessProcessesQuery, useGetProcessRopaQuery } = ropaApi;

export { ropaApi };
