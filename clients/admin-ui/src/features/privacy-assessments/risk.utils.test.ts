import { RiskBand } from "./risk.types";
import {
  bandForScore,
  bandRank,
  overallBandForScores,
  scoreFor,
} from "./risk.utils";

// PrivacyCare (spec 2026-09-16 D-W2-7g)
//
// Pins risk.utils.ts's client-side preview arithmetic against the exact
// table risk/banding.py's BANDS constant defines (1-4 low, 5-9 medium,
// 10-16 high, 17-25 critical) — every one of the 25 possible scores a
// 5x5 likelihood/severity matrix can produce, the same way
// screening.utils.test.ts pins its own ancestor-prefix walk against real
// taxonomy shapes rather than a handful of spot checks.

describe("scoreFor", () => {
  it("is likelihood × severity", () => {
    expect(scoreFor(1, 1)).toBe(1);
    expect(scoreFor(5, 5)).toBe(25);
    expect(scoreFor(3, 4)).toBe(12);
  });
});

describe("bandForScore", () => {
  const expected: Array<[number, RiskBand]> = [
    [1, RiskBand.LOW],
    [2, RiskBand.LOW],
    [3, RiskBand.LOW],
    [4, RiskBand.LOW],
    [5, RiskBand.MEDIUM],
    [6, RiskBand.MEDIUM],
    [7, RiskBand.MEDIUM],
    [8, RiskBand.MEDIUM],
    [9, RiskBand.MEDIUM],
    [10, RiskBand.HIGH],
    [11, RiskBand.HIGH],
    [12, RiskBand.HIGH],
    [13, RiskBand.HIGH],
    [14, RiskBand.HIGH],
    [15, RiskBand.HIGH],
    [16, RiskBand.HIGH],
    [17, RiskBand.CRITICAL],
    [18, RiskBand.CRITICAL],
    [19, RiskBand.CRITICAL],
    [20, RiskBand.CRITICAL],
    [21, RiskBand.CRITICAL],
    [22, RiskBand.CRITICAL],
    [23, RiskBand.CRITICAL],
    [24, RiskBand.CRITICAL],
    [25, RiskBand.CRITICAL],
  ];

  it.each(expected)("maps score %i to %s", (score, band) => {
    expect(bandForScore(score)).toBe(band);
  });
});

describe("overallBandForScores — the highest score, never the average", () => {
  it("defaults to LOW for an empty register", () => {
    expect(overallBandForScores([])).toBe(RiskBand.LOW);
  });

  it("is the band of the single highest score, not an average of several low ones plus one critical", () => {
    // A fuel retailer's DPIA: four minor/low risks (scores 2, 4, 6, 9) and
    // one catastrophic one (score 25, from a 5x5 matrix). Averaging these
    // five scores gives (2+4+6+9+25)/5 = 9.2, which bandForScore would
    // read as MEDIUM — exactly the failure banding.py's overall_band()
    // exists to prevent. The real rule takes the max: 25, CRITICAL.
    const scores = [2, 4, 6, 9, 25];
    expect(overallBandForScores(scores)).toBe(RiskBand.CRITICAL);
    expect(overallBandForScores(scores)).not.toBe(RiskBand.MEDIUM);
  });

  it("is LOW when every risk is low", () => {
    expect(overallBandForScores([1, 2, 4])).toBe(RiskBand.LOW);
  });
});

describe("bandRank", () => {
  it("orders LOW < MEDIUM < HIGH < CRITICAL", () => {
    expect(bandRank(RiskBand.LOW)).toBeLessThan(bandRank(RiskBand.MEDIUM));
    expect(bandRank(RiskBand.MEDIUM)).toBeLessThan(bandRank(RiskBand.HIGH));
    expect(bandRank(RiskBand.HIGH)).toBeLessThan(bandRank(RiskBand.CRITICAL));
  });
});
