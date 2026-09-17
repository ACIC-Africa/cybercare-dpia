// PrivacyCare (spec 2026-09-16 D-W2-7g)
import { render, screen } from "@testing-library/react";

import { AssessmentTaskPopoverContent } from "./AssessmentTaskPopoverContent";
import { AssessmentTaskResponse, TaskStatus } from "./types";

// ── Mocks ──────────────────────────────────────────────────────────────

jest.mock("~/features/common/hooks/useRelativeTime", () => ({
  useRelativeTime: () => "5 minutes ago",
}));

// ── Helpers ────────────────────────────────────────────────────────────

const makeTask = (
  overrides: Partial<AssessmentTaskResponse> & { skipped_count?: number } = {},
): AssessmentTaskResponse => ({
  id: "task-1",
  action_type: "generate",
  status: TaskStatus.IN_PROCESSING,
  total_count: 87,
  completed_count: 60,
  progress: 69,
  message: null,
  assessment_types: ["dpia"],
  system_fides_keys: null,
  systems: null,
  created_by: "test@example.com",
  use_llm: true,
  llm_model: "gpt-4",
  assessment_ids: [],
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
  ...overrides,
});

// The "Completed"/"Failed" status text appears twice when a run finished
// with no skips — once in the status Tag, once as the timestamp item's
// label — so status assertions look for the Tag specifically rather than
// any element with that text.
const getStatusTag = (text: string) =>
  screen.getAllByText(text).find((el) => el.className.includes("ant-tag"));

// ── Tests ──────────────────────────────────────────────────────────────

describe("AssessmentTaskPopoverContent — in progress", () => {
  it("shows the completed count and the screened-out count when the run has skips", () => {
    render(
      <AssessmentTaskPopoverContent
        activeTask={makeTask({
          completed_count: 60,
          total_count: 87,
          skipped_count: 27,
        })}
        lastCompletedTask={null}
      />,
    );

    expect(screen.getByText(/60 of 87 assessments/)).toBeInTheDocument();
    expect(screen.getByText(/27 screened out/)).toBeInTheDocument();
  });

  it("shows the counts and no screening text when the run has no skips", () => {
    render(
      <AssessmentTaskPopoverContent
        activeTask={makeTask({
          completed_count: 60,
          total_count: 87,
          skipped_count: 0,
        })}
        lastCompletedTask={null}
      />,
    );

    expect(screen.getByText(/60 of 87 assessments/)).toBeInTheDocument();
    expect(screen.queryByText(/screened out/)).not.toBeInTheDocument();
  });
});

describe("AssessmentTaskPopoverContent — completed", () => {
  it("shows how many assessments were produced and how many were screened out", () => {
    render(
      <AssessmentTaskPopoverContent
        activeTask={null}
        lastCompletedTask={makeTask({
          status: TaskStatus.COMPLETE,
          completed_count: 60,
          total_count: 87,
          skipped_count: 27,
        })}
      />,
    );

    expect(getStatusTag("Completed")).toBeInTheDocument();
    expect(screen.getByText(/60 assessments produced/)).toBeInTheDocument();
    expect(screen.getByText(/27 screened out/)).toBeInTheDocument();
  });

  it("shows the produced count and no screening text when nothing was skipped", () => {
    render(
      <AssessmentTaskPopoverContent
        activeTask={null}
        lastCompletedTask={makeTask({
          status: TaskStatus.COMPLETE,
          completed_count: 87,
          total_count: 87,
          skipped_count: 0,
        })}
      />,
    );

    expect(screen.getByText(/87 assessments produced/)).toBeInTheDocument();
    expect(screen.queryByText(/screened out/)).not.toBeInTheDocument();
  });

  it("does not read as a failure when every activity was screened out", () => {
    render(
      <AssessmentTaskPopoverContent
        activeTask={null}
        lastCompletedTask={makeTask({
          status: TaskStatus.COMPLETE,
          completed_count: 0,
          total_count: 27,
          skipped_count: 27,
        })}
      />,
    );

    expect(getStatusTag("Completed")).toBeInTheDocument();
    expect(screen.queryByText("Failed")).not.toBeInTheDocument();
    expect(screen.getByText(/0 assessments produced/)).toBeInTheDocument();
    expect(screen.getByText(/27 screened out/)).toBeInTheDocument();
  });
});
