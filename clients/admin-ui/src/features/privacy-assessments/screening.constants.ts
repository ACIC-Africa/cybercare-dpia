/**
 * PrivacyCare (spec 2026-09-16 D-W2-7g)
 *
 * Display-only constants for the DPIA screening & mapping screen. Never a
 * source of the screening questions themselves — those are fetched from
 * `GET /api/v1/privacycare/screening/triggers`, see screening.slice.ts.
 */
import { TagProps } from "fidesui";

import { ScreeningDisplayStatus } from "./screening.types";

type TagColor = TagProps["color"];

// Carol's word, 2026-09-17 — "applicable" / "not applicable" on screen,
// never "screened out". See docs/design/privacycare-screens/DESIGN.md's
// language table: a synonym for a locked term is a defect.
export const SCREENING_STATUS_LABELS: Record<ScreeningDisplayStatus, string> = {
  not_screened: "Not screened",
  applicable: "Applicable",
  not_applicable: "Not applicable",
};

// Text carries the meaning; colour only reinforces it (DESIGN.md, both the
// per-screen "Status" column note and the shared Accessibility section).
export const SCREENING_STATUS_TAG_COLORS: Record<
  ScreeningDisplayStatus,
  TagColor
> = {
  not_screened: "warning",
  applicable: "success",
  not_applicable: "default",
};

export const MAPPING_STATUS_LABELS = {
  complete: "Complete",
  not_started: "Not started",
} as const;

export const MAPPING_STATUS_TAG_COLORS: Record<
  keyof typeof MAPPING_STATUS_LABELS,
  TagColor
> = {
  complete: "success",
  not_started: "default",
};

// Mirrors `SPECIAL_TAG` in
// src/fides/api/privacycare/taxonomy/kenyan.py — a plain string, not an
// import, because that module lives on the Python side of the fork and has
// no TS counterpart. If that constant's value ever changes, this one has to
// change with it by hand.
export const KENYAN_SPECIAL_CATEGORY_TAG = "dpa2019:special_category";

export const KENYAN_SPECIAL_CATEGORY_DESCRIPTION =
  "health and HIV status, biometric and genetic data, conscience, belief or well-being";
