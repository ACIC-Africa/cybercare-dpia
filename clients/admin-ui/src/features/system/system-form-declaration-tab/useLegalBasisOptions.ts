import { useMemo } from "react";

// PrivacyCare (spec 2026-09-13 D-KT-5)
import { useGetProcessingGroundsQuery } from "~/features/privacycare/processing-grounds.slice";
import { LegalBasisForProcessingEnum } from "~/types/api";

export interface LegalBasisOption {
  label: string;
  value: string;
  /**
   * PrivacyCare (spec 2026-09-13 D-KT-5): id of the Kenyan processing
   * ground this option came from. Undefined for the enum-derived fallback
   * options (while the grounds query is loading, or if it errors) — those
   * never produce a `setDeclarationGround` call.
   */
  groundId?: string;
}

const enumFallbackOptions: LegalBasisOption[] = (
  Object.keys(LegalBasisForProcessingEnum) as Array<
    keyof typeof LegalBasisForProcessingEnum
  >
).map((key) => ({
  value: LegalBasisForProcessingEnum[key],
  label: LegalBasisForProcessingEnum[key],
}));

const useLegalBasisOptions = () => {
  // PrivacyCare (spec 2026-09-13 D-KT-5): the dropdown now lists the
  // customer's own Kenyan processing grounds (e.g. "KYC Requirements")
  // rather than the raw Article 6 enum. While the query is loading, or if
  // it has no mapped grounds yet, fall back to the enum options so the
  // form never renders empty.
  const { data, isLoading } = useGetProcessingGroundsQuery();

  const legalBasisOptions = useMemo<LegalBasisOption[]>(() => {
    if (!isLoading && data?.grounds && data.grounds.length > 0) {
      return data.grounds.map((g) => ({
        label: g.ground,
        value: g.fides_legal_basis,
        groundId: g.id,
      }));
    }
    return enumFallbackOptions;
  }, [data, isLoading]);

  return { legalBasisOptions };
};

export default useLegalBasisOptions;
