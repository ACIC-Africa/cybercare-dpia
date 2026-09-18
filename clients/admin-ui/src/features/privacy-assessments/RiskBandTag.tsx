import { Tag } from "fidesui";

import { RISK_BAND_LABELS, RISK_BAND_TAG_COLORS } from "./risk.constants";
import { RiskBand } from "./risk.types";

/**
 * PrivacyCare (spec 2026-09-16 D-W2-7g)
 *
 * DESIGN.md, accessibility section: "Every status conveyed by text, never
 * by colour alone. A band tag reads 'High'; the colour reinforces, never
 * carries." — this is the one place the four band words are rendered, used
 * both in the summary strip and in the table's Band column so the two can
 * never drift to different wording for the same value.
 */
export const RiskBandTag = ({ band }: { band: RiskBand }) => (
  <Tag color={RISK_BAND_TAG_COLORS[band]}>{RISK_BAND_LABELS[band]}</Tag>
);
