/**
 * Shaping stored lap traces into the series the comparison charts plot.
 *
 * Separate from the chart component because the delta calculation is real
 * arithmetic with a real failure mode: a delta trace computed against a
 * mismatched distance grid drifts smoothly and looks entirely believable.
 */

export type LapTrace = {
  lapNumber: number;
  distanceM: number[];
  lapTimeS: number[];
  speedKmh: (number | null)[];
  rpm: (number | null)[];
  lateralG: (number | null)[];
  longitudinalG: (number | null)[];
};

export const CHART_METRICS = [
  { key: "speedKmh", label: "Speed", unit: "km/h" },
  { key: "rpm", label: "RPM", unit: "rpm" },
  { key: "longitudinalG", label: "Longitudinal G", unit: "g" },
  { key: "lateralG", label: "Lateral G", unit: "g" },
] as const;

export type MetricKey = (typeof CHART_METRICS)[number]["key"];

/**
 * Linear interpolation of `ys` sampled at `xs`, evaluated at `at`.
 *
 * Outside the sampled range it returns null rather than extrapolating: a lap
 * whose GPS dropped near the line should leave a gap in the delta trace, not
 * a confident straight line into it.
 */
export function interpolateAt(
  xs: number[],
  ys: (number | null)[],
  at: number,
): number | null {
  if (xs.length === 0 || at < xs[0] || at > xs[xs.length - 1]) return null;

  let low = 0;
  let high = xs.length - 1;
  while (high - low > 1) {
    const mid = (low + high) >> 1;
    if (xs[mid] <= at) low = mid;
    else high = mid;
  }

  const x0 = xs[low];
  const x1 = xs[high];
  const y0 = ys[low];
  const y1 = ys[high];
  if (y0 === null || y1 === null) return null;
  if (x1 === x0) return y0;
  return y0 + ((y1 - y0) * (at - x0)) / (x1 - x0);
}

/**
 * Time gained or lost against a reference lap, over distance.
 *
 * Both laps are interpolated onto a common distance grid before subtracting,
 * because the two were sampled at whatever distances their own GPS fixes
 * happened to land on. Subtracting index-by-index instead would compare a
 * point 40 m into one lap with a point 43 m into the other -- an error that
 * grows and shrinks with the sampling and reads as real time gained.
 *
 * Positive means slower than the reference.
 */
export function deltaTrace(
  lap: LapTrace,
  reference: LapTrace,
  points = 400,
): { distanceM: number[]; deltaS: (number | null)[] } {
  const end = Math.min(
    lap.distanceM[lap.distanceM.length - 1] ?? 0,
    reference.distanceM[reference.distanceM.length - 1] ?? 0,
  );
  const start = Math.max(lap.distanceM[0] ?? 0, reference.distanceM[0] ?? 0);
  if (!(end > start)) return { distanceM: [], deltaS: [] };

  const distanceM: number[] = [];
  const deltaS: (number | null)[] = [];
  for (let i = 0; i < points; i += 1) {
    const at = start + ((end - start) * i) / (points - 1);
    const mine = interpolateAt(lap.distanceM, lap.lapTimeS, at);
    const theirs = interpolateAt(reference.distanceM, reference.lapTimeS, at);
    distanceM.push(at);
    deltaS.push(mine === null || theirs === null ? null : mine - theirs);
  }
  return { distanceM, deltaS };
}

/**
 * A stable colour per lap.
 *
 * Keyed on the lap's position in the selection rather than its lap number,
 * so the colours stay put as laps are added; and taken from the same
 * validated categorical ramp the track map uses, so nothing on the page
 * uses two different meanings for the same hue.
 */
export function lapColor(index: number, palette: readonly string[]): string {
  return palette[index % palette.length];
}
