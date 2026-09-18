/**
 * PrivacyCare (spec 2026-09-16 D-W2-7g)
 *
 * Display labels and colours for the risk register. DESIGN.md's language
 * table binds "risk band" as the only word for the four levels (never
 * "score" or "rating"), and its accessibility section requires text to
 * carry every status, colour only reinforcing — every label below is a
 * word, and every colour mapping exists purely as reinforcement.
 */
import { CUSTOM_TAG_COLOR } from "fidesui";

import { RiskBand, RiskCategory } from "./risk.types";

export const RISK_BAND_LABELS: Record<RiskBand, string> = {
  [RiskBand.LOW]: "Low",
  [RiskBand.MEDIUM]: "Medium",
  [RiskBand.HIGH]: "High",
  [RiskBand.CRITICAL]: "Critical",
};

// LOW/MEDIUM/HIGH follow the same DEFAULT/WARNING/ERROR ramp constants.ts
// already uses for the assessment card's (lossy, three-value) risk tag.
// CRITICAL gets its own colour, ALERT, rather than reusing ERROR — the
// entire reason this screen reads the risk API instead of risk_level is
// that CRITICAL and HIGH are NOT the same finding, and a screen that gave
// them the same colour would blur back into the distinction it exists to
// preserve.
export const RISK_BAND_TAG_COLORS: Record<RiskBand, CUSTOM_TAG_COLOR> = {
  [RiskBand.LOW]: CUSTOM_TAG_COLOR.DEFAULT,
  [RiskBand.MEDIUM]: CUSTOM_TAG_COLOR.WARNING,
  [RiskBand.HIGH]: CUSTOM_TAG_COLOR.ERROR,
  [RiskBand.CRITICAL]: CUSTOM_TAG_COLOR.ALERT,
};

// The seven categories, DESIGN.md's own wording, "unchanged from the
// privacy subject-matter expert's model" — carries across from
// register.py's _CATEGORIES in the same order.
export const RISK_CATEGORY_LABELS: Record<RiskCategory, string> = {
  [RiskCategory.CONFIDENTIALITY]: "Confidentiality",
  [RiskCategory.INTEGRITY]: "Integrity",
  [RiskCategory.AVAILABILITY]: "Availability",
  [RiskCategory.DISCRIMINATION]: "Discrimination or unfair treatment",
  [RiskCategory.LOSS_OF_AUTONOMY]: "Loss of autonomy or control",
  [RiskCategory.FINANCIAL_OR_REPUTATIONAL_HARM]:
    "Financial or reputational harm",
  [RiskCategory.PHYSICAL_HARM]: "Physical harm",
};

export const RISK_CATEGORY_OPTIONS = Object.values(RiskCategory).map(
  (value) => ({ value, label: RISK_CATEGORY_LABELS[value] }),
);

// Anchors for the 1-5 likelihood/severity scale, shown alongside the raw
// number so non-technical staff (DESIGN.md's stated audience) aren't asked
// to intuit what "3" means on its own. Not part of DESIGN.md's own
// vocabulary table — these are UX anchors, not a defined term, so no
// language-table entry governs them.
export const LIKELIHOOD_LABELS: Record<number, string> = {
  1: "Rare",
  2: "Unlikely",
  3: "Possible",
  4: "Likely",
  5: "Almost certain",
};

export const SEVERITY_LABELS: Record<number, string> = {
  1: "Negligible",
  2: "Minor",
  3: "Moderate",
  4: "Major",
  5: "Severe",
};

export const SCALE_OPTIONS = (labels: Record<number, string>) =>
  [1, 2, 3, 4, 5].map((n) => ({ value: n, label: `${n} — ${labels[n]}` }));
