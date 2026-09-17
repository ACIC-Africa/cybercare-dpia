import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { RecordDecisionModal } from "./RecordDecisionModal";

// PrivacyCare (spec 2026-09-16 D-W2-7g): Carol's six triggers, transcribed
// character for character from scripts/privacycare/seed_screening_triggers.py
// — the same real seeded rows the live API returns, used here instead of
// placeholder question text so this test can also prove the component
// renders whatever the API answers rather than a copy baked into the
// component (see the "renders from the API" test below).
const TRIGGERS = [
  {
    id: "trg-1",
    trigger_key: "large_scale",
    label: "Large-scale processing",
    description: "High volume of data subjects, records, or geographic spread.",
    display_order: 1,
  },
  {
    id: "trg-2",
    trigger_key: "special_category",
    label: "Special category or highly sensitive data",
    description:
      "Any of the special categories under the Data Protection Act 2019 " +
      "§2 — including health and HIV status, biometric and genetic " +
      "data, conscience, belief, and well-being.",
    display_order: 2,
  },
  {
    id: "trg-3",
    trigger_key: "systematic_monitoring",
    label: "Systematic monitoring",
    description: "Ongoing observation, tracking, or profiling of individuals.",
    display_order: 3,
  },
  {
    id: "trg-4",
    trigger_key: "new_technology",
    label: "New or unproven technology",
    description:
      "AI/ML, biometric matching, or a system not previously deployed at this scale.",
    display_order: 4,
  },
  {
    id: "trg-5",
    trigger_key: "automated_decision",
    label: "Automated decision-making with legal or similarly significant effect",
    description: "Decisions made about a person with no meaningful human review.",
    display_order: 5,
  },
  {
    id: "trg-6",
    trigger_key: "vulnerable_subjects",
    label: "Processing involving vulnerable data subjects",
    description:
      "Minors, dependants, job applicants, individuals captured on CCTV, " +
      "witnesses, complainants, suspected offenders, or anyone in a " +
      "relationship of economic dependence on the company.",
    display_order: 6,
  },
];

const mockRecordDecision = jest.fn(() => ({ unwrap: () => Promise.resolve({}) }));
const mockGetScreeningTriggersQuery = jest.fn();

jest.mock("./screening.slice", () => ({
  useGetScreeningTriggersQuery: () => mockGetScreeningTriggersQuery(),
  useRecordScreeningDecisionMutation: () => [
    mockRecordDecision,
    { isLoading: false },
  ],
}));

// The mapping step is its own component with its own tests
// (MappingStepForm.test.tsx) — stubbed here so this file can test the
// decision step in isolation, the same way AssessmentDetail.test.tsx stubs
// out QuestionCard/EvidenceDrawer/etc.
jest.mock("./MappingStepForm", () => ({
  MappingStepForm: ({ processName }: { processName: string }) => (
    <div data-testid="mapping-step-stub">Mapping step for {processName}</div>
  ),
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

// ConfirmCloseModal's own close-guard needs fidesui's Modal.confirm API,
// which in turn needs a FidesUIProvider ancestor this test does not stand
// up — the guard behaviour itself (confirm-before-discard) is
// ConfirmCloseModal's own concern, not this component's, so it is stubbed
// to a plain passthrough here the same way AssessmentDetail.test.tsx stubs
// useMessage/useNotification for the same reason, one level up the stack.
jest.mock(
  "~/features/common/hooks/useConfirmDirtyClose",
  () => ({
    __esModule: true,
    default: (onClose: () => void) => onClose,
  }),
);

const renderModal = () =>
  render(
    <RecordDecisionModal
      open
      onClose={jest.fn()}
      businessProcessId="bp_94d5439ced86"
      processName="Fuel Card Issuance"
      hasMapping={false}
    />,
  );

beforeEach(() => {
  jest.clearAllMocks();
  mockGetScreeningTriggersQuery.mockReturnValue({
    data: { triggers: TRIGGERS },
    isLoading: false,
    isError: false,
    refetch: jest.fn(),
  });
});

describe("RecordDecisionModal — questions come from the API", () => {
  it("renders every trigger's real label and description from the query, not a local constant", () => {
    renderModal();
    TRIGGERS.forEach((trigger) => {
      expect(screen.getByText(trigger.label)).toBeInTheDocument();
      expect(screen.getByText(trigger.description)).toBeInTheDocument();
    });
  });

  it("renders whatever the API answers, proving there is no hardcoded copy to fall back on", () => {
    mockGetScreeningTriggersQuery.mockReturnValue({
      data: {
        triggers: [
          {
            id: "trg-x",
            trigger_key: "revised_by_sme",
            label: "A question Carol revised this morning",
            description: "Not present anywhere in this component's source.",
            display_order: 1,
          },
        ],
      },
      isLoading: false,
      isError: false,
      refetch: jest.fn(),
    });
    renderModal();
    expect(
      screen.getByText("A question Carol revised this morning"),
    ).toBeInTheDocument();
  });
});

describe("RecordDecisionModal — the reason field and the live consequence", () => {
  it("shows the reason field and the not-applicable consequence when nothing is ticked", () => {
    renderModal();
    expect(screen.getByTestId("input-justification")).toBeInTheDocument();
    expect(screen.getByTestId("decision-consequence")).toHaveTextContent(
      /not applicable/i,
    );
  });

  it("hides the reason field once any question is ticked, and states the process is applicable", async () => {
    const user = userEvent.setup();
    renderModal();

    await user.click(screen.getByTestId("trigger-large_scale"));

    expect(screen.queryByTestId("input-justification")).not.toBeInTheDocument();
    expect(screen.getByTestId("decision-consequence")).toHaveTextContent(
      /is applicable and needs an assessment/i,
    );
  });
});

describe("RecordDecisionModal — validation", () => {
  it("keeps Record decision disabled when not applicable and the reason is blank", () => {
    renderModal();
    expect(screen.getByRole("button", { name: /record decision/i })).toBeDisabled();
  });

  it("keeps Record decision disabled when the reason is whitespace only", async () => {
    const user = userEvent.setup();
    renderModal();
    await user.type(screen.getByTestId("input-justification"), "   ");
    expect(screen.getByRole("button", { name: /record decision/i })).toBeDisabled();
  });

  it("enables Record decision once a real reason is typed", async () => {
    const user = userEvent.setup();
    renderModal();
    await user.type(
      screen.getByTestId("input-justification"),
      "No personal data is processed for this activity.",
    );
    expect(screen.getByRole("button", { name: /record decision/i })).toBeEnabled();
  });

  it("does not require a reason once a question is ticked", async () => {
    const user = userEvent.setup();
    renderModal();
    await user.click(screen.getByTestId("trigger-special_category"));
    expect(screen.getByRole("button", { name: /record decision/i })).toBeEnabled();
  });
});

describe("RecordDecisionModal — the permanence caution", () => {
  it("shows the caution before the submit button, never after it", () => {
    renderModal();
    const caution = screen.getByTestId("permanence-caution");
    const submitButton = screen.getByRole("button", { name: /record decision/i });

    expect(caution).toBeInTheDocument();
    expect(caution).toHaveTextContent(/permanent record/i);
    expect(caution).toHaveTextContent(/cannot be edited or deleted/i);

    // DOM_FOLLOWING means `caution` comes before `submitButton` in document
    // order — DESIGN.md: "above the submit button, not after it".
    // eslint-disable-next-line no-bitwise
    expect(
      caution.compareDocumentPosition(submitButton) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });
});
