import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { BusinessProcessListResponse } from "./ropa.types";
import { RopaList } from "./RopaList";

// PrivacyCare — Screen 3 of docs/design/privacycare-screens/DESIGN.md.
// Real fuel-retailer processes: "Fuel Card Issuance" (Card Operations, has
// been screened and is applicable) and "CSR Planning & Execution" (CSR,
// never screened, no processing activities) — the second is the exact case
// DESIGN.md calls out: "we do nothing with personal data here" is a
// finding, and its absence from a register is what a regulator asks about.
const BUSINESS_PROCESSES: BusinessProcessListResponse = {
  items: [
    {
      id: "bp_9e332dcbe707",
      name: "Fuel Card Issuance",
      description: null,
      business_cycle: "Card Operations",
      owner_name: "Josephine Wanjiru",
      owner_email: null,
      is_critical: true,
      criticality_note: null,
      external_ref: "17",
      last_attested_at: null,
      created_at: null,
      updated_at: null,
    },
    {
      id: "bp_csr_planning",
      name: "CSR Planning & Execution",
      description: null,
      business_cycle: "CSR",
      owner_name: null,
      owner_email: null,
      is_critical: false,
      criticality_note: null,
      external_ref: "42",
      last_attested_at: null,
      created_at: null,
      updated_at: null,
    },
  ],
  total: 2,
  page: 1,
  size: 100,
  pages: 1,
};

const mockGetBusinessProcessesQuery = jest.fn();
const mockGetScreeningStatusesQuery = jest.fn();

jest.mock("./ropa.slice", () => ({
  useGetBusinessProcessesQuery: () => mockGetBusinessProcessesQuery(),
  ropaApi: { endpoints: { getProcessRopa: { initiate: jest.fn() } } },
}));

jest.mock("./screening.slice", () => ({
  useGetScreeningStatusesQuery: () => mockGetScreeningStatusesQuery(),
}));

// RopaProcessingActivityCount has its own file and its own concerns (a
// bounded per-row fetch) — stubbed here so this file tests RopaList's own
// logic (columns, filter, empty state) in isolation, the same way
// RiskRegisterSection.test.tsx stubs AddRiskModal/RemoveRiskModal for its
// own container test.
jest.mock("./RopaProcessingActivityCount", () => ({
  RopaProcessingActivityCount: ({
    businessProcessId,
  }: {
    businessProcessId: string;
  }) => <span data-testid={`activity-count-${businessProcessId}`}>1</span>,
}));

jest.mock("~/app/hooks", () => ({
  useAppDispatch: () => jest.fn(),
}));

jest.mock("~/features/common/hooks/useTaxonomies", () => ({
  __esModule: true,
  default: () => ({
    getDataUseByKey: () => undefined,
    getDataCategoryByKey: () => undefined,
    getDataSubjectByKey: () => undefined,
  }),
}));

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

const SCREENING_STATUSES = {
  processes: [
    {
      business_process_id: "bp_9e332dcbe707",
      name: "Fuel Card Issuance",
      business_cycle: "Card Operations",
      dpia_required: true,
      decided_by: "fid_1",
      decided_by_display: "Josephine Wanjiru",
      decided_at: "2026-09-01T00:00:00Z",
      has_mapping: true,
    },
    {
      business_process_id: "bp_csr_planning",
      name: "CSR Planning & Execution",
      business_cycle: "CSR",
      dpia_required: null,
      decided_by: null,
      decided_by_display: null,
      decided_at: null,
      has_mapping: false,
    },
  ],
};

beforeEach(() => {
  mockGetBusinessProcessesQuery.mockReturnValue({
    data: BUSINESS_PROCESSES,
    isLoading: false,
    isError: false,
    refetch: jest.fn(),
  });
  mockGetScreeningStatusesQuery.mockReturnValue({
    data: SCREENING_STATUSES,
    isError: false,
  });
});

describe("RopaList — the register", () => {
  it("shows a process with no processing activities, rather than hiding it", () => {
    render(<RopaList onSelect={jest.fn()} />);

    const row = screen.getByText("CSR Planning & Execution").closest("tr")!;
    expect(within(row).getByText("CSR")).toBeInTheDocument();
    expect(within(row).getByText("Not screened")).toBeInTheDocument();
  });

  it("shows the screened status alongside every process", () => {
    render(<RopaList onSelect={jest.fn()} />);

    const fuelCardRow = screen.getByText("Fuel Card Issuance").closest("tr")!;
    expect(within(fuelCardRow).getByText("Applicable")).toBeInTheDocument();
  });

  it("filters by business cycle", async () => {
    const user = userEvent.setup();
    render(<RopaList onSelect={jest.fn()} />);

    expect(screen.getByText("Fuel Card Issuance")).toBeInTheDocument();
    expect(screen.getByText("CSR Planning & Execution")).toBeInTheDocument();

    await user.click(screen.getByTestId("ropa-cycle-filter"));
    const cycleOption = await screen.findByTitle("CSR");
    await user.click(cycleOption);

    expect(screen.queryByText("Fuel Card Issuance")).not.toBeInTheDocument();
    expect(screen.getByText("CSR Planning & Execution")).toBeInTheDocument();
  });

  it("opens the entry for the clicked process", async () => {
    const onSelect = jest.fn();
    const user = userEvent.setup();
    render(<RopaList onSelect={onSelect} />);

    await user.click(screen.getByTestId("ropa-open-bp_9e332dcbe707"));

    expect(onSelect).toHaveBeenCalledWith("bp_9e332dcbe707");
  });
});

describe("RopaList — loading and error states", () => {
  it("shows a retry control when the register fails to load", async () => {
    const refetch = jest.fn();
    mockGetBusinessProcessesQuery.mockReturnValue({
      data: undefined,
      isLoading: false,
      isError: true,
      refetch,
    });

    render(<RopaList onSelect={jest.fn()} />);

    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: /retry/i }));
    expect(refetch).toHaveBeenCalled();
  });

  it("does not crash the list when screening status fails to load — it degrades one column", () => {
    mockGetScreeningStatusesQuery.mockReturnValue({
      data: undefined,
      isError: true,
    });

    render(<RopaList onSelect={jest.fn()} />);

    expect(screen.getByText("Fuel Card Issuance")).toBeInTheDocument();
    expect(screen.getByText("CSR Planning & Execution")).toBeInTheDocument();
  });
});
