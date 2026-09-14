/**
 * PrivacyCare (spec 2026-09-13 D-KT-5)
 *
 * RTK Query slice for the Kenyan processing-grounds surface
 * (`src/fides/api/privacycare/api/grounds.py`). Follows the same
 * `baseApi.injectEndpoints` pattern as
 * `~/features/privacy-assessments/privacy-assessments.slice.ts` — no
 * store.ts registration is needed because `baseApi` is already registered
 * there and `injectEndpoints` only adds endpoints to it.
 *
 * These response shapes are not generated from the OpenAPI schema into
 * `~/types/api` — they are hand-authored here, and the interface names match
 * the Pydantic response models in grounds.py one-for-one on purpose:
 * tests/privacycare/test_response_model_ts_parity.py walks every
 * response_model on this surface and requires a same-named TS counterpart,
 * and test_api_schemas.py asserts the fields and optionality of each pair.
 * Add a field to a model in grounds.py without adding it here and those
 * tests fail — which is the point.
 */
import { baseApi } from "~/features/common/api.slice";

export interface ProcessingGroundResponse {
  id: string;
  ground: string;
  fides_legal_basis: string;
}

export interface ProcessingGroundListResponse {
  grounds: ProcessingGroundResponse[];
  unmapped_count: number;
}

export interface DeclarationGroundResponse {
  privacy_declaration_id: string;
  processing_ground_id: string;
  fides_legal_basis: string;
}

export interface SetDeclarationGroundRequest {
  id: string;
  processing_ground_id: string;
}

const processingGroundsApi = baseApi.injectEndpoints({
  overrideExisting: true,
  endpoints: (build) => ({
    getProcessingGrounds: build.query<ProcessingGroundListResponse, void>({
      query: () => ({
        url: "privacycare/processing-grounds",
      }),
    }),

    getDeclarationGround: build.query<DeclarationGroundResponse, string>({
      query: (declarationId) => ({
        url: `privacycare/declarations/${declarationId}/ground`,
      }),
    }),

    setDeclarationGround: build.mutation<
      DeclarationGroundResponse,
      SetDeclarationGroundRequest
    >({
      query: ({ id, processing_ground_id }) => ({
        url: `privacycare/declarations/${id}/ground`,
        method: "PUT",
        body: { processing_ground_id },
      }),
    }),
  }),
});

export const {
  useGetProcessingGroundsQuery,
  useGetDeclarationGroundQuery,
  useSetDeclarationGroundMutation,
} = processingGroundsApi;

export { processingGroundsApi };
