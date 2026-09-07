import { describe, expect, it } from "vitest";

import { CONDITION_COLOR } from "./conditions";
import { ENGINE_COLORS } from "./engine";
import { PODIUM_PLACE_COLOR, podiumPlaceColor } from "./tracks";

describe("podiumPlaceColor", () => {
  it("returns a colour for ranks 1 through 3", () => {
    expect(podiumPlaceColor(1)).toBe(PODIUM_PLACE_COLOR[1]);
    expect(podiumPlaceColor(2)).toBe(PODIUM_PLACE_COLOR[2]);
    expect(podiumPlaceColor(3)).toBe(PODIUM_PLACE_COLOR[3]);
  });

  it("returns undefined for anything outside the top three", () => {
    expect(podiumPlaceColor(0)).toBeUndefined();
    expect(podiumPlaceColor(4)).toBeUndefined();
  });

  it("never collides with an engine or condition colour", () => {
    // A gold podium row next to a Rotax chip that happened to be the same
    // gold would read as one thing, not two -- the same collision
    // conditions.test.ts already guards for engine vs. condition colours.
    const taken = new Set(
      [...ENGINE_COLORS, ...Object.values(CONDITION_COLOR)].map((c) => c.toLowerCase()),
    );
    for (const color of Object.values(PODIUM_PLACE_COLOR)) {
      expect(taken.has(color.toLowerCase())).toBe(false);
    }
  });

  it("gives each of the three places its own colour", () => {
    const colors = Object.values(PODIUM_PLACE_COLOR);
    expect(new Set(colors).size).toBe(colors.length);
  });
});
