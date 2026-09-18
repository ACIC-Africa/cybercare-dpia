import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { FindingResponse } from "./discovery-findings.types";
import { ReconcileFindingModal } from "./ReconcileFindingModal";

// PrivacyCare — Discovery, Screen 4 of docs/design/privacycare-screens/DESIGN.md.
// A real discovered table from the live scan (accessmanualwebhook, one of
// the 176 real Table rows — see discovery-findings-api-report.md), not a
// placeholder.
const NEEDS_REVIEW_FINDING: FindingResponse = {
  urn: "privacycare_local_discovery.fides.public.accessmanualwebhook",
  table_name: "accessmanualwebhook",
  schema_name: "public",
  monitor_key: "privacycare_local_discovery",
  field_count: 5,
  table_type: null,
  diff_status: "addition",
  state: "needs_review",
  system_id: null,
  system_name: null,
  reason: null,
  decided_by: null,
  decided_at: null,
};

const mockReconcile = jest.fn(() => ({
  unwrap: () => Promise.resolve({}),
}));

jest.mock("./discovery-findings.slice", () => ({
  useReconcileDiscoveryFindingMutation: () => [
    mockReconcile,
    { isLoading: false },
  ],
}));

// SystemSelect (2026-09-18 fix) is the "mark as mapped" picker now — a real
// systems listing, keyed by fides_key, same as every other system picker in
// this codebase. "Fuel Card Issuance" / "privacycare_process_bp_9e332dcbe707"
// is a real system from the live database (same one the old candidate-
// derivation fixture used, by its OTHER identifier — id sys_4481f3505f6b).
const mockUseGetSystemsQuery = jest.fn(() => ({
  data: {
    items: [
      {
        fides_key: "privacycare_process_bp_9e332dcbe707",
        name: "Fuel Card Issuance",
      },
    ],
  },
  isFetching: false,
}));

jest.mock("~/features/system/system.slice", () => ({
  useGetSystemsQuery: (...args: unknown[]) => mockUseGetSystemsQuery(...args),
}));

// useMessage needs a FidesUIProvider ancestor this test does not stand up —
// same gap RopaList.test.tsx and RecordDecisionModal.test.tsx document for
// their own use of it.
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

// Same close-guard fake RecordDecisionModal.test.tsx documents for its own
// use: ConfirmCloseModal's real Modal.confirm dialog needs a FidesUIProvider
// this test does not stand up, but the actual decision rule (close only
// when getIsDirty() is false) stays real, not a passthrough.
jest.mock("~/features/common/hooks/useConfirmDirtyClose", () => ({
  __esModule: true,
  default: (onClose: () => void, getIsDirty: () => boolean) => () => {
    if (!getIsDirty()) {
      onClose();
    }
  },
}));

const renderModal = (finding: FindingResponse = NEEDS_REVIEW_FINDING) => {
  const onClose = jest.fn();
  render(<ReconcileFindingModal open onClose={onClose} finding={finding} />);
  return { onClose };
};

beforeEach(() => {
  jest.clearAllMocks();
});

describe("ReconcileFindingModal — ignoring requires a written reason", () => {
  it("defaults to the ignore choice with the submit control disabled until a reason is given", () => {
    renderModal();

    expect(screen.getByTestId("reconcile-choice-ignored")).toBeChecked();
    expect(screen.getByTestId("reconcile-submit")).toBeDisabled();
  });

  it("stays disabled for a whitespace-only reason", async () => {
    const user = userEvent.setup();
    renderModal();

    await user.type(screen.getByTestId("reconcile-reason"), "   ");
    expect(screen.getByTestId("reconcile-submit")).toBeDisabled();
  });

  it("enables submit once a real reason is typed, and records an ignored reconciliation", async () => {
    const user = userEvent.setup();
    renderModal();

    await user.type(
      screen.getByTestId("reconcile-reason"),
      "Internal Fides audit log table, holds no personal data.",
    );
    expect(screen.getByTestId("reconcile-submit")).not.toBeDisabled();

    await user.click(screen.getByTestId("reconcile-submit"));

    await waitFor(() => {
      expect(mockReconcile).toHaveBeenCalledWith({
        urn: NEEDS_REVIEW_FINDING.urn,
        body: {
          state: "ignored",
          reason: "Internal Fides audit log table, holds no personal data.",
        },
      });
    });
  });
});

describe("ReconcileFindingModal — mapping to a system", () => {
  it("keeps submit disabled until a system is chosen", async () => {
    const user = userEvent.setup();
    renderModal();

    await user.click(screen.getByTestId("reconcile-choice-mapped"));

    expect(screen.getByTestId("reconcile-submit")).toBeDisabled();
  });

  it("offers the real systems list (2026-09-18 fix: no longer limited to systems some other finding already named) and records the reconciliation by fides_key", async () => {
    const user = userEvent.setup();
    renderModal();

    await user.click(screen.getByTestId("reconcile-choice-mapped"));

    await user.click(screen.getByTestId("reconcile-system-select"));
    await user.click(await screen.findByTitle("Fuel Card Issuance"));

    expect(screen.getByTestId("reconcile-submit")).not.toBeDisabled();
    await user.click(screen.getByTestId("reconcile-submit"));

    await waitFor(() => {
      expect(mockReconcile).toHaveBeenCalledWith({
        urn: NEEDS_REVIEW_FINDING.urn,
        body: {
          state: "mapped",
          system_fides_key: "privacycare_process_bp_9e332dcbe707",
        },
      });
    });
  }, 15000); // Bumped from the default 5000ms: this test drives the antd Select's
  // real virtualised dropdown (radio switch, open, pick an option, then
  // submit) rather than a plain field, measurably slower than the
  // ignore-path tests above.
});

describe("ReconcileFindingModal — a reconciliation is a permanent record", () => {
  it("shows the permanence caution above the submit button in every state", () => {
    renderModal();
    expect(
      screen.getByTestId("reconcile-permanence-caution"),
    ).toBeInTheDocument();
  });
});

describe("ReconcileFindingModal — save failure", () => {
  it("shows the server's error and keeps the entered reason", async () => {
    mockReconcile.mockReturnValueOnce({
      // eslint-disable-next-line prefer-promise-reject-errors
      unwrap: () => Promise.reject({ status: 400, data: { detail: "boom" } }),
    });
    const user = userEvent.setup();
    renderModal();

    await user.type(screen.getByTestId("reconcile-reason"), "A real reason");
    await user.click(screen.getByTestId("reconcile-submit"));

    expect(await screen.findByText("boom")).toBeInTheDocument();
    expect(screen.getByTestId("reconcile-reason")).toHaveValue("A real reason");
  });
});
