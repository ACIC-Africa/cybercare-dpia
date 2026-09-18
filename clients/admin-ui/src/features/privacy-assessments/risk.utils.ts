/**
 * PrivacyCare (spec 2026-09-16 D-W2-7g)
 *
 * A client-side MIRROR of risk/banding.py's pure arithmetic, used ONLY to
 * preview a consequence the server has not been asked about yet:
 *
 *   - the live score/band shown in AddRiskModal as likelihood/severity are
 *     chosen, before the risk is submitted;
 *   - the "what would the band become" preview in RemoveRiskModal, before
 *     the removal is confirmed.
 *
 * Nothing this file computes is ever displayed as an assessment's ACTUAL
 * risk band or a risk's ACTUAL score — those always come straight off a
 * RiskResponse the server returned (see RiskRegisterSection.tsx's own
 * comment on why it reads risks[0].band rather than recomputing). This
 * file exists because there is no "preview" API call, and a screen that
 * makes users guess what their own selection will do is exactly what
 * DESIGN.md's "show the consequence while they choose" line forbids.
 *
 * Kept intentionally identical to risk/banding.py's BANDS ranges and
 * overall_band()'s "max, never average" rule — see banding.py itself, and
 * risk.utils.test.ts's own parametrised test, which pins every one of the
 * 25 possible scores against the same table so the two cannot drift
 * silently.
 */
import { RiskBand } from "./risk.types";

const BAND_RANGES: ReadonlyArray<[number, number, RiskBand]> = [
  [1, 4, RiskBand.LOW],
  [5, 9, RiskBand.MEDIUM],
  [10, 16, RiskBand.HIGH],
  [17, 25, RiskBand.CRITICAL],
];

const BAND_ORDER: readonly RiskBand[] = [
  RiskBand.LOW,
  RiskBand.MEDIUM,
  RiskBand.HIGH,
  RiskBand.CRITICAL,
];

/** likelihood × severity, both expected 1..5 (the Select controls this
 * feeds never offer anything else) — mirrors banding.score(). */
export const scoreFor = (likelihood: number, severity: number): number =>
  likelihood * severity;

/** Mirrors banding.band(). Falls back to LOW rather than throwing for a
 * score outside 1..25 — this is UI preview code, not the source of truth,
 * and a defensive fallback is safer here than a crash the server-side
 * equivalent is correct to avoid. */
export const bandForScore = (score: number): RiskBand => {
  const found = BAND_RANGES.find(([min, max]) => score >= min && score <= max);
  return found ? found[2] : RiskBand.LOW;
};

/** Mirrors banding.overall_band(): the band of the HIGHEST score, never an
 * average — an empty list defaults to LOW, same as the empty register. */
export const overallBandForScores = (scores: number[]): RiskBand => {
  if (scores.length === 0) {
    return RiskBand.LOW;
  }
  return bandForScore(Math.max(...scores));
};

/** LOW < MEDIUM < HIGH < CRITICAL, for comparing two bands (e.g. "would
 * this raise the assessment's band?"). */
export const bandRank = (band: RiskBand): number => BAND_ORDER.indexOf(band);
