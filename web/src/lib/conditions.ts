/**
 * Track conditions, and the colours they are read in.
 *
 * These live in their own module rather than inside the Home component
 * because engine classes are shown in the same table cell, and the two sets
 * of colours must not overlap -- a blue "Rotax Senior" sitting where a blue
 * "Wet" is expected is not a small problem. `conditions.test.ts` asserts
 * they stay disjoint, which is the only way that holds once two files are
 * edited months apart.
 */

// telemetry/weather.py's CONDITION_OPTIONS -- kept identical so a session
// typed in one app reads back correctly in the other.
export const CONDITIONS = ["Dry", "Wet", "Mixed"] as const;

/**
 * Water reads blue, and a mixed track reads as the warning it is. Dry stays
 * plain, because "nothing unusual" should not compete for attention with the
 * two conditions that change how the lap times should be read.
 *
 * The two hues are the first two slots of the validated categorical palette
 * in trackMap.ts, so they are legible on this background and distinguishable
 * to a colour-blind reader.
 */
export const CONDITION_COLOR: Record<string, string> = {
  Wet: "#3987e5",
  Mixed: "#d95926",
  Dry: "#eef0f1",
};
