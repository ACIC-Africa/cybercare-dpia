import { render, screen } from "@testing-library/react";

import { MappingStatusTag, ScreeningStatusTag } from "./ScreeningStatusTag";
import { ScreeningStatusResponse } from "./screening.types";

// PrivacyCare (spec 2026-09-16 D-W2-7g): "Fuel Card Issuance" (Card
// Operations) is a real, live business process — applicable and mapped —
// used throughout these tests instead of a placeholder name.
const baseRow: ScreeningStatusResponse = {
  business_process_id: "bp_94d5439ced86",
  name: "Fuel Card Issuance",
  business_cycle: "Card Operations",
  dpia_required: null,
  decided_by: null,
  decided_at: null,
  has_mapping: false,
};

describe("ScreeningStatusTag", () => {
  it('renders "Not screened" for a process with no verdict yet', () => {
    render(<ScreeningStatusTag row={{ dpia_required: null }} />);
    expect(screen.getByText("Not screened")).toBeInTheDocument();
  });

  it('renders "Applicable" when dpia_required is true', () => {
    render(<ScreeningStatusTag row={{ dpia_required: true }} />);
    expect(screen.getByText("Applicable")).toBeInTheDocument();
  });

  it('renders "Not applicable" when dpia_required is false — never "screened out"', () => {
    render(<ScreeningStatusTag row={{ dpia_required: false }} />);
    expect(screen.getByText("Not applicable")).toBeInTheDocument();
    expect(screen.queryByText(/screened out/i)).not.toBeInTheDocument();
  });
});

describe("MappingStatusTag", () => {
  it("reads as an em dash when the process has never been screened", () => {
    render(<MappingStatusTag row={{ ...baseRow, dpia_required: null }} />);
    expect(screen.getByText("—")).toBeInTheDocument();
  });

  it("reads as an em dash when the process is not applicable — mapping is not meaningful there", () => {
    render(
      <MappingStatusTag row={{ ...baseRow, dpia_required: false, has_mapping: false }} />,
    );
    expect(screen.getByText("—")).toBeInTheDocument();
  });

  it('renders "Complete" for an applicable, mapped process (Fuel Card Issuance)', () => {
    render(
      <MappingStatusTag row={{ ...baseRow, dpia_required: true, has_mapping: true }} />,
    );
    expect(screen.getByText("Complete")).toBeInTheDocument();
  });

  it('renders "Not started" for an applicable, unmapped process', () => {
    render(
      <MappingStatusTag row={{ ...baseRow, dpia_required: true, has_mapping: false }} />,
    );
    expect(screen.getByText("Not started")).toBeInTheDocument();
  });
});
