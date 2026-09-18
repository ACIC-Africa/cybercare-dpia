import { Skeleton, Text, Tooltip } from "fidesui";

import { useGetProcessRopaQuery } from "./ropa.slice";

/**
 * PrivacyCare — Screen 3 of docs/design/privacycare-screens/DESIGN.md.
 *
 * DESIGN.md's list wants "how many processing activities it has" per
 * business process, but there is no bulk endpoint that reports a count —
 * `GET .../ropa` (processes.py) exists only per-process, and this task
 * modifies no Python file, so no such endpoint could be added. Rather than
 * approximate this from screening's own `has_mapping` boolean (which only
 * proves "at least one", not a count — a process CAN carry more than one
 * linked declaration through the PUT .../declarations route, even though no
 * shipped UI path calls it today), this reads the real count directly, one
 * `getProcessRopa` call per row.
 *
 * BOUNDED, NOT 87 CALLS ON LOAD: RopaList.tsx paginates its table
 * client-side at 20 rows (same pageSize ScreeningTable.tsx uses for the
 * same 86+-row register), and antd's Table only mounts the rows on the
 * current page — so this component, and the request it fires, exists for
 * at most ~20 rows at a time, not the whole register. RTK Query caches each
 * businessProcessId's result, so paging back to a row already seen does not
 * refetch it, and RopaEntry.tsx reuses this exact same cache entry when the
 * row is opened — the count and the entry can never show two different
 * numbers from two different fetches.
 */
export const RopaProcessingActivityCount = ({
  businessProcessId,
}: {
  businessProcessId: string;
}) => {
  const { data, isLoading, isError } =
    useGetProcessRopaQuery(businessProcessId);

  if (isLoading) {
    return <Skeleton.Input active size="small" style={{ width: 24 }} />;
  }

  if (isError || !data) {
    return (
      <Tooltip title="Could not load the processing-activity count for this row.">
        <Text type="secondary">—</Text>
      </Tooltip>
    );
  }

  return (
    <span data-testid={`activity-count-${businessProcessId}`}>
      {data.declarations.length}
    </span>
  );
};
