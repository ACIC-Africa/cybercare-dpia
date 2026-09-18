import { useRouter } from "next/router";

import { PRIVACY_ASSESSMENTS_ROPA_ROUTE } from "~/features/common/nav/routes";

import { RopaEntry } from "./RopaEntry";
import { RopaList } from "./RopaList";

function readQueryParam(
  value: string | string[] | undefined,
): string | undefined {
  return Array.isArray(value) ? value[0] : value;
}

/**
 * PrivacyCare — Screen 3 of docs/design/privacycare-screens/DESIGN.md.
 *
 * Master-detail on a single route (`/privacy-assessments/ropa`, per
 * DESIGN.md — no `[id]` sub-route the way the assessments detail page
 * has one), toggled by a `process` query param rather than local-only
 * state: a shareable/refreshable URL to one business process's entry is
 * exactly what a privacy officer reaching for this to answer a regulator
 * would want, and `router.push` (shallow) keeps this off the history
 * stack in a way that also makes the browser's own Back button do the
 * right thing between the list and an entry.
 */
export const Ropa = () => {
  const router = useRouter();
  const selectedProcessId = readQueryParam(router.query.process);

  const selectProcess = (businessProcessId: string) => {
    router.push(
      {
        pathname: PRIVACY_ASSESSMENTS_ROPA_ROUTE,
        query: { process: businessProcessId },
      },
      undefined,
      { shallow: true },
    );
  };

  const backToList = () => {
    router.push({ pathname: PRIVACY_ASSESSMENTS_ROPA_ROUTE }, undefined, {
      shallow: true,
    });
  };

  if (selectedProcessId) {
    return (
      <RopaEntry businessProcessId={selectedProcessId} onBack={backToList} />
    );
  }

  return <RopaList onSelect={selectProcess} />;
};
