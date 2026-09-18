import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { ScopeRegistryEnum } from "~/types/api";

import { DiscoveryScreen } from "./DiscoveryScreen";

// PrivacyCare — Screen 4 of docs/design/privacycare-screens/DESIGN.md.
// Same permission-mocking discipline as ScreeningTable.test.tsx: Restrict
// (~/features/common/Restrict) reads the current user's scopes via
// useAppSelector(selectThisUsersScopes), mocked here rather than standing
// up a real Redux store.
let mockUserScopes: ScopeRegistryEnum[] = [];

jest.mock("~/app/hooks", () => ({
  useAppSelector: (selector: (state: unknown) => unknown) =>
    selector(undefined),
}));

jest.mock("~/features/user-management", () => ({
  selectThisUsersScopes: () => mockUserScopes,
  selectThisUsersRoles: () => [],
}));

const CONNECTION_KEY = "privacycare_scratch_local_postgres";
const MONITOR_KEY = "privacycare_local_discovery";

const CONFIGURED_MONITOR = {
  items: [
    {
      name: "PrivacyCare local discovery",
      key: MONITOR_KEY,
      connection_config_key: CONNECTION_KEY,
      databases: [],
      excluded_databases: [],
      stewards: [],
      last_monitored: null,
      execution_records: null,
    },
  ],
  total: 1,
  page: 1,
  size: 50,
  pages: 1,
};

const NO_MONITORS = { items: [], total: 0, page: 1, size: 50, pages: 1 };

const mockGetDiscoveryMonitorsQuery = jest.fn();
const mockGetDiscoveryMonitorDeletionImpactQuery = jest.fn();
const mockPutMonitor = jest.fn(() => ({
  unwrap: () =>
    Promise.resolve({
      name: "PrivacyCare local discovery",
      key: MONITOR_KEY,
      connection_config_key: CONNECTION_KEY,
    }),
}));
const mockExecuteMonitor = jest.fn(() => ({
  unwrap: () =>
    Promise.resolve({
      detail: `Discovery scan queued for monitor ${MONITOR_KEY}`,
    }),
}));

jest.mock("./discovery.slice", () => ({
  ...jest.requireActual("./discovery.slice"),
  useGetDiscoveryMonitorsQuery: () => mockGetDiscoveryMonitorsQuery(),
  useGetDiscoveryMonitorDeletionImpactQuery: () =>
    mockGetDiscoveryMonitorDeletionImpactQuery(),
  usePutDiscoveryMonitorMutation: () => [mockPutMonitor, { isLoading: false }],
  useExecuteDiscoveryMonitorMutation: () => [
    mockExecuteMonitor,
    { isLoading: false },
  ],
}));

// useMessage needs a FidesUIProvider ancestor this test does not stand up —
// same gap RopaList.test.tsx and AddRiskModal.test.tsx document for their
// own use of it.
jest.mock(
  "fidesui",
  () =>
    new Proxy(jest.requireActual("fidesui"), {
      get(target, prop) {
        if (prop === "useMessage") {
          return () => ({
            success: jest.fn(),
            error: jest.fn(),
            warning: jest.fn(),
          });
        }
        return target[prop as keyof typeof target];
      },
    }),
);

const VIEWER_SCOPES = [ScopeRegistryEnum.PRIVACYCARE_DISCOVERY_READ];
const CONTRIBUTOR_SCOPES = [
  ScopeRegistryEnum.PRIVACYCARE_DISCOVERY_READ,
  ScopeRegistryEnum.PRIVACYCARE_DISCOVERY_UPDATE,
];

beforeEach(() => {
  jest.clearAllMocks();
  mockUserScopes = CONTRIBUTOR_SCOPES;
  mockGetDiscoveryMonitorsQuery.mockReturnValue({
    data: NO_MONITORS,
    isLoading: false,
    isError: false,
    refetch: jest.fn(),
  });
  mockGetDiscoveryMonitorDeletionImpactQuery.mockReturnValue({
    data: undefined,
    isFetching: false,
    isError: false,
    refetch: jest.fn(),
  });
});

describe("DiscoveryScreen — the target is named in words, never as a connection key", () => {
  it("never-scanned state names the target and never the raw connection key", () => {
    render(<DiscoveryScreen />);

    expect(
      screen.getAllByText(
        /PrivacyCare local database \(our own infrastructure\)/,
      ).length,
    ).toBeGreaterThan(0);
    expect(screen.queryByText(CONNECTION_KEY)).not.toBeInTheDocument();
  });

  it("configured state also names the target and never the raw connection key", () => {
    mockGetDiscoveryMonitorsQuery.mockReturnValue({
      data: CONFIGURED_MONITOR,
      isLoading: false,
      isError: false,
      refetch: jest.fn(),
    });

    render(<DiscoveryScreen />);

    expect(
      screen.getAllByText(
        /PrivacyCare local database \(our own infrastructure\)/,
      ).length,
    ).toBeGreaterThan(0);
    expect(screen.queryByText(CONNECTION_KEY)).not.toBeInTheDocument();
  });
});

describe("DiscoveryScreen — never-scanned state explains itself", () => {
  it("explains what discovery does rather than rendering an empty table", () => {
    render(<DiscoveryScreen />);

    expect(screen.getByTestId("discovery-never-run")).toBeInTheDocument();
    expect(screen.getByText("Discovery has never run")).toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });
});

describe("DiscoveryScreen — permissions", () => {
  it("hides the run-scan control from a Viewer (read-only scope) when never scanned", () => {
    mockUserScopes = VIEWER_SCOPES;
    render(<DiscoveryScreen />);

    expect(screen.queryByTestId("run-scan-button")).not.toBeInTheDocument();
  });

  it("shows the run-scan control to a Contributor (update scope) when never scanned", () => {
    mockUserScopes = CONTRIBUTOR_SCOPES;
    render(<DiscoveryScreen />);

    expect(screen.getByTestId("run-scan-button")).toBeInTheDocument();
  });

  it("hides the run-scan control from a Viewer once a monitor is configured", () => {
    mockUserScopes = VIEWER_SCOPES;
    mockGetDiscoveryMonitorsQuery.mockReturnValue({
      data: CONFIGURED_MONITOR,
      isLoading: false,
      isError: false,
      refetch: jest.fn(),
    });

    render(<DiscoveryScreen />);

    expect(screen.queryByTestId("run-scan-button")).not.toBeInTheDocument();
  });

  it("shows the run-scan control to a Contributor once a monitor is configured", () => {
    mockUserScopes = CONTRIBUTOR_SCOPES;
    mockGetDiscoveryMonitorsQuery.mockReturnValue({
      data: CONFIGURED_MONITOR,
      isLoading: false,
      isError: false,
      refetch: jest.fn(),
    });

    render(<DiscoveryScreen />);

    expect(screen.getByTestId("run-scan-button")).toBeInTheDocument();
  });
});

describe("DiscoveryScreen — running a scan", () => {
  it("creates the monitor bound to our own database, then executes it, on the first-ever run", async () => {
    const user = userEvent.setup();
    render(<DiscoveryScreen />);

    await user.click(screen.getByTestId("run-scan-button"));

    expect(mockPutMonitor).toHaveBeenCalledWith(
      expect.objectContaining({
        connection_config_key: CONNECTION_KEY,
        databases: [],
      }),
    );
    expect(mockExecuteMonitor).toHaveBeenCalledWith(MONITOR_KEY);
  });

  it("executes the already-configured monitor directly, without creating a second one", async () => {
    mockGetDiscoveryMonitorsQuery.mockReturnValue({
      data: CONFIGURED_MONITOR,
      isLoading: false,
      isError: false,
      refetch: jest.fn(),
    });
    const user = userEvent.setup();
    render(<DiscoveryScreen />);

    await user.click(screen.getByTestId("run-scan-button"));

    expect(mockPutMonitor).not.toHaveBeenCalled();
    expect(mockExecuteMonitor).toHaveBeenCalledWith(MONITOR_KEY);
  });

  it("surfaces the server's own 409 'already in progress' message plainly, rather than a generic failure", async () => {
    mockGetDiscoveryMonitorsQuery.mockReturnValue({
      data: CONFIGURED_MONITOR,
      isLoading: false,
      isError: false,
      refetch: jest.fn(),
    });
    mockExecuteMonitor.mockReturnValue({
      // Not an Error instance on purpose: this is RTK Query's own error
      // shape (FetchBaseQueryError — {status, data}), which is exactly
      // what getErrorMessage (real, unmocked, in features/common/helpers.ts)
      // expects to read the server's literal `detail` string out of.
      unwrap: () =>
        // eslint-disable-next-line prefer-promise-reject-errors
        Promise.reject({
          status: 409,
          data: {
            detail: `A discovery scan is already in progress for monitor ${MONITOR_KEY}`,
          },
        }),
    });
    const user = userEvent.setup();
    render(<DiscoveryScreen />);

    await user.click(screen.getByTestId("run-scan-button"));

    expect(
      await screen.findByTestId("discovery-action-error"),
    ).toHaveTextContent(
      `A discovery scan is already in progress for monitor ${MONITOR_KEY}`,
    );
  });
});

describe("DiscoveryScreen — the resource count is real, and there is no fabricated findings table", () => {
  it("shows the live resource count from the deletion-impact read when a monitor has found something", () => {
    mockGetDiscoveryMonitorsQuery.mockReturnValue({
      data: CONFIGURED_MONITOR,
      isLoading: false,
      isError: false,
      refetch: jest.fn(),
    });
    mockGetDiscoveryMonitorDeletionImpactQuery.mockReturnValue({
      data: {
        staged_resource_count: 148,
        linked_datasets: [],
        active_task_count: 0,
        associated_system_count: 0,
      },
      isFetching: false,
      isError: false,
      refetch: jest.fn(),
    });

    render(<DiscoveryScreen />);

    expect(screen.getByTestId("discovery-resource-count")).toHaveTextContent(
      "148 structural resources discovered so far",
    );
  });

  it("says plainly that nothing has been discovered yet, rather than showing zero rows unexplained", () => {
    mockGetDiscoveryMonitorsQuery.mockReturnValue({
      data: CONFIGURED_MONITOR,
      isLoading: false,
      isError: false,
      refetch: jest.fn(),
    });
    mockGetDiscoveryMonitorDeletionImpactQuery.mockReturnValue({
      data: {
        staged_resource_count: 0,
        linked_datasets: [],
        active_task_count: 0,
        associated_system_count: 0,
      },
      isFetching: false,
      isError: false,
      refetch: jest.fn(),
    });

    render(<DiscoveryScreen />);

    expect(screen.getByTestId("discovery-resource-count")).toHaveTextContent(
      "No resources discovered yet.",
    );
  });

  it("admits, in every state, that individual findings cannot be listed or reconciled here — never a table implying a review queue that does not exist", () => {
    render(<DiscoveryScreen />);
    expect(
      screen.getByTestId("discovery-missing-capabilities"),
    ).toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
    expect(screen.queryByText(/needs review/i)).not.toBeInTheDocument();
  });
});

describe("DiscoveryScreen — loading and error states", () => {
  it("shows a retry control when discovery's configuration fails to load", async () => {
    const refetch = jest.fn();
    mockGetDiscoveryMonitorsQuery.mockReturnValue({
      data: undefined,
      isLoading: false,
      isError: true,
      refetch,
    });

    render(<DiscoveryScreen />);

    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: /retry/i }));
    expect(refetch).toHaveBeenCalled();
  });
});
