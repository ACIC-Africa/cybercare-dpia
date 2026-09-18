import { render, screen } from "@testing-library/react";

import { AssessmentGroup } from "~/features/privacy-assessments/AssessmentGroup";

jest.mock("next/router", () => ({ useRouter: () => ({ push: jest.fn() }) }));

// The cards inside the group pull taxonomy through RTK Query, which needs a
// store. This test is about the group's own subtitle, so the card is stubbed
// rather than standing a redux Provider up around an assertion on one string.
jest.mock("~/features/privacy-assessments/AssessmentCard", () => ({
  AssessmentCard: ({ assessment }: any) => <div>{assessment.name}</div>,
}));

const assessment = (id: string, name: string) =>
  ({
    id,
    name,
    status: "in_progress",
    risk_level: null,
    completion_percentage: 17,
  }) as any;

describe("AssessmentGroup subtitle", () => {
  it("says 'assessment', singular, when there is exactly one", () => {
    // The demo's own first screen showed "1 system • 1 assessments" under
    // every group heading. systemCount was pluralised and the assessment
    // count was not.
    render(
      <AssessmentGroup
        dataUseName="Essential Fraud Detection"
        systemCount={1}
        assessments={[assessment("pa_1", "Fraud Risk Assessments")]}
      />,
    );
    expect(screen.getByText(/1 system • 1 assessment$/)).toBeInTheDocument();
  });

  it("pluralises both counts when there is more than one", () => {
    render(
      <AssessmentGroup
        dataUseName="Analytics for Reporting"
        systemCount={2}
        assessments={[
          assessment("pa_1", "Fuel Card Usage Analytics"),
          assessment("pa_2", "Digital Marketing Analytics"),
        ]}
      />,
    );
    expect(screen.getByText(/2 systems • 2 assessments/)).toBeInTheDocument();
  });
});
