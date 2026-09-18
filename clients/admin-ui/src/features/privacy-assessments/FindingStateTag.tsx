import { Tag, TagProps } from "fidesui";

import { FindingState } from "./discovery-findings.types";

// DESIGN.md Screen 4's own three states, verbatim. Text carries the
// meaning; colour only reinforces it (Accessibility section, shared by
// both screens): every Tag here renders the state word itself, never a
// colour standing in for it.
export const FINDING_STATE_LABELS: Record<FindingState, string> = {
  mapped: "Already mapped",
  ignored: "Ignored",
  needs_review: "Needs review",
};

const FINDING_STATE_TAG_COLORS: Record<FindingState, TagProps["color"]> = {
  mapped: "success",
  ignored: "default",
  needs_review: "warning",
};

export const FindingStateTag = ({ state }: { state: FindingState }) => (
  <Tag
    color={FINDING_STATE_TAG_COLORS[state]}
    data-testid={`finding-state-${state}`}
  >
    {FINDING_STATE_LABELS[state]}
  </Tag>
);
