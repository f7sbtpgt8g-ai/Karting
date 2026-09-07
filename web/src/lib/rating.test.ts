import { describe, expect, it } from "vitest";

import {
  DEFAULT_RATING_DISPLAY_CONFIG,
  deriveDisplayedRating,
  displayedRating,
  effectiveSigma,
  isProvisional,
  weeksSince,
} from "./rating";

const CONFIG = DEFAULT_RATING_DISPLAY_CONFIG;
const NOW = new Date("2026-06-01T00:00:00Z");

describe("weeksSince", () => {
  it("counts whole-ish weeks between two dates", () => {
    expect(weeksSince("2026-05-18T00:00:00Z", NOW)).toBeCloseTo(2, 5);
  });

  it("never goes negative for a future date", () => {
    expect(weeksSince("2026-07-01T00:00:00Z", NOW)).toBe(0);
  });
});

describe("effectiveSigma", () => {
  it("returns the stored sigma unchanged with no verified session yet", () => {
    expect(effectiveSigma(350, null, NOW, CONFIG)).toBe(350);
  });

  it("does not grow within the grace window", () => {
    const threeWeeksAgo = new Date(NOW.getTime() - 3 * 7 * 24 * 60 * 60 * 1000).toISOString();
    expect(effectiveSigma(200, threeWeeksAgo, NOW, CONFIG)).toBe(200);
  });

  it("grows past the grace window", () => {
    const twentyWeeksAgo = new Date(NOW.getTime() - 20 * 7 * 24 * 60 * 60 * 1000).toISOString();
    const grown = effectiveSigma(200, twentyWeeksAgo, NOW, CONFIG);
    expect(grown).toBeGreaterThan(200);
  });

  it("never exceeds sigmaMax however long the gap", () => {
    const yearsAgo = new Date(NOW.getTime() - 5000 * 7 * 24 * 60 * 60 * 1000).toISOString();
    expect(effectiveSigma(40, yearsAgo, NOW, CONFIG)).toBe(CONFIG.sigmaMax);
  });

  it("matches the sqrt-growth formula past grace", () => {
    const weeksPast = 10;
    const at = new Date(NOW.getTime() - (CONFIG.decayGraceWeeks + weeksPast) * 7 * 24 * 60 * 60 * 1000).toISOString();
    const expected = Math.sqrt(150 ** 2 + CONFIG.decayRatePerSqrtWeek ** 2 * weeksPast);
    expect(effectiveSigma(150, at, NOW, CONFIG)).toBeCloseTo(Math.min(CONFIG.sigmaMax, expected), 5);
  });
});

describe("displayedRating", () => {
  it("subtracts k times sigma from mu", () => {
    expect(displayedRating(1600, 100, CONFIG)).toBe(1600 - CONFIG.k * 100);
  });

  it("a higher sigma always shows a lower (more conservative) rating for the same mu", () => {
    expect(displayedRating(1600, 200, CONFIG)).toBeLessThan(displayedRating(1600, 100, CONFIG));
  });
});

describe("isProvisional", () => {
  it("is provisional above the threshold", () => {
    expect(isProvisional(CONFIG.provisionalSigmaThreshold + 1, CONFIG)).toBe(true);
  });

  it("is settled at or below the threshold", () => {
    expect(isProvisional(CONFIG.provisionalSigmaThreshold, CONFIG)).toBe(false);
  });
});

describe("deriveDisplayedRating", () => {
  it("composes effectiveSigma -> displayedRating -> isProvisional consistently", () => {
    const row = { mu: 1550, sigmaAtLastUpdate: 350, lastVerifiedSessionAt: null };
    const result = deriveDisplayedRating(row, NOW, CONFIG);
    expect(result.sigmaEffective).toBe(350);
    expect(result.displayedRating).toBe(1550 - CONFIG.k * 350);
    expect(result.provisional).toBe(true);
  });

  it("a settled driver with recent evidence and low sigma is not provisional", () => {
    const recent = new Date(NOW.getTime() - 1 * 7 * 24 * 60 * 60 * 1000).toISOString();
    const row = { mu: 1600, sigmaAtLastUpdate: 60, lastVerifiedSessionAt: recent };
    const result = deriveDisplayedRating(row, NOW, CONFIG);
    expect(result.provisional).toBe(false);
    expect(result.displayedRating).toBe(1600 - CONFIG.k * 60);
  });
});
