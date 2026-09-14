import { useMemo } from "react";

// PrivacyCare (spec 2026-09-13 D-KT-5)
import { useGetProcessingGroundsQuery } from "~/features/privacycare/processing-grounds.slice";
import { LegalBasisForProcessingEnum } from "~/types/api";

export interface LegalBasisOption {
  label: string;
  value: string;
  /**
   * PrivacyCare (spec 2026-09-13 D-KT-5): the Article 6 class this option
   * resolves to. `value` is the option's identity (a ground id for a Kenyan
   * ground, the class itself for a class-only option); `legalBasis` is what
   * the system PUT must write to
   * `privacydeclaration.legal_basis_for_processing`, which D-KT-4 requires
   * stay the enum. The caller substitutes this for `value` before saving.
   */
  legalBasis: string;
  /**
   * PrivacyCare (spec 2026-09-13 D-KT-5): id of the Kenyan processing
   * ground this option came from. Undefined for the class-only options —
   * those never produce a `setDeclarationGround` call.
   */
  groundId?: string;
}

// PrivacyCare (spec 2026-09-13 D-KT-5): the bare Article 6 classes, offered
// alongside the Kenyan grounds so a consultant can still record "Consent"
// without claiming one of the customer's named grounds — and so a
// declaration whose class was recorded before this screen existed (or whose
// ground row was never written) opens on the class it actually stores
// rather than on a ground nobody chose.
const classOnlyOptions: LegalBasisOption[] = (
  Object.keys(LegalBasisForProcessingEnum) as Array<
    keyof typeof LegalBasisForProcessingEnum
  >
).map((key) => ({
  value: LegalBasisForProcessingEnum[key],
  label: LegalBasisForProcessingEnum[key],
  legalBasis: LegalBasisForProcessingEnum[key],
}));

const useLegalBasisOptions = () => {
  // PrivacyCare (spec 2026-09-13 D-KT-5): the dropdown lists the customer's
  // own Kenyan processing grounds (e.g. "KYC Requirements") plus the bare
  // Article 6 classes. Several grounds share one class, so the option's
  // `value` is the GROUND ID, not the class — keying on the class made
  // antd render the alphabetically-first ground with that class as the
  // selected label, i.e. a ground nobody chose. While the query is loading,
  // or if it has no mapped grounds yet, the class-only options alone keep
  // the form from rendering empty.
  const { data, isLoading } = useGetProcessingGroundsQuery();

  const legalBasisOptions = useMemo<LegalBasisOption[]>(() => {
    if (!isLoading && data?.grounds && data.grounds.length > 0) {
      return [
        ...data.grounds.map((g) => ({
          label: g.ground,
          value: g.id,
          legalBasis: g.fides_legal_basis,
          groundId: g.id,
        })),
        ...classOnlyOptions,
      ];
    }
    return classOnlyOptions;
  }, [data, isLoading]);

  return { legalBasisOptions };
};

export default useLegalBasisOptions;
