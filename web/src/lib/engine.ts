/**
 * Engine classes, and what follows from the one a driver races.
 */

/**
 * The classes offered at registration and in settings.
 *
 * Grouped by manufacturer, junior classes first within each, which is how a
 * driver thinks about them. Not enforced by a database constraint: this list
 * will gain entries (IAME, KZ, new Rotax classes) and a CHECK would turn
 * each addition into a migration.
 */
export const ENGINE_CATEGORIES = [
  "Rotax Micro",
  "Rotax Mini",
  "Rotax Junior",
  "Rotax Senior",
  "Rotax DD2",
  // X30 is IAME's too; kept under its own name because that is what everyone
  // calls it, and because these values are already stored on live accounts.
  "X30 Mini",
  "X30 Junior",
  "X30 Senior",
  "IAME Micro Swift",
  "IAME Mini Swift",
  "IAME KA100",
  "OK Junior",
  "OK-N",
  // Shifter classes.
  "KZ2",
  "KZ2 Masters",
] as const;

export type EngineCategory = (typeof ENGINE_CATEGORIES)[number];

/**
 * The Rotax peak-power band.
 *
 * Mirrors `DEFAULT_PEAK_POWER_RPM_BAND` in telemetry/setup_engine.py, which
 * is the single definition on the Python side -- the engine page and the
 * gearing suggestions have to agree about where the engine makes power. This
 * copy exists only because the value is shown in the UI text; the percentage
 * itself is computed server-side against the Python constant, so a drift
 * here would misdescribe the number rather than change it.
 *
 * A reasonable default, not a confirmed spec: your engine builder's numbers
 * are better.
 */
export const POWERZONE_RPM: [number, number] = [9000, 12500];

/**
 * Whether "powerzone %" means anything for this driver.
 *
 * The figure is stored for every lap regardless -- it is just time in an RPM
 * window -- but the band is where a *Rotax* makes power, and reading an X30,
 * a KA100 or a KZ2 against it would be worse than showing nothing. Each of
 * those would need its own band before the number meant anything.
 */
export function hasPowerzone(category: string | null | undefined): boolean {
  return Boolean(category && category.startsWith("Rotax"));
}

/**
 * A colour per engine *family*, not per class.
 *
 * Sixteen classes would need sixteen hues, and no palette has sixteen a
 * reader can actually tell apart -- the label would be coloured without
 * being readable, which is worse than plain text. Five families is a number
 * that works, and it is also the distinction that matters when scanning a
 * list: "which of these are Rotax" is the question, "Rotax Junior vs Rotax
 * Senior" is already written next to it.
 *
 * The hues are the ones `SECTOR_COLORS` in trackMap.ts already uses, chosen
 * to stay distinguishable to a colour-blind reader on this background. Reused
 * rather than picked afresh so the app has one categorical palette.
 *
 * Slots 0 and 1 of that palette are deliberately skipped: they are the Wet
 * and Mixed track conditions, which are shown in the *same table cell* as
 * the engine class. A blue "Rotax Senior" next to where a blue "Wet" belongs
 * reads as a condition at a glance -- which is exactly what the first
 * version of this did. `conditions.test.ts` keeps the two sets apart.
 *
 * Slot 5 (#008300) is skipped too: it is a second green, and the one hue in
 * the palette a reader would confuse with slot 2 when both label engines in
 * the same column.
 */
const ENGINE_FAMILY_COLOR: Array<[string, string]> = [
  ["Rotax", "#199e70"],
  ["X30", "#c98500"],
  ["IAME", "#d55181"],
  ["OK", "#9085e9"],
  ["KZ", "#e66767"],
];

/** Every colour an engine class can be drawn in. */
export const ENGINE_COLORS = ENGINE_FAMILY_COLOR.map(([, color]) => color);

/** The colour for an engine class, or null when it is unset or unknown. */
export function engineColor(category: string | null | undefined): string | null {
  if (!category) return null;
  const match = ENGINE_FAMILY_COLOR.find(([family]) => category.startsWith(family));
  return match ? match[1] : null;
}
