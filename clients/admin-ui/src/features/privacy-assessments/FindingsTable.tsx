import {
  Button,
  ColumnsType,
  Flex,
  Result,
  Select,
  Skeleton,
  Table,
  Text,
} from "fidesui";
import { useState } from "react";

import Restrict from "~/features/common/Restrict";
import { ScopeRegistryEnum } from "~/types/api";

import { useGetDiscoveryFindingsQuery } from "./discovery-findings.slice";
import { FindingResponse, FindingState } from "./discovery-findings.types";
import { FindingHistoryPanel } from "./FindingHistoryPanel";
import { FindingStateTag } from "./FindingStateTag";
import { ReconcileFindingModal } from "./ReconcileFindingModal";

type FindingsFilter = FindingState | "all";

const FILTER_OPTIONS: { value: FindingsFilter; label: string }[] = [
  { value: "needs_review", label: "Needs review" },
  { value: "mapped", label: "Already mapped" },
  { value: "ignored", label: "Ignored" },
  { value: "all", label: "All findings" },
];

const EMPTY_TEXT: Record<FindingsFilter, string> = {
  needs_review:
    "Nothing needs review. Every discovered table has been reconciled.",
  mapped: "No tables have been marked as already mapped yet.",
  ignored: "No tables have been ignored yet.",
  all: "No tables have been discovered yet.",
};

// Defined at module scope, not inline in FindingsTable's JSX — an inline
// arrow function returning JSX inside `expandable` is flagged as an
// "unstable nested component" and remounts the expanded panel (and its own
// in-flight history fetch) on every render, same reasoning
// ScreeningTable.tsx's own renderExpandedRow documents.
const renderExpandedRow = (row: FindingResponse) => (
  <FindingHistoryPanel urn={row.urn} />
);

/**
 * PrivacyCare — Discovery, Screen 4 of docs/design/privacycare-screens/DESIGN.md.
 *
 * "The findings, one row per discovered table: the schema and table, how
 * many columns, and — the column that matters — whether it is already
 * accounted for... Default the list to Needs review, because that is the
 * work. A discovery screen that opens on everything it has ever seen
 * buries the only rows that need a person."
 */
export const FindingsTable = () => {
  const [filter, setFilter] = useState<FindingsFilter>("needs_review");
  const [reconcileTarget, setReconcileTarget] =
    useState<FindingResponse | null>(null);

  const { data, isLoading, isError, refetch } = useGetDiscoveryFindingsQuery({
    state: filter,
  });

  if (isLoading) {
    return <Skeleton active paragraph={{ rows: 6 }} />;
  }

  if (isError) {
    return (
      <Result
        status="error"
        title="Failed to load discovery findings"
        subTitle="There was an error loading the findings. Please try again."
        extra={
          <Button type="primary" onClick={() => refetch()}>
            Retry
          </Button>
        }
      />
    );
  }

  const findings = data?.findings ?? [];

  const columns: ColumnsType<FindingResponse> = [
    {
      title: "Schema",
      dataIndex: "schema_name",
      key: "schema_name",
      sorter: (a, b) => a.schema_name.localeCompare(b.schema_name),
    },
    {
      title: "Table",
      dataIndex: "table_name",
      key: "table_name",
      sorter: (a, b) => a.table_name.localeCompare(b.table_name),
    },
    {
      title: "Columns",
      dataIndex: "field_count",
      key: "field_count",
      sorter: (a, b) => a.field_count - b.field_count,
    },
    {
      title: "State",
      key: "state",
      render: (_value, row) => <FindingStateTag state={row.state} />,
    },
    {
      title: "Action",
      key: "action",
      render: (_value, row) => (
        <Restrict scopes={[ScopeRegistryEnum.PRIVACYCARE_DISCOVERY_UPDATE]}>
          <Button
            size="small"
            data-testid={`reconcile-action-${row.urn}`}
            onClick={() => setReconcileTarget(row)}
          >
            Reconcile
          </Button>
        </Restrict>
      ),
    },
  ];

  return (
    <Flex vertical gap="middle">
      <Flex align="center" gap="small">
        <Text>Show</Text>
        <Select<FindingsFilter>
          aria-label="Filter by reconciliation state"
          data-testid="findings-state-filter"
          value={filter}
          onChange={setFilter}
          options={FILTER_OPTIONS}
          className="w-56"
        />
      </Flex>

      <Table<FindingResponse>
        rowKey="urn"
        columns={columns}
        dataSource={findings}
        pagination={{ pageSize: 20, showSizeChanger: true }}
        locale={{ emptyText: EMPTY_TEXT[filter] }}
        expandable={{ expandedRowRender: renderExpandedRow }}
      />

      {reconcileTarget && (
        <ReconcileFindingModal
          key={reconcileTarget.urn}
          open
          onClose={() => setReconcileTarget(null)}
          finding={reconcileTarget}
        />
      )}
    </Flex>
  );
};
