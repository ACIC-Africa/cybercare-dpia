import {
  Button,
  ColumnsType,
  Flex,
  Icons,
  Select,
  Table,
  Text,
  Tooltip,
  useMessage,
} from "fidesui";
import { useMemo, useState } from "react";

import { useAppDispatch } from "~/app/hooks";
import { getErrorMessage } from "~/features/common/helpers";
import useTaxonomies from "~/features/common/hooks/useTaxonomies";
import { TableSkeletonLoader } from "~/features/common/table/v2/TableSkeletonLoader";
import { RTKErrorResult } from "~/types/errors/api";

import { downloadCsv, ropaCsvFilename, ropaEntriesToCsv } from "./ropa.csv";
import { ropaApi, useGetBusinessProcessesQuery } from "./ropa.slice";
import { BusinessProcessResponse } from "./ropa.types";
import { RopaProcessingActivityCount } from "./RopaProcessingActivityCount";
import { useGetScreeningStatusesQuery } from "./screening.slice";
import { ScreeningStatusTag } from "./ScreeningStatusTag";

const ALL_CYCLES = "__all__";
const NO_CYCLE = "__none__";

interface RopaListRow extends BusinessProcessResponse {
  dpia_required: boolean | null;
}

/**
 * PrivacyCare — Screen 3 of docs/design/privacycare-screens/DESIGN.md.
 *
 * The list half of the ROPA screen: every business process — the register
 * itself — filterable by business cycle, the same idiom
 * ScreeningTable.tsx uses for the same 86+-row register. A process with no
 * processing activities is shown, not hidden: "we do nothing with personal
 * data here" is itself a finding a regulator would ask about.
 */
export const RopaList = ({
  onSelect,
}: {
  onSelect: (businessProcessId: string) => void;
}) => {
  const message = useMessage();
  const dispatch = useAppDispatch();
  const { getDataUseByKey, getDataCategoryByKey, getDataSubjectByKey } =
    useTaxonomies();
  const [cycleFilter, setCycleFilter] = useState<string>(ALL_CYCLES);
  const [isExportingRegister, setIsExportingRegister] = useState(false);

  const {
    data: processesData,
    isLoading: isLoadingProcesses,
    isError: isProcessesError,
    refetch: refetchProcesses,
  } = useGetBusinessProcessesQuery();

  // Enrichment, not the backbone: "whether it has been screened" is only
  // available from the screening list (ropa.slice.ts's own header comment
  // explains the scope split), so its failure degrades this one column to
  // "—" rather than taking down the whole list — the register itself still
  // renders from getBusinessProcesses alone.
  const { data: screeningData, isError: isScreeningError } =
    useGetScreeningStatusesQuery();

  const screenedByProcessId = useMemo(() => {
    const map = new Map<string, boolean | null>();
    (screeningData?.processes ?? []).forEach((row) => {
      map.set(row.business_process_id, row.dpia_required);
    });
    return map;
  }, [screeningData]);

  const rows: RopaListRow[] = useMemo(
    () =>
      (processesData?.items ?? []).map((process) => ({
        ...process,
        dpia_required: screenedByProcessId.has(process.id)
          ? (screenedByProcessId.get(process.id) as boolean | null)
          : null,
      })),
    [processesData, screenedByProcessId],
  );

  const cycleOptions = useMemo(() => {
    const cycles = new Set<string>();
    let hasUncategorized = false;
    rows.forEach((p) => {
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
  }, [rows]);

  const filteredRows = useMemo(() => {
    if (cycleFilter === ALL_CYCLES) {
      return rows;
    }
    if (cycleFilter === NO_CYCLE) {
      return rows.filter((p) => !p.business_cycle);
    }
    return rows.filter((p) => p.business_cycle === cycleFilter);
  }, [rows, cycleFilter]);

  const handleExportRegister = async () => {
    setIsExportingRegister(true);
    try {
      const entries = await Promise.all(
        rows.map((row) =>
          dispatch(ropaApi.endpoints.getProcessRopa.initiate(row.id)).unwrap(),
        ),
      );
      const csv = ropaEntriesToCsv(entries, {
        dataUseName: (key) => getDataUseByKey(key)?.name ?? key,
        dataCategoryName: (key) => getDataCategoryByKey(key)?.name ?? key,
        dataSubjectName: (key) => getDataSubjectByKey(key)?.name ?? key,
      });
      downloadCsv(csv, ropaCsvFilename("register"));
      message.success(
        `Downloaded the record of processing activities for ${entries.length} business processes.`,
      );
    } catch (error) {
      message.error(
        getErrorMessage(
          error as RTKErrorResult["error"],
          "Failed to build the ROPA export. Please try again.",
        ),
      );
    } finally {
      setIsExportingRegister(false);
    }
  };

  const columns: ColumnsType<RopaListRow> = [
    {
      title: "Business process",
      dataIndex: "name",
      key: "name",
      sorter: (a, b) => a.name.localeCompare(b.name),
      render: (value: string, row) => (
        <Button
          type="link"
          className="p-0 text-left"
          data-testid={`ropa-open-${row.id}`}
          onClick={() => onSelect(row.id)}
        >
          {value}
        </Button>
      ),
    },
    {
      title: "Business cycle",
      dataIndex: "business_cycle",
      key: "business_cycle",
      render: (value: string | null) => value ?? "—",
    },
    {
      title: "Processing activities",
      key: "processing_activities",
      render: (_value, row) => (
        <RopaProcessingActivityCount businessProcessId={row.id} />
      ),
    },
    {
      title: "Screened",
      key: "screened",
      render: (_value, row) => {
        if (isScreeningError) {
          return (
            <Tooltip title="Could not load screening status.">
              <Text type="secondary">—</Text>
            </Tooltip>
          );
        }
        return (
          <ScreeningStatusTag row={{ dpia_required: row.dpia_required }} />
        );
      },
    },
    {
      title: "Action",
      key: "action",
      render: (_value, row) => (
        <Button
          size="small"
          data-testid={`ropa-view-${row.id}`}
          onClick={() => onSelect(row.id)}
        >
          View
        </Button>
      ),
    },
  ];

  if (isLoadingProcesses) {
    return <TableSkeletonLoader rowHeight={44} numRows={10} />;
  }

  if (isProcessesError) {
    return (
      <Flex vertical align="center" gap="small" className="py-8">
        <Text type="danger">Failed to load the business process register.</Text>
        <Button type="primary" onClick={() => refetchProcesses()}>
          Retry
        </Button>
      </Flex>
    );
  }

  return (
    <Flex vertical gap="middle">
      <Flex align="center" justify="space-between" wrap="wrap" gap="small">
        <Flex align="center" gap="small">
          <Text>Business cycle</Text>
          <Select
            aria-label="Filter by business cycle"
            data-testid="ropa-cycle-filter"
            value={cycleFilter}
            onChange={setCycleFilter}
            options={cycleOptions}
            className="w-64"
          />
        </Flex>
        <Tooltip title="Downloads one row per processing activity, across every business process, with the process columns repeated.">
          <Button
            icon={<Icons.Download />}
            data-testid="ropa-export-register"
            loading={isExportingRegister}
            disabled={rows.length === 0}
            onClick={handleExportRegister}
          >
            Export register as CSV
          </Button>
        </Tooltip>
      </Flex>

      <Table<RopaListRow>
        rowKey="id"
        columns={columns}
        dataSource={filteredRows}
        pagination={{ pageSize: 20, showSizeChanger: true }}
        locale={{
          emptyText:
            cycleFilter === ALL_CYCLES
              ? "No business processes."
              : "No business processes in this business cycle.",
        }}
      />
    </Flex>
  );
};
