import { render, screen, within } from "@testing-library/react";

import { RopaEntryResponse } from "./ropa.types";
import { RopaEntry } from "./RopaEntry";

// PrivacyCare — Screen 3 of docs/design/privacycare-screens/DESIGN.md.
// Real fuel-retailer data, not placeholders: "Fuel Card Issuance"
// (bp_9e332dcbe707, Card Operations, register reference "17") is the exact
// worked example the build prompt names — "Legitimate interests" and
// "7 years post account closure" are its real lawful basis and retention
// in the live deployment.
const FUEL_CARD_PROCESS: RopaEntryResponse["process"] = {
  id: "bp_9e332dcbe707",
  name: "Fuel Card Issuance",
  description: "Issuing corporate and retail fuel cards after KYC checks.",
  business_cycle: "Card Operations",
  owner_name: "Josephine Wanjiru",
  owner_email: "josephine@example.co.ke",
  is_critical: true,
  criticality_note: null,
  external_ref: "17",
  last_attested_at: null,
  created_at: "2026-09-01T00:00:00Z",
  updated_at: "2026-09-10T00:00:00Z",
};

const FUEL_CARD_DECLARATION = {
  id: "pd_fuelcard1",
  name: "Fuel Card Issuance — Fuel card issuance and KYC verification",
  data_use: "essential.service.kyc",
  data_categories: ["user.demographic.religious_belief", "user.financial"],
  data_subjects: ["customer"],
  legal_basis: "Legitimate interests",
  retention_period: "7 years post account closure",
  system_id: "sys_1",
  system_name: "Fuel Card Platform",
};

const mockGetProcessRopaQuery = jest.fn();

jest.mock("./ropa.slice", () => ({
  useGetProcessRopaQuery: () => mockGetProcessRopaQuery(),
}));

// C1 discipline (Screen 1's own fix wave): human names, never raw fides
// keys. "Religion" / user.demographic.religious_belief and "Financial
// Information" / user.financial are the exact same pair
// MappingStepForm.test.tsx uses for its own C1 regression test.
jest.mock("~/features/common/hooks/useTaxonomies", () => ({
  __esModule: true,
  default: () => ({
    getDataUseByKey: (key: string) => {
      const byKey: Record<string, { name: string }> = {
        "essential.service.kyc": { name: "KYC Verification" },
      };
      return byKey[key];
    },
    getDataCategoryByKey: (key: string) => {
      const byKey: Record<string, { name: string }> = {
        "user.demographic.religious_belief": { name: "Religion" },
        "user.financial": { name: "Financial Information" },
      };
      return byKey[key];
    },
    getDataSubjectByKey: (key: string) => {
      const byKey: Record<string, { name: string }> = {
        customer: { name: "Customer" },
      };
      return byKey[key];
    },
  }),
}));

// useMessage needs a FidesUIProvider ancestor this test does not stand up —
// same stub AddRiskModal.test.tsx uses for its own use of useMessage.
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

const noop = () => {};

describe("RopaEntry — renders activities in human language, never raw keys", () => {
  it("shows the process's own register reference, and each activity's purpose, lawful basis and retention", () => {
    mockGetProcessRopaQuery.mockReturnValue({
      data: {
        process: FUEL_CARD_PROCESS,
        declarations: [FUEL_CARD_DECLARATION],
        missing_declarations: [],
      },
      isLoading: false,
      isError: false,
      error: undefined,
      refetch: jest.fn(),
    });

    render(<RopaEntry businessProcessId="bp_9e332dcbe707" onBack={noop} />);

    expect(screen.getByText("Fuel Card Issuance")).toBeInTheDocument();
    expect(screen.getByText("17")).toBeInTheDocument();
    expect(screen.getByText("Card Operations")).toBeInTheDocument();

    const block = screen.getByTestId("ropa-activity-pd_fuelcard1");
    expect(within(block).getByText("KYC Verification")).toBeInTheDocument();
    expect(within(block).getByText("Legitimate interests")).toBeInTheDocument();
    expect(
      within(block).getByText("7 years post account closure"),
    ).toBeInTheDocument();
    expect(within(block).getByText("Fuel Card Platform")).toBeInTheDocument();

    // Human names, never the raw fides_key, as the VISIBLE text.
    expect(within(block).getByText("Religion")).toBeInTheDocument();
    expect(
      within(block).getByText("Financial Information"),
    ).toBeInTheDocument();
    expect(
      within(block).queryByText("user.demographic.religious_belief"),
    ).not.toBeInTheDocument();
    expect(within(block).queryByText("user.financial")).not.toBeInTheDocument();
  });

  it("never claims approval — the not-approved notice always renders", () => {
    mockGetProcessRopaQuery.mockReturnValue({
      data: {
        process: FUEL_CARD_PROCESS,
        declarations: [FUEL_CARD_DECLARATION],
        missing_declarations: [],
      },
      isLoading: false,
      isError: false,
      error: undefined,
      refetch: jest.fn(),
    });

    render(<RopaEntry businessProcessId="bp_9e332dcbe707" onBack={noop} />);

    expect(screen.getByTestId("ropa-not-approved-notice")).toHaveTextContent(
      /not an approved record/i,
    );
  });
});

describe("RopaEntry — no write path", () => {
  it("shows no edit control, and explains where mappings are made", () => {
    mockGetProcessRopaQuery.mockReturnValue({
      data: {
        process: FUEL_CARD_PROCESS,
        declarations: [FUEL_CARD_DECLARATION],
        missing_declarations: [],
      },
      isLoading: false,
      isError: false,
      error: undefined,
      refetch: jest.fn(),
    });

    render(<RopaEntry businessProcessId="bp_9e332dcbe707" onBack={noop} />);

    expect(
      screen.queryByRole("button", { name: /^edit$/i }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /^save$/i }),
    ).not.toBeInTheDocument();
    const notice = screen.getByTestId("ropa-no-write-path-notice");
    expect(notice).toHaveTextContent(/no edit control/i);
    expect(within(notice).getByText("Screening")).toBeInTheDocument();
  });
});

describe("RopaEntry — a process with no activities", () => {
  it("shows the process and says plainly that nothing has been recorded, with a link to Screening", () => {
    mockGetProcessRopaQuery.mockReturnValue({
      data: {
        process: FUEL_CARD_PROCESS,
        declarations: [],
        missing_declarations: [],
      },
      isLoading: false,
      isError: false,
      error: undefined,
      refetch: jest.fn(),
    });

    render(<RopaEntry businessProcessId="bp_9e332dcbe707" onBack={noop} />);

    const notice = screen.getByTestId("ropa-no-activities-notice");
    expect(notice).toHaveTextContent(
      /no processing activity has been recorded/i,
    );
    expect(within(notice).getByText("Screening")).toBeInTheDocument();
    // Not a blank box — the process itself is still shown.
    expect(screen.getByText("Fuel Card Issuance")).toBeInTheDocument();
  });
});

describe("RopaEntry — missing_declarations render as a named gap", () => {
  it("names the dangling link rather than dropping it silently", () => {
    mockGetProcessRopaQuery.mockReturnValue({
      data: {
        process: FUEL_CARD_PROCESS,
        declarations: [],
        missing_declarations: ["pd_gone123"],
      },
      isLoading: false,
      isError: false,
      error: undefined,
      refetch: jest.fn(),
    });

    render(<RopaEntry businessProcessId="bp_9e332dcbe707" onBack={noop} />);

    const gapNotice = screen.getByTestId("ropa-missing-declarations");
    expect(gapNotice).toHaveTextContent(/no longer exists/i);
    expect(within(gapNotice).getByText("pd_gone123")).toBeInTheDocument();
    // A dangling link with nothing else linked is still "no activities" for
    // the purposes of the empty-register notice — the gap notice covers
    // explaining WHY, so the two are not shown as contradicting each other.
    expect(
      screen.queryByTestId("ropa-no-activities-notice"),
    ).not.toBeInTheDocument();
  });
});

describe("RopaEntry — loading and error states", () => {
  it("shows skeleton placeholders while loading", () => {
    mockGetProcessRopaQuery.mockReturnValue({
      data: undefined,
      isLoading: true,
      isError: false,
      error: undefined,
      refetch: jest.fn(),
    });

    render(<RopaEntry businessProcessId="bp_9e332dcbe707" onBack={noop} />);

    expect(screen.queryByText("Fuel Card Issuance")).not.toBeInTheDocument();
  });

  it("renders a distinct message for an unknown business process (404)", () => {
    mockGetProcessRopaQuery.mockReturnValue({
      data: undefined,
      isLoading: false,
      isError: true,
      error: { status: 404, data: { detail: "no such business process" } },
      refetch: jest.fn(),
    });

    render(<RopaEntry businessProcessId="bp_unknown" onBack={noop} />);

    expect(screen.getByText(/could not be found/i)).toBeInTheDocument();
  });

  it("renders a retry for any other failure, and does not take down the page", () => {
    const refetch = jest.fn();
    mockGetProcessRopaQuery.mockReturnValue({
      data: undefined,
      isLoading: false,
      isError: true,
      error: { status: 500, data: {} },
      refetch,
    });

    render(<RopaEntry businessProcessId="bp_9e332dcbe707" onBack={noop} />);

    expect(screen.getByText(/failed to load this record/i)).toBeInTheDocument();
    screen.getByRole("button", { name: /retry/i }).click();
    expect(refetch).toHaveBeenCalled();
  });
});
