import {
  Alert,
  Button,
  Flex,
  Icons,
  Result,
  Skeleton,
  Space,
  Text,
  Tooltip,
  useMessage,
} from "fidesui";
import { useState } from "react";

import { getErrorMessage } from "~/features/common/helpers";
import Restrict from "~/features/common/Restrict";
import { ScopeRegistryEnum } from "~/types/api";
import { RTKErrorResult } from "~/types/errors/api";

import {
  PRIVACYCARE_DISCOVERY_MONITOR_KEY,
  PRIVACYCARE_SCRATCH_CONNECTION_KEY,
  useExecuteDiscoveryMonitorMutation,
  useGetDiscoveryMonitorDeletionImpactQuery,
  useGetDiscoveryMonitorsQuery,
  usePutDiscoveryMonitorMutation,
} from "./discovery.slice";

// Design brief (docs/design/privacycare-screens/DESIGN.md, Screen 4):
// "Name the target in words, every time. Not a connection key." This is the
// only connection this screen will ever create or execute a monitor
// against (D-DM-5) — never a customer system.
const TARGET_LABEL = "PrivacyCare local database (our own infrastructure)";

function targetLabelFor(connectionConfigKey: string): string {
  if (connectionConfigKey === PRIVACYCARE_SCRATCH_CONNECTION_KEY) {
    return TARGET_LABEL;
  }
  // Defensive only — this screen never creates or executes a monitor
  // against any other connection (see discovery.slice.ts's own header
  // comment), but `getDiscoveryMonitors` still returns whatever is
  // actually in the database. Naming an unexpected key plainly, rather
  // than silently reusing TARGET_LABEL for it, is what stops some future
  // customer connection from ever being described on screen as "our own
  // infrastructure" by mistake.
  return `an unrecognised connection (${connectionConfigKey})`;
}

/**
 * PrivacyCare — Discovery, Screen 4 of docs/design/privacycare-screens/DESIGN.md.
 *
 * Finds personal data nobody wrote down — Josephine's brief: "automated or
 * semi-automated discovery of personal data across company systems… flag
 * any newly discovered or undocumented processing activity for review."
 *
 * THIS BUILD CANNOT DO THE SECOND HALF OF THAT SENTENCE. DESIGN.md's own
 * spec for this screen is a findings list (one row per discovered table,
 * "Already mapped" / "Ignored" / "Needs review") with a reconcile action.
 * No route anywhere in this codebase lists an individual `stagedresource`
 * row or writes a per-resource decision — see discovery.slice.ts's own
 * header comment for the full accounting, and
 * docs/design/privacycare-screens/04-discovery-build-report.md for what was
 * looked for and not found. Rather than fabricate a findings table with no
 * API behind it, this screen shows exactly what the four real routes it
 * calls can prove: whether a monitor is configured, the one live resource
 * count `deletion-impact` can report, and a plain admission of what is not
 * built yet — never a table implying a review queue that does not exist.
 */
export const DiscoveryScreen = () => {
  const message = useMessage();
  const [actionError, setActionError] = useState<string | null>(null);

  const {
    data: monitorsData,
    isLoading: isLoadingMonitors,
    isError: isMonitorsError,
    refetch: refetchMonitors,
  } = useGetDiscoveryMonitorsQuery();

  // At most one monitor is ever created by this screen (a fixed key — see
  // discovery.slice.ts), so the first (only) match against our own
  // connection is "the" monitor, or there is none yet.
  const monitor = monitorsData?.items?.[0] ?? null;

  const {
    data: impact,
    isFetching: isLoadingImpact,
    isError: isImpactError,
    refetch: refetchImpact,
  } = useGetDiscoveryMonitorDeletionImpactQuery(monitor?.key ?? "", {
    skip: !monitor?.key,
  });

  const [putMonitor, { isLoading: isCreating }] =
    usePutDiscoveryMonitorMutation();
  const [executeMonitor, { isLoading: isExecuting }] =
    useExecuteDiscoveryMonitorMutation();

  const isBusy = isCreating || isExecuting;

  const handleRunScan = async () => {
    setActionError(null);
    try {
      let monitorKey = monitor?.key ?? undefined;
      if (!monitorKey) {
        // First run ever: create the monitor bound to our own database,
        // unscoped (databases: []) so it walks the whole catalogue rather
        // than one schema of it — see walk.py's own docstring on what an
        // empty scope means. PUT is idempotent per key, so a later click
        // with `monitor` already loaded skips straight to execute below.
        const created = await putMonitor({
          name: "PrivacyCare local discovery",
          key: PRIVACYCARE_DISCOVERY_MONITOR_KEY,
          connection_config_key: PRIVACYCARE_SCRATCH_CONNECTION_KEY,
          databases: [],
          excluded_databases: [],
        }).unwrap();
        monitorKey = created.key ?? PRIVACYCARE_DISCOVERY_MONITOR_KEY;
      }
      await executeMonitor(monitorKey).unwrap();
      message.success(`Scan queued for ${TARGET_LABEL}.`);
    } catch (error) {
      // F3's 409 ("A discovery scan is already in progress for monitor
      // ...") lands here exactly like any other failure — getErrorMessage
      // reads it from the plain {detail: string} body monitors.py sends,
      // so a double-click or a retry against a still-running scan surfaces
      // the server's own true answer rather than a generic failure.
      setActionError(
        getErrorMessage(
          error as RTKErrorResult["error"],
          "Failed to queue the discovery scan. Please try again.",
        ),
      );
    }
  };

  if (isLoadingMonitors) {
    return (
      <Space orientation="vertical" size="middle" className="w-full">
        <Skeleton active paragraph={{ rows: 2 }} />
        <Skeleton active paragraph={{ rows: 4 }} />
      </Space>
    );
  }

  if (isMonitorsError) {
    return (
      <Result
        status="error"
        title="Failed to load discovery"
        subTitle="There was an error loading discovery's configuration. Please try again."
        extra={
          <Button type="primary" onClick={() => refetchMonitors()}>
            Retry
          </Button>
        }
      />
    );
  }

  const targetLabel = monitor
    ? targetLabelFor(monitor.connection_config_key)
    : TARGET_LABEL;

  const runScanButton = (
    <Restrict scopes={[ScopeRegistryEnum.PRIVACYCARE_DISCOVERY_UPDATE]}>
      <Button
        type="primary"
        icon={<Icons.Renew />}
        loading={isBusy}
        data-testid="run-scan-button"
        onClick={handleRunScan}
      >
        {monitor ? "Run scan again" : "Run scan"}
      </Button>
    </Restrict>
  );

  return (
    <Space orientation="vertical" size="large" className="w-full">
      {/* The boundary that governs this screen, DESIGN.md's own words:
          named in the same place and the same terms every time this
          screen renders, in every state. */}
      <Alert
        type="info"
        showIcon
        message={`Discovery target: ${targetLabel}`}
        description="Discovery reads structure only — schema, table and column names. It never reads a row of data, and no system of a customer's is scanned until she names one and authorises access."
        data-testid="discovery-target"
      />

      {!monitor && (
        <Result
          icon={<Icons.DataAnalytics size={48} />}
          title="Discovery has never run"
          subTitle={`No discovery monitor has been configured yet. Running a scan reads the structure of ${targetLabel} — its schemas, tables and columns — and never a row of data. A large database can take a few minutes.`}
          extra={runScanButton}
          data-testid="discovery-never-run"
        />
      )}

      {monitor && (
        <Space orientation="vertical" size="middle" className="w-full">
          <Flex justify="space-between" align="flex-start" wrap="wrap">
            <Space orientation="vertical" size="small">
              <Text strong>Configured</Text>
              <Text>Target: {targetLabel}</Text>
              {/* Neither MonitorConfig.last_monitored nor an execution-history
                  route is populated by this build's pipeline — see
                  discovery.slice.ts's own header comment. Admitting that
                  plainly is the whole point of this screen, per the CEO's
                  own instruction against a screen that pretends. */}
              <Tooltip title="This version of the API does not record when a monitor last completed a run, or its result. See the build report for what would be needed to show this.">
                <Text type="secondary" data-testid="discovery-last-run">
                  Last run: not reported by this version of PrivacyCare
                </Text>
              </Tooltip>
            </Space>
            {runScanButton}
          </Flex>

          <Flex align="center" gap="small" wrap="wrap">
            {isLoadingImpact && <Skeleton.Button active size="small" />}
            {!isLoadingImpact && isImpactError && (
              <Text type="danger">Could not read the resource count.</Text>
            )}
            {!isLoadingImpact && !isImpactError && impact && (
              <Text data-testid="discovery-resource-count">
                {impact.staged_resource_count === 0
                  ? "No resources discovered yet."
                  : `${impact.staged_resource_count} structural resources discovered so far (schemas, tables and columns combined).`}
              </Text>
            )}
            <Button
              type="link"
              size="small"
              className="p-0"
              data-testid="discovery-refresh"
              onClick={() => refetchImpact()}
            >
              Refresh
            </Button>
          </Flex>

          {actionError && (
            <Alert
              type="error"
              showIcon
              message="Could not queue the scan"
              description={actionError}
              data-testid="discovery-action-error"
            />
          )}
        </Space>
      )}

      {/* An empty findings table with no explanation reads as a broken
          scan, per DESIGN.md — the fix here is not a table (there is no
          API to fill one honestly) but a plain admission of what this
          screen cannot yet do, in the privacy officer's own language. */}
      <Alert
        type="warning"
        showIcon
        message="This screen cannot list individual findings yet"
        description="Discovery can tell you whether a scan has run and roughly how much it found. It cannot yet list the individual tables or columns it found for you to review, or let you mark one as already mapped or ignored — that capability has not been built into PrivacyCare's API yet. See the build report for exactly what is missing."
        data-testid="discovery-missing-capabilities"
      />
    </Space>
  );
};
