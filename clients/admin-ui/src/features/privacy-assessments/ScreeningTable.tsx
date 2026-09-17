import { Button, ColumnsType, Flex, Select, Table, Text } from "fidesui";
import { useMemo, useState } from "react";

import { useRelativeTime } from "~/features/common/hooks/useRelativeTime";
import Restrict from "~/features/common/Restrict";
import { ScopeRegistryEnum } from "~/types/api";

import { RecordDecisionModal, RecordDecisionStep } from "./RecordDecisionModal";
import {
  screeningDisplayStatus,
  ScreeningStatusResponse,
} from "./screening.types";
import { ScreeningHistoryPanel } from "./ScreeningHistoryPanel";
import { MappingStatusTag, ScreeningStatusTag } from "./ScreeningStatusTag";

const ALL_CYCLES = "__all__";
const NO_CYCLE = "__none__";

const DecidedAt = ({ isoDate }: { isoDate: string | null }) => {
  const relative = useRelativeTime(isoDate ? new Date(isoDate) : null);
  return <span>{isoDate ? relative : "—"}</span>;
};

// Defined at module scope, not inline in ScreeningTable's JSX: an inline
// arrow function returning JSX inside `expandable` gets flagged as an
// "unstable nested component" (react/no-unstable-nested-components) — a new
// function identity every render would otherwise remount the expanded panel
// (and its own in-flight history fetch) on every keystroke of the cycle
// filter.
const renderExpandedRow = (row: ScreeningStatusResponse) => (
  <ScreeningHistoryPanel businessProcessId={row.business_process_id} />
);

interface ModalState {
  businessProcessId: string;
  processName: string;
  hasMapping: boolean;
  initialStep: RecordDecisionStep;
}

export const ScreeningTable = ({
  processes,
}: {
  processes: ScreeningStatusResponse[];
}) => {
  const [cycleFilter, setCycleFilter] = useState<string>(ALL_CYCLES);
  const [modalState, setModalState] = useState<ModalState | null>(null);

  // 86 rows is a working session, not a glance, and she works cycle by
  // cycle — there are 18 of them (DESIGN.md).
  const cycleOptions = useMemo(() => {
    const cycles = new Set<string>();
    let hasUncategorized = false;
    processes.forEach((p) => {
      if (p.business_cycle) {
        cycles.add(p.business_cycle);
      } else {
        hasUncategorized = true;
      }
    });
    const options = [
      { value: ALL_CYCLES, label: "All business cycles" },
      ...[...cycles].sort().map((cycle) => ({ value: cycle, label: cycle })),
    ];
    if (hasUncategorized) {
      options.push({ value: NO_CYCLE, label: "No business cycle" });
    }
    return options;
  }, [processes]);

  const filteredProcesses = useMemo(() => {
    if (cycleFilter === ALL_CYCLES) {
      return processes;
    }
    if (cycleFilter === NO_CYCLE) {
      return processes.filter((p) => !p.business_cycle);
    }
    return processes.filter((p) => p.business_cycle === cycleFilter);
  }, [processes, cycleFilter]);

  const columns: ColumnsType<ScreeningStatusResponse> = [
    {
      title: "Business process",
      dataIndex: "name",
      key: "name",
      sorter: (a, b) => a.name.localeCompare(b.name),
    },
    {
      title: "Business cycle",
      dataIndex: "business_cycle",
      key: "business_cycle",
      render: (value: string | null) => value ?? "—",
    },
    {
      title: "Status",
      key: "status",
      render: (_value, row) => <ScreeningStatusTag row={row} />,
    },
    {
      title: "Mapping",
      key: "mapping",
      render: (_value, row) => {
        const status = screeningDisplayStatus(row);
        if (status !== "applicable") {
          return <MappingStatusTag row={row} />;
        }
        return (
          <Flex align="center" gap="small">
            <MappingStatusTag row={row} />
            <Restrict scopes={[ScopeRegistryEnum.PRIVACYCARE_SCREENING_CREATE]}>
              <Button
                type="link"
                size="small"
                className="p-0"
                data-testid={`mapping-action-${row.business_process_id}`}
                onClick={() =>
                  setModalState({
                    businessProcessId: row.business_process_id,
                    processName: row.name,
                    hasMapping: row.has_mapping,
                    initialStep: "mapping",
                  })
                }
              >
                {row.has_mapping ? "Edit mapping" : "Start mapping"}
              </Button>
            </Restrict>
          </Flex>
        );
      },
    },
    {
      title: "Decided by",
      dataIndex: "decided_by",
      key: "decided_by",
      render: (value: string | null) => value ?? "—",
    },
    {
      title: "Decided",
      key: "decided_at",
      render: (_value, row) => <DecidedAt isoDate={row.decided_at} />,
    },
    {
      title: "Action",
      key: "action",
      render: (_value, row) => {
        const status = screeningDisplayStatus(row);
        return (
          <Restrict scopes={[ScopeRegistryEnum.PRIVACYCARE_SCREENING_CREATE]}>
            <Button
              size="small"
              data-testid={`record-decision-${row.business_process_id}`}
              onClick={() =>
                setModalState({
                  businessProcessId: row.business_process_id,
                  processName: row.name,
                  hasMapping: row.has_mapping,
                  initialStep: "decision",
                })
              }
            >
              {status === "not_screened" ? "Record decision" : "Re-screen"}
            </Button>
          </Restrict>
        );
      },
    },
  ];

  return (
    <Flex vertical gap="middle">
      <Flex align="center" gap="small">
        <Text>Business cycle</Text>
        <Select
          aria-label="Filter by business cycle"
          data-testid="cycle-filter"
          value={cycleFilter}
          onChange={setCycleFilter}
          options={cycleOptions}
          className="w-64"
        />
      </Flex>

      <Table<ScreeningStatusResponse>
        rowKey="business_process_id"
        columns={columns}
        dataSource={filteredProcesses}
        pagination={{ pageSize: 20, showSizeChanger: true }}
        locale={{
          emptyText:
            cycleFilter === ALL_CYCLES
              ? "No business processes."
              : "No business processes in this business cycle.",
        }}
        expandable={{ expandedRowRender: renderExpandedRow }}
      />

      {modalState && (
        <RecordDecisionModal
          key={`${modalState.businessProcessId}-${modalState.initialStep}`}
          open
          onClose={() => setModalState(null)}
          businessProcessId={modalState.businessProcessId}
          processName={modalState.processName}
          hasMapping={modalState.hasMapping}
          initialStep={modalState.initialStep}
        />
      )}
    </Flex>
  );
};
