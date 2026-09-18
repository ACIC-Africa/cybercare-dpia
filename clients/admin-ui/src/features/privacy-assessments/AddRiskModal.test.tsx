import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { AddRiskModal } from "./AddRiskModal";
import { RiskBand } from "./risk.types";

// Real AntD Select interactions (open dropdown, click option) are slower
// than the default 5s per-test budget once several run back to back in one
// file — each passes comfortably in isolation; this only gives them room
// under shared-run load.
jest.setTimeout(15000);

const mockAddRisk = jest.fn(() => ({
  unwrap: () =>
    Promise.resolve({
      id: "risk-new",
      assessment_id: "pa_018a033614e6",
      category: "financial_or_reputational_harm",
      description:
        "A cloned fuel card PIN allows unauthorised fuel draw-downs.",
      likelihood: 5,
      severity: 5,
      score: 25,
      band: "critical",
    }),
}));

jest.mock("./risk.slice", () => ({
  useAddRiskMutation: () => [mockAddRisk, { isLoading: false }],
}));

// useMessage needs a FidesUIProvider ancestor this test does not stand up
// (same gap RecordDecisionModal.test.tsx documents for its own use of
// useMessage) — stubbed the same way.
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

// Real dirty-guard CONTRACT, not a passthrough — same discipline
// RecordDecisionModal.test.tsx's own I2 fix documents: a stub that ignores
// getIsDirty entirely would let a regression back to "dirty guard off"
// ship without a single test here failing.
jest.mock("~/features/common/hooks/useConfirmDirtyClose", () => ({
  __esModule: true,
  default: (onClose: () => void, getIsDirty: () => boolean) => () => {
    if (!getIsDirty()) {
      onClose();
    }
  },
}));

const selectOption = async (
  user: ReturnType<typeof userEvent.setup>,
  testId: string,
  optionText: string,
) => {
  await user.click(screen.getByTestId(testId));
  await user.click(await screen.findByText(optionText));
};

beforeEach(() => {
  jest.clearAllMocks();
});

describe("AddRiskModal — live consequence (DESIGN.md: show the consequence while they choose)", () => {
  it("shows the live score and band as likelihood and severity are chosen", async () => {
    const user = userEvent.setup();
    render(
      <AddRiskModal
        open
        onClose={jest.fn()}
        assessmentId="pa_018a033614e6"
        currentOverallBand={RiskBand.LOW}
      />,
    );

    await selectOption(user, "input-likelihood", "5 — Almost certain");
    await selectOption(user, "input-severity", "5 — Severe");

    const consequence = await screen.findByTestId("add-risk-consequence");
    expect(consequence).toHaveTextContent("Score 25 of 25");
    expect(consequence).toHaveTextContent("Critical");
  });

  it("warns when this entry would raise the assessment's overall band", async () => {
    const user = userEvent.setup();
    render(
      <AddRiskModal
        open
        onClose={jest.fn()}
        assessmentId="pa_018a033614e6"
        currentOverallBand={RiskBand.LOW}
      />,
    );

    await selectOption(user, "input-likelihood", "5 — Almost certain");
    await selectOption(user, "input-severity", "5 — Severe");

    expect(
      await screen.findByText(
        "This risk would raise the assessment to Critical.",
      ),
    ).toBeInTheDocument();
  });

  it("says nothing about raising the band when the entry does not exceed the current band", async () => {
    const user = userEvent.setup();
    render(
      <AddRiskModal
        open
        onClose={jest.fn()}
        assessmentId="pa_018a033614e6"
        currentOverallBand={RiskBand.CRITICAL}
      />,
    );

    await selectOption(user, "input-likelihood", "1 — Rare");
    await selectOption(user, "input-severity", "1 — Negligible");

    await screen.findByTestId("add-risk-consequence");
    expect(
      screen.queryByText(/would raise the assessment/i),
    ).not.toBeInTheDocument();
  });
});

describe("AddRiskModal — validation", () => {
  it("rejects a description that is only whitespace, and blocks submission", async () => {
    // Fix wave (Screen 2 review), finding 8: this test used to type
    // whitespace, blur, and assert `mockAddRisk` was never called — but it
    // never attempted to submit, so that assertion was true of ANY
    // unsubmitted form and proved nothing about the validator. Every other
    // field is now filled with a valid value and Submit is actually
    // clicked, so "the mutation was not called" now demonstrates the
    // whitespace-only description genuinely blocks submission, not merely
    // that nothing happens when nothing is attempted.
    const user = userEvent.setup();
    render(
      <AddRiskModal
        open
        onClose={jest.fn()}
        assessmentId="pa_018a033614e6"
        currentOverallBand={RiskBand.LOW}
      />,
    );

    await selectOption(
      user,
      "input-category",
      "Financial or reputational harm",
    );
    await selectOption(user, "input-likelihood", "5 — Almost certain");
    await selectOption(user, "input-severity", "5 — Severe");
    await user.type(screen.getByTestId("input-description"), "   ");
    await user.click(screen.getByTestId("submit-add-risk"));

    expect(
      await screen.findByText("Describe what this risk is."),
    ).toBeInTheDocument();
    expect(mockAddRisk).not.toHaveBeenCalled();
  });
});

describe("AddRiskModal — the dirty guard (do not repeat I2: Escape must not silently discard typed input)", () => {
  it("blocks a close attempt once the form has been touched", async () => {
    const user = userEvent.setup();
    const onClose = jest.fn();
    render(
      <AddRiskModal
        open
        onClose={onClose}
        assessmentId="pa_018a033614e6"
        currentOverallBand={RiskBand.LOW}
      />,
    );

    await user.type(
      screen.getByTestId("input-description"),
      "Fuel card PINs are logged in plaintext.",
    );
    await user.keyboard("{Escape}");

    expect(onClose).not.toHaveBeenCalled();
  });

  it("closes immediately when nothing has been touched", async () => {
    const user = userEvent.setup();
    const onClose = jest.fn();
    render(
      <AddRiskModal
        open
        onClose={onClose}
        assessmentId="pa_018a033614e6"
        currentOverallBand={RiskBand.LOW}
      />,
    );

    await user.keyboard("{Escape}");

    expect(onClose).toHaveBeenCalledTimes(1);
  });
});

describe("AddRiskModal — submitting", () => {
  it("submits a trimmed description with the chosen category/likelihood/severity", async () => {
    const user = userEvent.setup();
    const onClose = jest.fn();
    render(
      <AddRiskModal
        open
        onClose={onClose}
        assessmentId="pa_018a033614e6"
        currentOverallBand={RiskBand.LOW}
      />,
    );

    await selectOption(
      user,
      "input-category",
      "Financial or reputational harm",
    );
    await user.type(
      screen.getByTestId("input-description"),
      "  A cloned fuel card PIN allows unauthorised fuel draw-downs.  ",
    );
    await selectOption(user, "input-likelihood", "5 — Almost certain");
    await selectOption(user, "input-severity", "5 — Severe");

    await user.click(screen.getByTestId("submit-add-risk"));

    expect(mockAddRisk).toHaveBeenCalledWith({
      assessmentId: "pa_018a033614e6",
      body: {
        category: "financial_or_reputational_harm",
        description:
          "A cloned fuel card PIN allows unauthorised fuel draw-downs.",
        likelihood: 5,
        severity: 5,
      },
    });
  });
});
