import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { ScopeRegistryEnum } from "~/types/api";

import { FindingResponse } from "./discovery-findings.types";
import { FindingsTable } from "./FindingsTable";

// PrivacyCare — Discovery, Screen 4 of docs/design/privacycare-screens/DESIGN.md.
// Real tables from the live scan (176 Table rows against our own fides-db
// — discovery-findings-api-report.md), not placeholders.
const NEEDS_REVIEW_FINDING: FindingResponse = {
  urn: "privacycare_local_discovery.fides.public.accessmanualwebhook",
  table_name: "accessmanualwebhook",
  schema_name: "public",
  monitor_key: "privacycare_local_discovery",
  field_count: 5,
  table_type: null,
  diff_status: "addition",
  state: "needs_review",
  system_id: null,
  system_name: null,
  reason: null,
  decided_by: null,
  decided_at: null,
};

const MAPPED_FINDING: FindingResponse = {
  urn: "privacycare_local_discovery.fides.public.ctl_systems",
  table_name: "ctl_systems",
  schema_name: "public",
  monitor_key: "privacycare_local_discovery",
  field_count: 17,
  table_type: null,
  diff_status: "addition",
  state: "mapped",
  system_id: "sys_4481f3505f6b",
  system_name: "Fuel Card Issuance",
  reason: null,
  decided_by: "fid_b06b0e55-d950-43b1-bf5a-9fa4623d28a9",
  decided_at: "2026-09-18T09:00:00Z",
};

const ALL_FINDINGS = [NEEDS_REVIEW_FINDING, MAPPED_FINDING];

let mockUserScopes: ScopeRegistryEnum[] = [];

jest.mock("~/app/hooks", () => ({
  useAppSelector: (selector: (state: unknown) => unknown) =>
    selector(undefined),
}));

jest.mock("~/features/user-management", () => ({
  selectThisUsersScopes: () => mockUserScopes,
  selectThisUsersRoles: () => [],
}));

const mockGetFindingsQuery = jest.fn();

jest.mock("./discovery-findings.slice", () => ({
  useGetDiscoveryFindingsQuery: (
    args: { state?: string } | undefined,
    opts?: { skip?: boolean },
  ) => mockGetFindingsQuery(args, opts),
}));

// FindingHistoryPanel and ReconcileFindingModal each carry their own test
// file — stubbed here so this file tests the table in isolation, same
// discipline RecordDecisionModal.test.tsx documents for its own
// MappingStepForm stub.
jest.mock("./FindingHistoryPanel", () => ({
  FindingHistoryPanel: ({ urn }: { urn: string }) => (
    <div data-testid="history-stub">History for {urn}</div>
  ),
}));

jest.mock("./ReconcileFindingModal", () => ({
  ReconcileFindingModal: ({
    finding,
    onClose,
  }: {
    finding: FindingResponse;
    onClose: () => void;
  }) => (
    <div data-testid="reconcile-modal-stub">
      Reconciling {finding.urn}
      <button type="button" onClick={onClose}>
        close
      </button>
    </div>
  ),
}));

const VIEWER_SCOPES = [ScopeRegistryEnum.PRIVACYCARE_DISCOVERY_READ];
const CONTRIBUTOR_SCOPES = [
  ScopeRegistryEnum.PRIVACYCARE_DISCOVERY_READ,
  ScopeRegistryEnum.PRIVACYCARE_DISCOVERY_UPDATE,
];

const fixtureFor = (findings: FindingResponse[]) => ({
  data: { findings },
  isLoading: false,
  isError: false,
  refetch: jest.fn(),
});

beforeEach(() => {
  jest.clearAllMocks();
  mockUserScopes = CONTRIBUTOR_SCOPES;
  mockGetFindingsQuery.mockImplementation((args, opts) => {
    if (opts?.skip) {
      return {
        data: undefined,
        isLoading: false,
        isError: false,
        refetch: jest.fn(),
      };
    }
    if (args?.state === "needs_review") {
      return fixtureFor([NEEDS_REVIEW_FINDING]);
    }
    return fixtureFor(ALL_FINDINGS);
  });
});

describe("FindingsTable — defaults to Needs review", () => {
  it("asks the API for needs_review on first render, because that is the work", () => {
    render(<FindingsTable />);

    expect(mockGetFindingsQuery).toHaveBeenCalledWith(
      { state: "needs_review" },
      undefined,
    );
  });

  it("renders real discovered tables with their schema, name, and column count", () => {
    render(<FindingsTable />);

    expect(screen.getByText("accessmanualwebhook")).toBeInTheDocument();
    expect(screen.getAllByText("public").length).toBeGreaterThan(0);
    expect(screen.getByText("5")).toBeInTheDocument();
    expect(screen.getByTestId("finding-state-needs_review")).toHaveTextContent(
      "Needs review",
    );
  });

  it("switching the filter re-queries and shows the matching empty text", async () => {
    mockGetFindingsQuery.mockImplementation((args, opts) => {
      if (opts?.skip) {
        return {
          data: undefined,
          isLoading: false,
          isError: false,
          refetch: jest.fn(),
        };
      }
      return fixtureFor([]);
    });
    const user = userEvent.setup();
    render(<FindingsTable />);

    await user.click(screen.getByTestId("findings-state-filter"));
    await user.click(await screen.findByText("Already mapped"));

    expect(mockGetFindingsQuery).toHaveBeenCalledWith(
      { state: "mapped" },
      undefined,
    );
    expect(
      screen.getByText(/no tables have been marked as already mapped yet/i),
    ).toBeInTheDocument();
  });
});

describe("FindingsTable — permissions", () => {
  it("hides the Reconcile control from a Viewer (read-only scope)", () => {
    mockUserScopes = VIEWER_SCOPES;
    render(<FindingsTable />);

    expect(
      screen.queryByTestId(`reconcile-action-${NEEDS_REVIEW_FINDING.urn}`),
    ).not.toBeInTheDocument();
  });

  it("does not fetch the unfiltered candidate list for a Viewer at all", () => {
    mockUserScopes = VIEWER_SCOPES;
    render(<FindingsTable />);

    expect(mockGetFindingsQuery).toHaveBeenCalledWith(
      { state: "all" },
      { skip: true },
    );
  });

  it("shows the Reconcile control to a Contributor (update scope)", () => {
    mockUserScopes = CONTRIBUTOR_SCOPES;
    render(<FindingsTable />);

    expect(
      screen.getByTestId(`reconcile-action-${NEEDS_REVIEW_FINDING.urn}`),
    ).toBeInTheDocument();
  });

  it("opens the reconcile modal for the clicked row", async () => {
    const user = userEvent.setup();
    render(<FindingsTable />);

    await user.click(
      screen.getByTestId(`reconcile-action-${NEEDS_REVIEW_FINDING.urn}`),
    );

    expect(screen.getByTestId("reconcile-modal-stub")).toHaveTextContent(
      NEEDS_REVIEW_FINDING.urn,
    );
  });
});

describe("FindingsTable — history", () => {
  it("shows a table's reconciliation history when its row is expanded", async () => {
    const user = userEvent.setup();
    render(<FindingsTable />);

    await user.click(screen.getByLabelText(/expand row/i));

    expect(screen.getByTestId("history-stub")).toHaveTextContent(
      NEEDS_REVIEW_FINDING.urn,
    );
  });
});

describe("FindingsTable — loading and error states", () => {
  it("shows a retry control when the findings list fails to load", async () => {
    const refetch = jest.fn();
    mockGetFindingsQuery.mockImplementation((_args, opts) => {
      if (opts?.skip) {
        return {
          data: undefined,
          isLoading: false,
          isError: false,
          refetch: jest.fn(),
        };
      }
      return { data: undefined, isLoading: false, isError: true, refetch };
    });

    render(<FindingsTable />);

    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: /retry/i }));
    expect(refetch).toHaveBeenCalled();
  });
});
