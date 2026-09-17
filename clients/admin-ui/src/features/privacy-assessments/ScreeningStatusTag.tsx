import { Tag } from "fidesui";

import {
  MAPPING_STATUS_LABELS,
  MAPPING_STATUS_TAG_COLORS,
  SCREENING_STATUS_LABELS,
  SCREENING_STATUS_TAG_COLORS,
} from "./screening.constants";
import {
  screeningDisplayStatus,
  ScreeningStatusResponse,
} from "./screening.types";

// PrivacyCare (spec 2026-09-16 D-W2-7g): text carries the meaning here,
// colour only reinforces it (DESIGN.md) — every one of these Tags renders
// the status word itself, never a colour standing in for it.
export const ScreeningStatusTag = ({
  row,
}: {
  row: Pick<ScreeningStatusResponse, "dpia_required">;
}) => {
  const status = screeningDisplayStatus(row);
  return (
    <Tag color={SCREENING_STATUS_TAG_COLORS[status]}>
      {SCREENING_STATUS_LABELS[status]}
    </Tag>
  );
};

export const MappingStatusTag = ({
  row,
}: {
  row: Pick<ScreeningStatusResponse, "dpia_required" | "has_mapping">;
}) => {
  const status = screeningDisplayStatus(row);
  // Mapping is only meaningful once a process is applicable — DESIGN.md:
  // "Only meaningful when applicable." Not screened / not applicable both
  // read as an em dash rather than a mapping status that has no referent.
  if (status !== "applicable") {
    return <span>—</span>;
  }
  const key = row.has_mapping ? "complete" : "not_started";
  return (
    <Tag color={MAPPING_STATUS_TAG_COLORS[key]}>
      {MAPPING_STATUS_LABELS[key]}
    </Tag>
  );
};
