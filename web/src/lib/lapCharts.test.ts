import { describe, expect, it } from "vitest";

import { deltaTrace, interpolateAt, type LapTrace } from "./lapCharts";

/** A lap covering `length` metres at a constant `secondsPerMetre`. */
function lap(lapNumber: number, secondsPerMetre: number, length = 600, step = 7): LapTrace {
  const distanceM: number[] = [];
  const lapTimeS: number[] = [];
  for (let d = 0; d < length; d += step) {
    distanceM.push(d);
    lapTimeS.push(d * secondsPerMetre);
  }
  // A real lap ends on the line, not at whatever multiple of the sampling
  // interval happens to fall short of it.
  distanceM.push(length);
  lapTimeS.push(length * secondsPerMetre);
  return {
    lapNumber,
    distanceM,
    lapTimeS,
    speedKmh: distanceM.map(() => 60),
    rpm: distanceM.map(() => 11000),
    lateralG: distanceM.map(() => 0),
    longitudinalG: distanceM.map(() => 0),
  };
}

describe("interpolateAt", () => {
  it("interpolates between samples", () => {
    expect(interpolateAt([0, 10], [0, 100], 5)).toBe(50);
    expect(interpolateAt([0, 10, 20], [0, 100, 100], 15)).toBe(100);
  });

  it("returns the endpoints exactly", () => {
    expect(interpolateAt([0, 10], [3, 7], 0)).toBe(3);
    expect(interpolateAt([0, 10], [3, 7], 10)).toBe(7);
  });

  it("refuses to extrapolate past the sampled range", () => {
    // A lap whose GPS dropped near the line should leave a gap, not a
    // confident straight line into it.
    expect(interpolateAt([10, 20], [0, 1], 5)).toBeNull();
    expect(interpolateAt([10, 20], [0, 1], 25)).toBeNull();
  });

  it("gives up rather than guessing across a missing sample", () => {
    expect(interpolateAt([0, 10, 20], [0, null, 20], 5)).toBeNull();
  });
});

describe("deltaTrace", () => {
  it("is zero against itself", () => {
    const reference = lap(1, 1 / 20);
    const { deltaS } = deltaTrace(reference, reference);
    for (const value of deltaS) expect(value).toBeCloseTo(0, 9);
  });

  it("is positive for a slower lap and negative for a quicker one", () => {
    const reference = lap(1, 1 / 20); // 20 m/s
    const slower = lap(2, 1 / 18); // 18 m/s
    const quicker = lap(3, 1 / 22);

    const slowerEnd = deltaTrace(slower, reference).deltaS.at(-1)!;
    const quickerEnd = deltaTrace(quicker, reference).deltaS.at(-1)!;
    expect(slowerEnd).toBeGreaterThan(0);
    expect(quickerEnd).toBeLessThan(0);
  });

  it("gets the magnitude right at the end of the lap", () => {
    // 600 m at 20 m/s is 30.0 s; at 18 m/s it is 33.33 s.
    const { deltaS } = deltaTrace(lap(2, 1 / 18), lap(1, 1 / 20));
    expect(deltaS.at(-1)!).toBeCloseTo(600 / 18 - 600 / 20, 2);
  });

  it("does not depend on the two laps sharing a sampling grid", () => {
    // The bug this guards: subtracting index-by-index compares a point 40 m
    // into one lap with a point 43 m into the other. The error grows and
    // shrinks with the sampling, and reads as real time gained and lost.
    const reference = lap(1, 1 / 20, 600, 7);
    const sameSpeedDifferentSampling = lap(2, 1 / 20, 600, 11);
    const { deltaS } = deltaTrace(sameSpeedDifferentSampling, reference);
    for (const value of deltaS) expect(Math.abs(value!)).toBeLessThan(1e-6);
  });

  it("only covers the distance both laps actually recorded", () => {
    const short = lap(2, 1 / 20, 400);
    const full = lap(1, 1 / 20, 600);
    const { distanceM } = deltaTrace(short, full);
    expect(distanceM.at(-1)!).toBeLessThanOrEqual(400);
  });

  it("returns nothing when the laps do not overlap at all", () => {
    const a: LapTrace = { ...lap(1, 1 / 20, 100), distanceM: [0, 50, 100] };
    const b: LapTrace = { ...lap(2, 1 / 20, 100), distanceM: [500, 550, 600] };
    expect(deltaTrace(a, b).distanceM).toEqual([]);
  });
});
