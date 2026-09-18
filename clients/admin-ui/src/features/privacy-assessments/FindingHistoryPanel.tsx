import { Button, Result, Space, Spin, Text, Tooltip } from "fidesui";

import { isAPIError } from "~/types/errors/api";

import { useGetDiscoveryFindingHistoryQuery } from "./discovery-findings.slice";
import { FindingStateTag } from "./FindingStateTag";

const formatDecidedAt = (isoDate: string): string => {
  const date = new Date(isoDate);
  return Number.isNaN(date.getTime())
    ? isoDate
    : date.toLocaleString(undefined, {
        dateStyle: "medium",
        timeStyle: "short",
      });
};

/**
 * PrivacyCare — Discovery, Screen 4 of docs/design/privacycare-screens/DESIGN.md.
 *
 * "Show the history where a finding has been reconciled before — who
 * decided, when, and why." Newest first, nothing ever removed — same
 * discipline ScreeningHistoryPanel already keeps for a screening verdict.
 * Rendered inside FindingsTable's own `expandable.expandedRowRender`.
 */
export const FindingHistoryPanel = ({ urn }: { urn: string }) => {
  const { data, isLoading, isError, error, refetch } =
    useGetDiscoveryFindingHistoryQuery(urn);

  if (isLoading) {
    return (
      <Space align="center" className="py-4">
        <Spin size="small" />
        <Text type="secondary" size="sm">
          Loading reconciliation history…
        </Text>
      </Space>
    );
  }

  if (isError) {
    // 404 (never discovered) and any other failure mean different things —
    // same discipline api/discovery.py's own docstring keeps for
    // _require_finding ahead of this route.
    const isNotFound = isAPIError(error) && error.status === 404;
    return (
      <Result
        status={isNotFound ? "warning" : "error"}
        title={
          isNotFound
            ? "This table could not be found."
            : "Failed to load reconciliation history."
        }
        subTitle={
          isNotFound
            ? "It may no longer be part of the latest scan."
            : "Please try again."
        }
        extra={
          isNotFound ? undefined : (
            <Button type="link" onClick={() => refetch()}>
              Retry
            </Button>
          )
        }
      />
    );
  }

  const reconciliations = data?.reconciliations ?? [];

  if (reconciliations.length === 0) {
    return (
      <Text type="secondary" size="sm" data-testid="finding-history-empty">
        Needs review. No reconciliation has been recorded for this table.
      </Text>
    );
  }

  return (
    <Space
      orientation="vertical"
      size="middle"
      className="w-full"
      data-testid="finding-history"
    >
      {reconciliations.map((entry) => (
        <div
          key={entry.id}
          className="border-b border-gray-100 pb-3 last:border-b-0 last:pb-0"
        >
          <Space orientation="vertical" size="small" className="w-full">
            <Space align="center">
              <FindingStateTag state={entry.state} />
              <Text type="secondary" size="sm">
                <Tooltip title={`Stored identifier: ${entry.decided_by}`}>
                  <span>{entry.decided_by}</span>
                </Tooltip>{" "}
                · {formatDecidedAt(entry.decided_at)}
              </Text>
            </Space>
            {entry.state === "mapped" && entry.system_name && (
              <Text size="sm">System: {entry.system_name}</Text>
            )}
            {entry.state === "ignored" && entry.reason && (
              <Text size="sm">Reason: {entry.reason}</Text>
            )}
          </Space>
        </div>
      ))}
    </Space>
  );
};
