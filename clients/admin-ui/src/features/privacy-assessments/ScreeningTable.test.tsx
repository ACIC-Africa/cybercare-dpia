import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { ScopeRegistryEnum } from "~/types/api";

import { ScreeningTable } from "./ScreeningTable";
import { ScreeningStatusResponse } from "./screening.types";

// PrivacyCare (spec 2026-09-16 D-W2-7g): real business processes and cycle,
// not placeholders — Fuel Card Issuance is applicable and mapped, CSR
// Planning & Execution is not applicable with a written reason on record.
const PROCESSES: ScreeningStatusResponse[] = [
  {
    business_process_id: "bp_94d5439ced86",
    name: "Fuel Card Issuance",
    business_cycle: "Card Operations",
    dpia_required: true,
    decided_by: "carol@example.com",
    decided_at: "2026-09-10T09:00:00Z",
    has_mapping: true,
  },
  {
    business_process_id: "bp_csr_planning",
    name: "CSR Planning & Execution",
    business_cycle: "CSR",
    dpia_required: false,
    decided_by: "carol@example.com",
    decided_at: "2026-09-11T09:00:00Z",
    has_mapping: false,
  },
];

// Restrict (~/features/common/Restrict) reads the current user's scopes via
// useAppSelector(selectThisUsersScopes) — this mock lets each test control
// that answer without standing up a real Redux store, the same way
// AssessmentDetail.test.tsx mocks ~/app/hooks for its own user object.
let mockUserScopes: ScopeRegistryEnum[] = [];

jest.mock("~/app/hooks", () => ({
  useAppSelector: (selector: (state: unknown) => unknown) => selector(undefined),
}));

jest.mock("~/features/user-management", () => ({
  selectThisUsersScopes: () => mockUserScopes,
  selectThisUsersRoles: () => [],
}));

jest.mock("~/features/common/hooks/useRelativeTime", () => ({
  useRelativeTime: () => "5 minutes ago",
}));

describe("ScreeningTable — permissions", () => {
  beforeEach(() => {
    mockUserScopes = [];
  });

  it("hides the record-decision control from a Viewer (read-only scope)", () => {
    mockUserScopes = [ScopeRegistryEnum.PRIVACYCARE_SCREENING_READ];
    render(<ScreeningTable processes={PROCESSES} />);

    expect(
      screen.queryByTestId("record-decision-bp_94d5439ced86"),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByTestId("record-decision-bp_csr_planning"),
    ).not.toBeInTheDocument();
    // Hidden, not disabled — a disabled button with no explanation
    // generates a support ticket (DESIGN.md).
    expect(screen.queryByRole("button", { name: /re-screen/i })).not.toBeInTheDocument();
  });

  it("shows the record-decision control to a Contributor (create scope)", () => {
    mockUserScopes = [
      ScopeRegistryEnum.PRIVACYCARE_SCREENING_READ,
      ScopeRegistryEnum.PRIVACYCARE_SCREENING_CREATE,
    ];
    render(<ScreeningTable processes={PROCESSES} />);

    expect(
      screen.getByTestId("record-decision-bp_94d5439ced86"),
    ).toBeInTheDocument();
    expect(
      screen.getByTestId("record-decision-bp_csr_planning"),
    ).toBeInTheDocument();
  });

  it("labels the action Re-screen for an already-screened process, not Record decision", () => {
    mockUserScopes = [ScopeRegistryEnum.PRIVACYCARE_SCREENING_CREATE];
    render(<ScreeningTable processes={PROCESSES} />);

    expect(
      screen.getByTestId("record-decision-bp_94d5439ced86"),
    ).toHaveTextContent("Re-screen");
  });
});

describe("ScreeningTable — rendering", () => {
  beforeEach(() => {
    mockUserScopes = [];
  });

  it("renders each business process by its own real name", () => {
    render(<ScreeningTable processes={PROCESSES} />);
    expect(screen.getByText("Fuel Card Issuance")).toBeInTheDocument();
    expect(screen.getByText("CSR Planning & Execution")).toBeInTheDocument();
  });

  it("filters by business cycle — 86 rows is a working session, not a glance", async () => {
    const user = userEvent.setup();
    render(<ScreeningTable processes={PROCESSES} />);

    expect(screen.getByText("Fuel Card Issuance")).toBeInTheDocument();
    expect(screen.getByText("CSR Planning & Execution")).toBeInTheDocument();

    await user.click(screen.getByTestId("cycle-filter"));
    const cycleOption = await screen.findByTitle("CSR");
    await user.click(cycleOption);

    expect(screen.getByText("CSR Planning & Execution")).toBeInTheDocument();
    expect(screen.queryByText("Fuel Card Issuance")).not.toBeInTheDocument();
  });
});
