import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { MappingStepForm } from "./MappingStepForm";

// PrivacyCare (spec 2026-09-16 D-W2-7g): real Kenyan processing grounds,
// transcribed from src/fides/api/privacycare/taxonomy/kenyan.py, not
// placeholder ground names. "KYC Requirements" and "Consent by a Child's
// Parent or Guardian" are the exact two DESIGN.md itself names as the
// worked example for this picker. "Support Business Operation" has no
// fides_legal_basis in the real data (D-KT-4: 12 of the 23 loaded grounds
// have none) and is used here to prove the picker does not offer a
// guaranteed-to-fail choice.
const GROUNDS = [
  { id: "grd-kyc", ground: "KYC Requirements", fides_legal_basis: "Legitimate interests" },
  {
    id: "grd-consent-minor",
    ground: "Consent by a Child's Parent or Guardian",
    fides_legal_basis: "Consent",
  },
  { id: "grd-no-basis", ground: "Support Business Operation", fides_legal_basis: "" },
];

const mockGetDataMappingQuery = jest.fn();
const mockSaveDataMapping = jest.fn(() => ({ unwrap: () => Promise.resolve({}) }));

jest.mock("./screening.slice", () => ({
  useGetDataMappingQuery: () => mockGetDataMappingQuery(),
  useSaveDataMappingMutation: () => [mockSaveDataMapping, { isLoading: false }],
}));

jest.mock("~/features/privacycare/processing-grounds.slice", () => ({
  useGetProcessingGroundsQuery: () => ({
    data: { grounds: GROUNDS, unmapped_count: 1 },
    isLoading: false,
  }),
}));

jest.mock("~/features/common/hooks/useTaxonomies", () => ({
  __esModule: true,
  default: () => ({
    getDataCategories: () => [
      { fides_key: "user.contact.email", name: "Email", tags: [] },
      { fides_key: "user.financial", name: "Financial", tags: [] },
    ],
    getDataUses: () => [
      { fides_key: "operations.support", name: "Support" },
      { fides_key: "marketing.advertising", name: "Advertising" },
    ],
    getDataSubjects: () => [{ fides_key: "customer", name: "Customer" }],
    isLoading: false,
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

beforeEach(() => {
  jest.clearAllMocks();
  mockGetDataMappingQuery.mockReturnValue({
    data: { business_process_id: "bp_94d5439ced86", mapping: null },
    isLoading: false,
    isError: false,
    refetch: jest.fn(),
  });
});

const renderForm = () =>
  render(
    <MappingStepForm
      businessProcessId="bp_94d5439ced86"
      processName="Fuel Card Issuance"
      hasMappingFromList={false}
      onSaved={jest.fn()}
      onCancel={jest.fn()}
    />,
  );

describe("MappingStepForm — lawful basis is guided, not an abstract list", () => {
  it("shows the derived legal basis beneath the picker once a business situation is chosen", async () => {
    const user = userEvent.setup();
    renderForm();

    await user.click(screen.getByTestId("input-ground"));
    const option = await screen.findByTitle("KYC Requirements");
    await user.click(option);

    expect(screen.getByTestId("derived-legal-basis")).toHaveTextContent(
      "KYC Requirements",
    );
    expect(screen.getByTestId("derived-legal-basis")).toHaveTextContent(
      "Legitimate interests",
    );
  });

  it("derives Consent for a different business situation — the system supplies the law", async () => {
    const user = userEvent.setup();
    renderForm();

    await user.click(screen.getByTestId("input-ground"));
    const option = await screen.findByTitle("Consent by a Child's Parent or Guardian");
    await user.click(option);

    expect(screen.getByTestId("derived-legal-basis")).toHaveTextContent("Consent");
  });

  it("never offers a ground with no determined legal basis — it would only ever fail to save", async () => {
    const user = userEvent.setup();
    renderForm();

    await user.click(screen.getByTestId("input-ground"));
    expect(screen.queryByTitle("Support Business Operation")).not.toBeInTheDocument();
  });
});

describe("MappingStepForm — the third, honest state", () => {
  it("shows an ownership notice, not an empty form, when has_mapping is true but this route owns nothing", () => {
    render(
      <MappingStepForm
        businessProcessId="bp_94d5439ced86"
        processName="Fuel Card Issuance"
        hasMappingFromList
        onSaved={jest.fn()}
        onCancel={jest.fn()}
      />,
    );
    expect(screen.getByText(/already has a data mapping/i)).toBeInTheDocument();
    expect(screen.queryByTestId("input-mapping-name")).not.toBeInTheDocument();
  });
});
