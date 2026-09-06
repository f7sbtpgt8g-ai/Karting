/**
 * Turning the track's corner/straight segment map into N timing sectors.
 *
 * The analysis segments a lap into every corner and every straight -- 15-16
 * of them on a typical kart track. That is the right granularity for coaching
 * and the wrong one for a lap-time table, which wants 3-8 comparable splits.
 *
 * The rule that matters: **a sector boundary is always a segment boundary.**
 * Cutting a lap into N equal distances would routinely put a split halfway
 * round a corner, so a driver who takes a different line through it appears
 * to lose time in one sector and gain it in the next, for no reason. Grouping
 * whole segments makes that impossible by construction.
 *
 * Where there is a choice, boundaries prefer the *start of a straight*: a
 * split on the straight leaves each corner whole inside one sector, which is
 * how sector timing is drawn on a real circuit.
 */

export type Segment = {
  label: string;
  kind: string;
  start_m: number;
  end_m: number;
};

export type Sector = {
  index: number;
  label: string;
  /** Indices into the segment list, in order. */
  segmentIndices: number[];
  startM: number;
  endM: number;
};

export const MIN_SECTORS = 3;
export const MAX_SECTORS = 8;
export const DEFAULT_SECTORS = 4;

/**
 * Split `segments` into `count` contiguous sectors of as near as possible
 * equal distance.
 *
 * Returns fewer sectors than asked for only when there are fewer segments
 * than sectors -- every sector must contain at least one whole segment, so a
 * track mapped into 5 segments cannot yield 8 sectors.
 */
export function buildSectors(segments: Segment[], count: number): Sector[] {
  if (segments.length === 0) return [];
  const wanted = Math.max(1, Math.min(count, segments.length));
  if (wanted === 1) {
    return [
      {
        index: 0,
        label: "S1",
        segmentIndices: segments.map((_, i) => i),
        startM: segments[0].start_m,
        endM: segments[segments.length - 1].end_m,
      },
    ];
  }

  const start = segments[0].start_m;
  const total = segments[segments.length - 1].end_m - start;

  // Candidate cut points are the gaps *between* segments: cut k sits before
  // segment k, so 1..segments.length-1. Choosing `wanted - 1` of them gives
  // `wanted` sectors.
  const cuts: number[] = [];
  let taken = 0;

  for (let sector = 1; sector < wanted; sector += 1) {
    // Where an equal split would land, measured from the lap start rather
    // than from the previous cut, so rounding cannot accumulate.
    const target = start + (total * sector) / wanted;

    // Every cut still available, leaving room for the sectors after this one
    // to get at least one segment each.
    const earliest = taken + 1;
    const latest = segments.length - (wanted - sector);

    let best = -1;
    let bestScore = Number.POSITIVE_INFINITY;
    for (let cut = earliest; cut <= latest; cut += 1) {
      const distance = Math.abs(segments[cut].start_m - target);
      // A cut before a straight leaves the preceding corner whole in the
      // sector that ends here. Worth a modest bias, not an override: a
      // straight far from the target still makes a worse sector than a
      // corner boundary right on it.
      const penalty = segments[cut].kind === "straight" ? 0 : total * 0.04;
      const score = distance + penalty;
      if (score < bestScore) {
        bestScore = score;
        best = cut;
      }
    }
    cuts.push(best);
    taken = best;
  }

  const bounds = [0, ...cuts, segments.length];
  return bounds.slice(0, -1).map((from, index) => {
    const to = bounds[index + 1];
    const indices = [];
    for (let i = from; i < to; i += 1) indices.push(i);
    return {
      index,
      label: `Sector ${index + 1}`,
      segmentIndices: indices,
      startM: segments[from].start_m,
      endM: segments[to - 1].end_m,
    };
  });
}

/**
 * One lap's time in each sector, by summing its segment times.
 *
 * `segmentTimes` is keyed by segment label rather than index because that is
 * how `lap_segment_times` records them, and a lap missing a segment (a
 * dropped GPS fix across a boundary) must yield null for the whole sector
 * rather than a plausible-looking short time.
 */
export function sectorTimes(
  sectors: Sector[],
  segments: Segment[],
  segmentTimes: Map<string, number | null>,
): (number | null)[] {
  return sectors.map((sector) => {
    let total = 0;
    for (const index of sector.segmentIndices) {
      const value = segmentTimes.get(segments[index].label);
      if (value === undefined || value === null || Number.isNaN(value)) return null;
      total += value;
    }
    return total;
  });
}

/**
 * The theoretical best: the sum of the quickest time anyone managed in each
 * sector, across the laps still included.
 *
 * Returns null if any sector has no time at all -- a "theoretical best"
 * missing a sector would read as an impossibly quick lap rather than as
 * missing data.
 */
export function theoreticalBest(
  lapSectorTimes: (number | null)[][],
): { total: number | null; perSector: (number | null)[]; ownerLap: (number | null)[] } {
  const sectorCount = lapSectorTimes[0]?.length ?? 0;
  const perSector: (number | null)[] = [];
  const ownerLap: (number | null)[] = [];

  for (let s = 0; s < sectorCount; s += 1) {
    let best: number | null = null;
    let owner: number | null = null;
    lapSectorTimes.forEach((times, lapIndex) => {
      const value = times[s];
      if (value === null) return;
      if (best === null || value < best) {
        best = value;
        owner = lapIndex;
      }
    });
    perSector.push(best);
    ownerLap.push(owner);
  }

  const total = perSector.every((v) => v !== null)
    ? perSector.reduce((sum: number, v) => sum + (v as number), 0)
    : null;
  return { total, perSector, ownerLap };
}
