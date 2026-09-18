import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { RemoveRiskModal } from "./RemoveRiskModal";
import { RiskResponse } from "./risk.types";

// PrivacyCare (spec 2026-09-16 D-W2-7g): real fuel-retailer risks, not
// placeholder text — same fixtures as RiskRegisterSection.test.tsx.
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
const LOW_RISK: RiskResponse = {
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

const mockRemoveRisk = jest.fn(() => ({
  unwrap: () => Promise.resolve({ id: "risk-3", removed: true }),
}));

jest.mock("./risk.slice", () => ({
  useRemoveRiskMutation: () => [mockRemoveRisk, { isLoading: false }],
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

beforeEach(() => {
  jest.clearAllMocks();
});

describe("RemoveRiskModal — naming the risk (test requirement: confirmation names the risk removed)", () => {
  it("names the risk's category and description being removed", () => {
    render(
      <RemoveRiskModal
        open
        onClose={jest.fn()}
        assessmentId="pa_018a033614e6"
        risk={CRITICAL_RISK}
        remainingRisks={[LOW_RISK]}
      />,
    );

    expect(
      screen.getByText("Financial or reputational harm"),
    ).toBeInTheDocument();
    expect(screen.getByText(CRITICAL_RISK.description)).toBeInTheDocument();
  });

  it("always states removal cannot be undone", () => {
    render(
      <RemoveRiskModal
        open
        onClose={jest.fn()}
        assessmentId="pa_018a033614e6"
        risk={LOW_RISK}
        remainingRisks={[]}
      />,
    );

    expect(screen.getByText("Removal cannot be undone.")).toBeInTheDocument();
  });
});

describe("RemoveRiskModal — the band-change preview", () => {
  it("says what the band becomes when removal would change it", () => {
    // Removing the ONLY critical risk, leaving one low risk — the
    // assessment's band drops from critical to low.
    render(
      <RemoveRiskModal
        open
        onClose={jest.fn()}
        assessmentId="pa_018a033614e6"
        risk={CRITICAL_RISK}
        remainingRisks={[LOW_RISK]}
      />,
    );

    // Fix wave (Screen 2 review), finding 3: this used to pin the raw wire
    // values ("critical"/"low"), which made the component's own regression
    // (rendering unlabelled band strings) look intentional. Every other
    // band on screen renders through RISK_BAND_LABELS ("Critical"/"Low");
    // this sentence must match that, not the wire value.
    expect(
      screen.getByText("The risk band will change from Critical to Low."),
    ).toBeInTheDocument();
  });

  it("says nothing about a band change when the band would stay the same", () => {
    // Removing a low risk while a critical one remains — band stays
    // critical either way.
    render(
      <RemoveRiskModal
        open
        onClose={jest.fn()}
        assessmentId="pa_018a033614e6"
        risk={LOW_RISK}
        remainingRisks={[CRITICAL_RISK]}
      />,
    );

    expect(
      screen.queryByText(/risk band will change/i),
    ).not.toBeInTheDocument();
  });
});

describe("RemoveRiskModal — confirming and cancelling", () => {
  it("calls the remove mutation with the risk and assessment id on confirm", async () => {
    const user = userEvent.setup();
    const onClose = jest.fn();
    render(
      <RemoveRiskModal
        open
        onClose={onClose}
        assessmentId="pa_018a033614e6"
        risk={CRITICAL_RISK}
        remainingRisks={[LOW_RISK]}
      />,
    );

    await user.click(screen.getByTestId("confirm-remove-risk"));

    expect(mockRemoveRisk).toHaveBeenCalledWith({
      riskId: "risk-3",
      assessmentId: "pa_018a033614e6",
    });
  });

  it("closes without removing anything when Cancel is clicked", async () => {
    const user = userEvent.setup();
    const onClose = jest.fn();
    render(
      <RemoveRiskModal
        open
        onClose={onClose}
        assessmentId="pa_018a033614e6"
        risk={CRITICAL_RISK}
        remainingRisks={[LOW_RISK]}
      />,
    );

    await user.click(screen.getByRole("button", { name: /cancel/i }));

    expect(mockRemoveRisk).not.toHaveBeenCalled();
    expect(onClose).toHaveBeenCalled();
  });
});
