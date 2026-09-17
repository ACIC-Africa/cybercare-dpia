import { Button, Result, Text } from "fidesui";

import { TableSkeletonLoader } from "~/features/common/table/v2/TableSkeletonLoader";

import { useGetScreeningStatusesQuery } from "./screening.slice";
import { ScreeningEmptyState } from "./ScreeningEmptyState";
import { ScreeningTable } from "./ScreeningTable";

/**
 * PrivacyCare (spec 2026-09-16 D-W2-7g)
 *
 * Screen 1 of docs/design/privacycare-screens/DESIGN.md: decide whether each
 * of the customer's business processes needs a DPIA, and for the ones that
 * do, capture the mapping. Mounted at
 * `/privacy-assessments/screening` (`src/pages/privacy-assessments/screening.tsx`).
 */
export const Screening = () => {
  const { data, isLoading, isError, refetch } = useGetScreeningStatusesQuery();

  if (isLoading) {
    return <TableSkeletonLoader rowHeight={44} numRows={10} />;
  }

  if (isError) {
    return (
      <Result
        status="error"
        title="Failed to load screening statuses"
        subTitle="There was an error loading the business process register. Please try again."
        extra={
          <Button type="primary" onClick={() => refetch()}>
            Retry
          </Button>
        }
      />
    );
  }

  const processes = data?.processes ?? [];

  if (processes.length === 0) {
    return <ScreeningEmptyState />;
  }

  return (
    <>
      <Text type="secondary" className="mb-4 block">
        Decide whether each business process needs a Data Protection Impact
        Assessment. Marking one applicable opens the mapping step, so the data
        it touches is on record from the moment it is decided to matter.
      </Text>
      <ScreeningTable processes={processes} />
    </>
  );
};
