import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { ScopeRegistryEnum } from "~/types/api";

import { RiskResponse } from "./risk.types";
import { RiskRegisterSection } from "./RiskRegisterSection";

// PrivacyCare (spec 2026-09-16 D-W2-7g): real fuel-retailer risks against a
// real, live process — "Fuel Card Issuance — Fuel card issuance and KYC
// verification" (assessment pa_018a033614e6 in the running deployment) —
// not placeholder text.
const LOW_RISK_1: RiskResponse = {
  id: "risk-1",
  assessment_id: "pa_018a033614e6",
  category: "confidentiality",
  description:
    "National ID and KRA PIN copies collected during fuel card KYC are kept in a shared branch folder accessible to all branch staff, not only the onboarding team.",
  likelihood: 3,
  severity: 2,
  score: 6,
  band: "medium" as RiskResponse["band"],
};
const LOW_RISK_2: RiskResponse = {
  id: "risk-2",
  assessment_id: "pa_018a033614e6",
  category: "integrity",
  description:
    "A customer's registered phone number can be changed on request without re-verifying identity against the KYC record on file.",
  likelihood: 2,
  severity: 2,
  score: 4,
  band: "low" as RiskResponse["band"],
};
const CRITICAL_RISK: RiskResponse = {
  id: "risk-3",
  assessment_id: "pa_018a033614e6",
  category: "financial_or_reputational_harm",
  description:
    "A cloned fuel card PIN could allow unauthorised fuel draw-downs against a corporate fleet account before the monthly reconciliation catches it.",
  likelihood: 5,
  severity: 5,
  score: 25,
  band: "critical" as RiskResponse["band"],
};

// Sorted highest score first, same as register.list_risks' own ordering —
// the component reads this order rather than re-sorting it.
const RISKS_INCLUDING_CRITICAL = [CRITICAL_RISK, LOW_RISK_1, LOW_RISK_2];

let mockListRisksResult: {
  data: { items: RiskResponse[] } | undefined;
  isLoading: boolean;
  isError: boolean;
  refetch: () => void;
};
let mockOdpcResult: {
  data:
    | { required: boolean; band: string; window_days: number; reason: string }
    | undefined;
  isLoading: boolean;
  isError: boolean;
  refetch: () => void;
};

jest.mock("./risk.slice", () => ({
  useListRisksQuery: () => mockListRisksResult,
  useGetOdpcFindingQuery: () => mockOdpcResult,
}));

// AddRiskModal/RemoveRiskModal have their own test files — stubbed here so
// this file tests RiskRegisterSection's own logic (band, summary,
// permissions) in isolation, the same way AssessmentDetail.test.tsx stubs
// QuestionCard/EvidenceDrawer for its own container test.
jest.mock("./AddRiskModal", () => ({
  AddRiskModal: ({ open }: { open: boolean }) =>
    open ? <div data-testid="add-risk-modal" /> : null,
}));
jest.mock("./RemoveRiskModal", () => ({
  RemoveRiskModal: ({ open, risk }: { open: boolean; risk: RiskResponse }) =>
    open ? <div data-testid="remove-risk-modal">{risk.description}</div> : null,
}));

// Restrict (~/features/common/Restrict) reads the current user's scopes via
// useAppSelector(selectThisUsersScopes) — mocked the same way
// ScreeningTable.test.tsx mocks it for its own permission tests.
let mockUserScopes: ScopeRegistryEnum[] = [
  ScopeRegistryEnum.PRIVACYCARE_RISK_READ,
  ScopeRegistryEnum.PRIVACYCARE_RISK_CREATE,
];

jest.mock("~/app/hooks", () => ({
  useAppSelector: (selector: (state: unknown) => unknown) =>
    selector(undefined),
}));

jest.mock("~/features/user-management", () => ({
  selectThisUsersScopes: () => mockUserScopes,
  selectThisUsersRoles: () => [],
}));

const ASSESSMENT_ID = "pa_018a033614e6";

beforeEach(() => {
  mockUserScopes = [
    ScopeRegistryEnum.PRIVACYCARE_RISK_READ,
    ScopeRegistryEnum.PRIVACYCARE_RISK_CREATE,
  ];
  mockOdpcResult = {
    data: {
      required: false,
      band: "low",
      window_days: 60,
      reason:
        "Residual risk band is low — prior consultation with the ODPC is NOT required. Only a high or critical residual risk triggers the requirement to submit at least 60 days before processing begins.",
    },
    isLoading: false,
    isError: false,
    refetch: jest.fn(),
  };
});

describe("RiskRegisterSection — the band reflects the highest risk, never the average", () => {
  it("reads Critical when several low risks sit alongside one critical risk", () => {
    mockListRisksResult = {
      data: { items: RISKS_INCLUDING_CRITICAL },
      isLoading: false,
      isError: false,
      refetch: jest.fn(),
    };

    render(<RiskRegisterSection assessmentId={ASSESSMENT_ID} />);

    expect(
      within(screen.getByTestId("risk-band-summary")).getByText("Critical"),
    ).toBeInTheDocument();
    expect(
      within(screen.getByTestId("risk-band-summary")).queryByText("Low"),
    ).not.toBeInTheDocument();
    expect(
      within(screen.getByTestId("risk-band-summary")).queryByText("Medium"),
    ).not.toBeInTheDocument();
    // Named, per DESIGN.md's "highest risk, named" — the critical entry's
    // own description, not a generic count.
    expect(screen.getByTestId("highest-risk-summary")).toHaveTextContent(
      /cloned fuel card PIN/i,
    );
  });

  it("reads Critical from an OUT-OF-ORDER list — the band is computed, not read positionally", () => {
    // Fix wave (Screen 2 review), finding 5: RiskRegisterSection used to
    // read `risks[0].band` for the summary band, trusting the list route's
    // own highest-score-first ordering. Every other fixture in this file
    // happens to already be sorted that way, so a positional read would
    // have passed every one of them — this is the test that actually
    // exercises the failure mode: the critical risk sits LAST here, not
    // first, and the band must still read Critical.
    mockListRisksResult = {
      data: { items: [LOW_RISK_1, LOW_RISK_2, CRITICAL_RISK] },
      isLoading: false,
      isError: false,
      refetch: jest.fn(),
    };

    render(<RiskRegisterSection assessmentId={ASSESSMENT_ID} />);

    expect(
      within(screen.getByTestId("risk-band-summary")).getByText("Critical"),
    ).toBeInTheDocument();
  });

  // Fix wave (Screen 2 review), finding 8: a third test used to sit here
  // ("never reads risk_level — the component has no such prop and cannot
  // fall back to it"), reusing this same RISKS_INCLUDING_CRITICAL fixture
  // and asserting the exact same "Critical" text as the first test in this
  // describe block. RiskRegisterSectionProps carries only assessmentId, so
  // there is genuinely no risk_level in scope for the component to read —
  // but nothing distinguished that test's setup or assertion from the one
  // above it, so it proved nothing the first test had not already proved.
  // Removed rather than kept as duplicate coverage.
});

describe("RiskRegisterSection — empty register", () => {
  it("reads Low with the explanatory copy, and is not a blank area", () => {
    mockListRisksResult = {
      data: { items: [] },
      isLoading: false,
      isError: false,
      refetch: jest.fn(),
    };

    render(<RiskRegisterSection assessmentId={ASSESSMENT_ID} />);

    expect(screen.getByText("Low")).toBeInTheDocument();
    expect(screen.getByText("No risks recorded yet.")).toBeInTheDocument();
    expect(
      screen.getByText(
        "No risks recorded. The risk band stays Low until a risk is added.",
      ),
    ).toBeInTheDocument();
    // The add control is still offered from an empty register.
    expect(screen.getByTestId("add-risk")).toBeInTheDocument();
  });
});

describe("RiskRegisterSection — prior consultation", () => {
  it("shows Required, with the reason, when the band is high or critical", () => {
    mockListRisksResult = {
      data: { items: RISKS_INCLUDING_CRITICAL },
      isLoading: false,
      isError: false,
      refetch: jest.fn(),
    };
    mockOdpcResult = {
      data: {
        required: true,
        band: "critical",
        window_days: 60,
        reason:
          "Residual risk band is critical — the Data Protection Act, 2019 requires prior consultation with the Office of the Data Protection Commissioner (ODPC), submitted at least 60 days before this processing activity begins.",
      },
      isLoading: false,
      isError: false,
      refetch: jest.fn(),
    };

    render(<RiskRegisterSection assessmentId={ASSESSMENT_ID} />);

    expect(screen.getByTestId("odpc-status")).toHaveTextContent("Required");
    expect(
      screen.getByTestId("prior-consultation-required-banner"),
    ).toBeInTheDocument();
    expect(screen.getByText(/submitted at least 60 days/i)).toBeInTheDocument();
  });

  it("shows Not required, with no banner, when the band is low or medium", () => {
    mockListRisksResult = {
      data: { items: [LOW_RISK_1, LOW_RISK_2] },
      isLoading: false,
      isError: false,
      refetch: jest.fn(),
    };
    // mockOdpcResult from beforeEach already has required: false.

    render(<RiskRegisterSection assessmentId={ASSESSMENT_ID} />);

    expect(screen.getByTestId("odpc-status")).toHaveTextContent("Not required");
    expect(
      screen.queryByTestId("prior-consultation-required-banner"),
    ).not.toBeInTheDocument();
  });
});

describe("RiskRegisterSection — permissions", () => {
  beforeEach(() => {
    mockListRisksResult = {
      data: { items: [LOW_RISK_1] },
      isLoading: false,
      isError: false,
      refetch: jest.fn(),
    };
  });

  it("hides the add and remove controls from a Viewer (read-only scope)", () => {
    mockUserScopes = [ScopeRegistryEnum.PRIVACYCARE_RISK_READ];

    render(<RiskRegisterSection assessmentId={ASSESSMENT_ID} />);

    expect(screen.queryByTestId("add-risk")).not.toBeInTheDocument();
    expect(
      screen.queryByTestId(`remove-risk-${LOW_RISK_1.id}`),
    ).not.toBeInTheDocument();
    // The risk itself, and the band, are still visible to the Viewer.
    expect(screen.getByText(LOW_RISK_1.description)).toBeInTheDocument();
  });

  it("shows the add and remove controls to a Contributor (create scope)", () => {
    mockUserScopes = [
      ScopeRegistryEnum.PRIVACYCARE_RISK_READ,
      ScopeRegistryEnum.PRIVACYCARE_RISK_CREATE,
    ];

    render(<RiskRegisterSection assessmentId={ASSESSMENT_ID} />);

    expect(screen.getByTestId("add-risk")).toBeInTheDocument();
    expect(
      screen.getByTestId(`remove-risk-${LOW_RISK_1.id}`),
    ).toBeInTheDocument();
  });

  it("opens the remove-confirmation modal naming the correct risk when Remove is clicked", async () => {
    mockUserScopes = [
      ScopeRegistryEnum.PRIVACYCARE_RISK_READ,
      ScopeRegistryEnum.PRIVACYCARE_RISK_CREATE,
    ];
    const user = userEvent.setup();

    render(<RiskRegisterSection assessmentId={ASSESSMENT_ID} />);

    await user.click(screen.getByTestId(`remove-risk-${LOW_RISK_1.id}`));

    expect(screen.getByTestId("remove-risk-modal")).toHaveTextContent(
      LOW_RISK_1.description,
    );
  });
});

describe("RiskRegisterSection — loading and error states", () => {
  it("shows an inline error with retry, without touching the rest of the page", () => {
    const refetch = jest.fn();
    mockListRisksResult = {
      data: undefined,
      isLoading: false,
      isError: true,
      refetch,
    };

    render(<RiskRegisterSection assessmentId={ASSESSMENT_ID} />);

    expect(
      screen.getByText(/Couldn't load the risk register/i),
    ).toBeInTheDocument();
    const retryButton = screen.getByRole("button", { name: /retry/i });
    retryButton.click();
    expect(refetch).toHaveBeenCalled();
  });

  // Fix wave (Screen 2 review), finding 8: the loading/skeleton branch
  // (isLoadingRisks true) had no test at all — only the error branch above
  // was covered. A regression that swapped SKELETON_ROWS for the real
  // `risks` array while isLoading was still true, or that stopped guarding
  // the summary strip's Text/Tag with `isLoading ? <Skeleton.Input /> :
  // ...`, would have shipped with every other test here green.
  it("shows skeleton placeholders while risks are loading, not real values", () => {
    mockListRisksResult = {
      data: undefined,
      isLoading: true,
      isError: false,
      refetch: jest.fn(),
    };

    render(<RiskRegisterSection assessmentId={ASSESSMENT_ID} />);

    // Real summary content is swapped for skeletons — none of it renders
    // while loading.
    expect(screen.queryByTestId("highest-risk-summary")).not.toBeInTheDocument();
    expect(screen.queryByText("No risks recorded yet.")).not.toBeInTheDocument();
    // The empty-register copy is also a real value, not shown mid-load.
    expect(
      screen.queryByText(
        "No risks recorded. The risk band stays Low until a risk is added.",
      ),
    ).not.toBeInTheDocument();
    // The table renders its SKELETON_ROW_COUNT placeholder rows (header +
    // 3), not zero rows and not real data.
    expect(screen.getAllByRole("row")).toHaveLength(4);
  });
});
