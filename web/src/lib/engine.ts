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
  "X30 Mini",
  "X30 Junior",
  "X30 Senior",
  "OK Junior",
  "OK-N",
] as const;

export type EngineCategory = (typeof ENGINE_CATEGORIES)[number];

/** The Rotax peak-power band, matching telemetry/analysis_store.py. */
export const POWERZONE_RPM: [number, number] = [9000, 12000];

/**
 * Whether "powerzone %" means anything for this driver.
 *
 * The figure is stored for every lap regardless -- it is just time in an RPM
 * window -- but 9,000-12,000 rpm is where a Rotax makes power, and reading
 * an X30 or an OK against a Rotax's band would be worse than showing
 * nothing.
 */
export function hasPowerzone(category: string | null | undefined): boolean {
  return Boolean(category && category.startsWith("Rotax"));
}
