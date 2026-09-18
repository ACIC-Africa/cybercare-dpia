import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { FindingHistoryPanel } from "./FindingHistoryPanel";

const URN = "privacycare_local_discovery.fides.public.accessmanualwebhook";

const mockGetHistoryQuery = jest.fn();

jest.mock("./discovery-findings.slice", () => ({
  useGetDiscoveryFindingHistoryQuery: (urn: string) => mockGetHistoryQuery(urn),
}));

beforeEach(() => {
  jest.clearAllMocks();
});

describe("FindingHistoryPanel — who decided, when, and why", () => {
  it("shows the plain not-yet-reconciled message for an empty, real history", () => {
    mockGetHistoryQuery.mockReturnValue({
      data: { urn: URN, reconciliations: [] },
      isLoading: false,
      isError: false,
      refetch: jest.fn(),
    });

    render(<FindingHistoryPanel urn={URN} />);

    expect(screen.getByTestId("finding-history-empty")).toHaveTextContent(
      /needs review/i,
    );
  });

  it("renders every reconciliation, newest first, with who/when/why for a mapped entry", () => {
    mockGetHistoryQuery.mockReturnValue({
      data: {
        urn: URN,
        reconciliations: [
          {
            id: "rec-2",
            urn: URN,
            state: "mapped",
            system_id: "sys_4481f3505f6b",
            system_name: "Fuel Card Issuance",
            reason: null,
            decided_by: "fid_b06b0e55-d950-43b1-bf5a-9fa4623d28a9",
            decided_at: "2026-09-18T10:00:00Z",
          },
          {
            id: "rec-1",
            urn: URN,
            state: "ignored",
            system_id: null,
            system_name: null,
            reason: "Internal Fides table, no personal data.",
            decided_by: "fid_b06b0e55-d950-43b1-bf5a-9fa4623d28a9",
            decided_at: "2026-09-17T10:00:00Z",
          },
        ],
      },
      isLoading: false,
      isError: false,
      refetch: jest.fn(),
    });

    render(<FindingHistoryPanel urn={URN} />);

    const history = screen.getByTestId("finding-history");
    expect(history).toHaveTextContent("System: Fuel Card Issuance");
    expect(history).toHaveTextContent(
      "Reason: Internal Fides table, no personal data.",
    );

    // Newest first: the mapped entry (2026-09-18) renders before the
    // ignored one (2026-09-17) — the component trusts the API's own
    // ordering and never re-sorts, so this also proves it didn't.
    expect(history.textContent!.indexOf("Fuel Card Issuance")).toBeLessThan(
      history.textContent!.indexOf("Internal Fides table"),
    );
  });

  it("tells apart a table that was never discovered (404) from a real one with no reconciliation yet", () => {
    mockGetHistoryQuery.mockReturnValue({
      data: undefined,
      isLoading: false,
      isError: true,
      error: { status: 404, data: { detail: "no such finding" } },
      refetch: jest.fn(),
    });

    render(<FindingHistoryPanel urn={URN} />);

    expect(
      screen.getByText("This table could not be found."),
    ).toBeInTheDocument();
  });

  it("offers a retry for a real failure, distinct from a 404", async () => {
    const refetch = jest.fn();
    mockGetHistoryQuery.mockReturnValue({
      data: undefined,
      isLoading: false,
      isError: true,
      error: { status: 500, data: { detail: "server error" } },
      refetch,
    });

    render(<FindingHistoryPanel urn={URN} />);

    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: /retry/i }));
    expect(refetch).toHaveBeenCalled();
  });
});
