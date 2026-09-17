import { render, screen, within } from "@testing-library/react";
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
  {
    id: "grd-kyc",
    ground: "KYC Requirements",
    fides_legal_basis: "Legitimate interests",
  },
  {
    id: "grd-consent-minor",
    ground: "Consent by a Child's Parent or Guardian",
    fides_legal_basis: "Consent",
  },
  {
    id: "grd-no-basis",
    ground: "Support Business Operation",
    fides_legal_basis: "",
  },
];

const mockGetDataMappingQuery = jest.fn();
const mockSaveDataMapping = jest.fn(() => ({
  unwrap: () => Promise.resolve({}),
}));

jest.mock("./screening.slice", () => ({
  useGetDataMappingQuery: () => mockGetDataMappingQuery(),
  useSaveDataMappingMutation: () => [mockSaveDataMapping, { isLoading: false }],
}));

const mockGetProcessingGroundsQuery = jest.fn();

jest.mock("~/features/privacycare/processing-grounds.slice", () => ({
  useGetProcessingGroundsQuery: () => mockGetProcessingGroundsQuery(),
}));

// Real Kenyan taxonomy rows, not placeholders — "Religion"
// (user.demographic.religious_belief) and "Emergency Contact/Next of Kin"
// (next_of_kin) are the exact two C1 names as the search cases her review
// failed on; both carry `active: true` because DataCategorySelect/
// DataSubjectSelect/DataUseSelect (fix wave, item C1) filter to active rows
// by default, and every real row in this database measures active=true.
jest.mock("~/features/common/hooks/useTaxonomies", () => ({
  __esModule: true,
  default: () => ({
    getDataCategories: () => [
      {
        fides_key: "user.demographic.religious_belief",
        name: "Religion",
        tags: [],
        active: true,
      },
      {
        fides_key: "user.financial",
        name: "Financial Information",
        tags: [],
        active: true,
      },
    ],
    getDataCategoryDisplayNameProps: (key: string) => {
      const byKey: Record<string, { name: string }> = {
        "user.demographic.religious_belief": { name: "Religion" },
        "user.financial": { name: "Financial Information" },
      };
      return byKey[key] ?? {};
    },
    getDataUses: () => [
      {
        fides_key: "analytics.reporting.ad_performance",
        name: "Analytics for Advertising Performance",
        active: true,
      },
      {
        fides_key: "essential.legal_obligation",
        name: "Legal Obligation",
        active: true,
      },
    ],
    getDataUseDisplayNameProps: (key: string) => {
      const byKey: Record<string, { name: string }> = {
        "analytics.reporting.ad_performance": {
          name: "Analytics for Advertising Performance",
        },
        "essential.legal_obligation": { name: "Legal Obligation" },
      };
      return byKey[key] ?? {};
    },
    getDataSubjects: () => [
      { fides_key: "customer", name: "Customer", active: true },
      {
        fides_key: "next_of_kin",
        name: "Emergency Contact/Next of Kin",
        active: true,
      },
    ],
    getDataSubjectDisplayNameProps: (key: string) => {
      const byKey: Record<string, { name: string }> = {
        customer: { name: "Customer" },
        next_of_kin: { name: "Emergency Contact/Next of Kin" },
      };
      return byKey[key] ?? {};
    },
    isLoading: false,
  }),
}));

// MappingStepForm's own canSaveMapping check (fix wave, item M7) reads the
// current user's scopes via useHasPermission -> useAppSelector — the same
// mock shape ScreeningTable.test.tsx uses for the same reason. Defaults to
// having SYSTEM_UPDATE so the existing tests below (none of which are about
// permissions) see an enabled Save button; the M7 tests override this.
let mockUserScopes: string[] = ["system:update"];

jest.mock("~/app/hooks", () => ({
  useAppSelector: (selector: (state: unknown) => unknown) =>
    selector(undefined),
}));

jest.mock("~/features/user-management", () => ({
  selectThisUsersScopes: () => mockUserScopes,
  selectThisUsersRoles: () => [],
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
  mockUserScopes = ["system:update"];
  mockGetDataMappingQuery.mockReturnValue({
    data: { business_process_id: "bp_94d5439ced86", mapping: null },
    isLoading: false,
    isError: false,
    refetch: jest.fn(),
  });
  mockGetProcessingGroundsQuery.mockReturnValue({
    data: { grounds: GROUNDS, unmapped_count: 1 },
    isLoading: false,
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
    const option = await screen.findByTitle(
      "Consent by a Child's Parent or Guardian",
    );
    await user.click(option);

    expect(screen.getByTestId("derived-legal-basis")).toHaveTextContent(
      "Consent",
    );
  });

  it("never offers a ground with no determined legal basis — it would only ever fail to save", async () => {
    const user = userEvent.setup();
    renderForm();

    await user.click(screen.getByTestId("input-ground"));
    expect(
      screen.queryByTitle("Support Business Operation"),
    ).not.toBeInTheDocument();
  });
});

describe("MappingStepForm — pickers speak her language, not fides-key (C1)", () => {
  it("shows the taxonomy's own human name for a data category, with the fides_key visible alongside it", async () => {
    const user = userEvent.setup();
    renderForm();

    await user.click(screen.getByTestId("input-data_categories"));

    // Before this fix, the only text rendered for this option was the raw
    // key ("user.demographic.religious_belief"); the human name did not
    // appear anywhere in the DOM at all, which is exactly why searching
    // "Religion" against it found nothing.
    expect(await screen.findByText("Religion")).toBeInTheDocument();
    expect(
      screen.getByText("user.demographic.religious_belief"),
    ).toBeInTheDocument();
  });

  it("finds the Religion row by typing her own word for it, not the fides_key", async () => {
    const user = userEvent.setup();
    renderForm();

    const picker = screen.getByTestId("input-data_categories");
    await user.click(picker);
    const search = within(picker).getByRole("combobox");
    await user.type(search, "Religion");

    expect(await screen.findByText("Religion")).toBeInTheDocument();
    expect(screen.queryByText("Financial Information")).not.toBeInTheDocument();
  });

  it("finds Emergency Contact/Next of Kin by typing 'Emergency', not next_of_kin", async () => {
    const user = userEvent.setup();
    renderForm();

    const picker = screen.getByTestId("input-data_subjects");
    await user.click(picker);
    const search = within(picker).getByRole("combobox");
    await user.type(search, "Emergency");

    expect(
      await screen.findByText("Emergency Contact/Next of Kin"),
    ).toBeInTheDocument();
    expect(screen.queryByText("Customer")).not.toBeInTheDocument();
  });

  it("shows the human name for the purpose picker too — this component's own code, not an inherited one", async () => {
    const user = userEvent.setup();
    renderForm();

    const picker = screen.getByTestId("input-purpose");
    await user.click(picker);
    const search = within(picker).getByRole("combobox");
    await user.type(search, "Advertising");

    expect(
      await screen.findByText("Analytics for Advertising Performance"),
    ).toBeInTheDocument();
  });
});

describe("MappingStepForm — the lawful basis she recorded prefills (I1)", () => {
  it("prefills the picker and shows the derived legal basis for a mapping loaded from GET, with no interaction", async () => {
    mockGetDataMappingQuery.mockReturnValue({
      data: {
        business_process_id: "bp_94d5439ced86",
        mapping: {
          business_process_id: "bp_94d5439ced86",
          privacy_declaration_id: "pri_1",
          system_id: "sys_1",
          name: "Fuel card KYC verification",
          data_subjects: ["customer"],
          data_categories: ["user.financial"],
          // The exact bug this fix closes: get_mapping used to always
          // return ground=None on a GET, even though fides_legal_basis
          // (below) was already being returned correctly — see
          // screening/mapping.py's get_mapping docstring.
          ground: "KYC Requirements",
          fides_legal_basis: "Legitimate interests",
          purpose: null,
          retention_period: null,
          third_parties: null,
          processes_special_category_data: false,
          created: false,
        },
      },
      isLoading: false,
      isError: false,
      refetch: jest.fn(),
    });

    renderForm();

    expect(await screen.findByTestId("derived-legal-basis")).toHaveTextContent(
      "KYC Requirements",
    );
    expect(screen.getByTestId("derived-legal-basis")).toHaveTextContent(
      "Legitimate interests",
    );
  });

  it("reads Lawful basis as blank for a mapping that never named a ground — not every mapping has one", async () => {
    mockGetDataMappingQuery.mockReturnValue({
      data: {
        business_process_id: "bp_94d5439ced86",
        mapping: {
          business_process_id: "bp_94d5439ced86",
          privacy_declaration_id: "pri_2",
          system_id: "sys_1",
          name: "Fuel card account administration",
          data_subjects: [],
          data_categories: ["user.financial"],
          ground: null,
          fides_legal_basis: null,
          purpose: null,
          retention_period: null,
          third_parties: null,
          processes_special_category_data: false,
          created: false,
        },
      },
      isLoading: false,
      isError: false,
      refetch: jest.fn(),
    });

    renderForm();

    await screen.findByTestId("input-mapping-name");
    expect(screen.queryByTestId("derived-legal-basis")).not.toBeInTheDocument();
  });
});

describe("MappingStepForm — disclosing the missing business situations (I3)", () => {
  it("says how many of the real total are available, not just how many are offered", async () => {
    mockGetProcessingGroundsQuery.mockReturnValue({
      data: {
        grounds: Array.from({ length: 11 }, (_, i) => ({
          id: `grd-${i}`,
          ground: `Business situation ${i}`,
          fides_legal_basis: "Legitimate interests",
        })),
        unmapped_count: 12,
      },
      isLoading: false,
    });

    renderForm();

    expect(
      await screen.findByTestId("grounds-availability-note"),
    ).toHaveTextContent(
      "11 of 23 business situations are available; the rest are awaiting a lawful-basis mapping.",
    );
  });
});

describe("MappingStepForm — the mapping-step caution (M2)", () => {
  it("warns that saving creates a processing activity used elsewhere in the product", async () => {
    renderForm();
    const caution = await screen.findByTestId("mapping-caution");
    expect(caution).toHaveTextContent(/processing activity/i);
    expect(caution).toHaveTextContent(/visible elsewhere in the product/i);
  });
});

describe("MappingStepForm — gated on SYSTEM_UPDATE too, not screening alone (M7)", () => {
  it("disables Save and explains why when the user lacks System Update", async () => {
    mockUserScopes = ["privacycare_screening:create"];
    renderForm();

    expect(
      await screen.findByTestId("missing-system-update-notice"),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /save mapping/i }),
    ).toBeDisabled();
  });

  it("enables Save and shows no notice when the user holds System Update", async () => {
    mockUserScopes = ["privacycare_screening:create", "system:update"];
    renderForm();

    await screen.findByTestId("input-mapping-name");
    expect(
      screen.queryByTestId("missing-system-update-notice"),
    ).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /save mapping/i })).toBeEnabled();
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
