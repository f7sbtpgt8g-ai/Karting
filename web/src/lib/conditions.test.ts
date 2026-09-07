import { describe, expect, it } from "vitest";

import { CONDITION_COLOR, CONDITIONS } from "./conditions";
import { ENGINE_CATEGORIES, ENGINE_COLORS, engineColor } from "./engine";

describe("condition and engine colours", () => {
  it("never uses the same colour for an engine class and a track condition", () => {
    // Both are shown in the same cell on Home. The first version of the
    // engine palette gave Rotax the Wet blue and X30 the Mixed orange, so a
    // Rotax session read as a wet one at a glance.
    const conditionColors = new Set(Object.values(CONDITION_COLOR).map((c) => c.toLowerCase()));
    for (const color of ENGINE_COLORS) {
      expect(conditionColors.has(color.toLowerCase())).toBe(false);
    }
  });

  it("gives every engine family its own colour", () => {
    expect(new Set(ENGINE_COLORS).size).toBe(ENGINE_COLORS.length);
  });

  it("colours every engine class the dropdown offers", () => {
    // A class added to the list without a family prefix would render in the
    // default ink and look like a rendering bug rather than a missing case.
    for (const category of ENGINE_CATEGORIES) {
      expect(engineColor(category), `no colour for ${category}`).not.toBeNull();
    }
  });

  it("gives every family a distinct colour from every other family", () => {
    const seen = new Map<string, string>();
    for (const category of ENGINE_CATEGORIES) {
      const color = engineColor(category)!;
      const family = seen.get(color);
      // Same colour is fine within a family (Rotax Micro and Rotax Senior),
      // and only within one.
      if (family) expect(category.startsWith(family.split(" ")[0])).toBe(true);
      else seen.set(color, category);
    }
  });

  it("has a colour for every condition offered", () => {
    for (const condition of CONDITIONS) {
      expect(CONDITION_COLOR[condition]).toBeDefined();
    }
  });
});
