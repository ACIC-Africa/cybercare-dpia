import { Button, Result, Space, Spin, Tag, Text } from "fidesui";
import { useMemo } from "react";

import { isAPIError } from "~/types/errors/api";

import {
  useGetScreeningHistoryQuery,
  useGetScreeningTriggersQuery,
} from "./screening.slice";

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
 * PrivacyCare (spec 2026-09-16 D-W2-7g): a business process's full decision
 * history, newest first — nothing here is ever removed. Rendered inside a
 * Table row's `expandable.expandedRowRender` (DESIGN.md: "Expanding a row
 * shows the full decision history").
 */
export const ScreeningHistoryPanel = ({
  businessProcessId,
}: {
  businessProcessId: string;
}) => {
  const { data: triggersData, isLoading: isLoadingTriggers } =
    useGetScreeningTriggersQuery();
  const {
    data: historyData,
    isLoading: isLoadingHistory,
    isError,
    error,
    refetch,
  } = useGetScreeningHistoryQuery(businessProcessId);

  const triggerLabelByKey = useMemo(() => {
    const map = new Map<string, string>();
    (triggersData?.triggers ?? []).forEach((trigger) => {
      map.set(trigger.trigger_key, trigger.label);
    });
    return map;
  }, [triggersData]);

  if (isLoadingHistory || isLoadingTriggers) {
    return (
      <Space align="center" className="py-4">
        <Spin size="small" />
        <Text type="secondary" size="sm">
          Loading decision history…
        </Text>
      </Space>
    );
  }

  if (isError) {
    // 404 (no such business process) and any other failure are both real,
    // but they mean different things — see screening.py's own module
    // docstring on why "no such X" and "anything else" are kept apart.
    const isNotFound = isAPIError(error) && error.status === 404;
    return (
      <Result
        status={isNotFound ? "warning" : "error"}
        title={
          isNotFound
            ? "This business process could not be found."
            : "Failed to load decision history."
        }
        subTitle={
          isNotFound
            ? "It may have been removed since this list was loaded."
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

  const decisions = historyData?.decisions ?? [];

  if (decisions.length === 0) {
    return (
      <Text type="secondary" size="sm">
        Not screened yet. No decision has been recorded for this business
        process.
      </Text>
    );
  }

  return (
    <Space orientation="vertical" size="middle" className="w-full">
      {decisions.map((decision) => (
        <div
          // A business process's decisions have no id of their own
          // (ScreeningVerdictResponse's own comment: gate.ScreeningVerdict
          // carries none) — decided_by + decided_at + the ticked questions
          // is the best stable key available without one, and decided_at
          // is unique per row in practice (the backend's own ORDER BY
          // tie-break is on an id this response does not expose).
          key={`${decision.decided_by}-${decision.decided_at}-${decision.triggered_keys.join(",")}`}
          className="border-b border-gray-100 pb-3 last:border-b-0 last:pb-0"
        >
          <Space orientation="vertical" size="small" className="w-full">
            <Space align="center">
              <Tag color={decision.dpia_required ? "success" : "default"}>
                {decision.dpia_required ? "Applicable" : "Not applicable"}
              </Tag>
              <Text type="secondary" size="sm">
                {decision.decided_by} · {formatDecidedAt(decision.decided_at)}
              </Text>
            </Space>
            {decision.triggered_keys.length > 0 && (
              <Text size="sm">
                Questions ticked:{" "}
                {decision.triggered_keys
                  .map((key) => triggerLabelByKey.get(key) ?? key)
                  .join("; ")}
              </Text>
            )}
            {decision.justification && (
              <Text size="sm">Reason: {decision.justification}</Text>
            )}
          </Space>
        </div>
      ))}
    </Space>
  );
};
