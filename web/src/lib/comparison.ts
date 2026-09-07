/**
 * Identifying a lap once more than one session is on the page.
 *
 * With a single session a lap is just its number. Add a teammate's session
 * and "lap 10" stops being unique -- and the failure is silent: two drivers'
 * tenth laps merge into one selection, one of them stops being plottable,
 * and the chart still draws a perfectly believable line. So the key is the
 * pair, everywhere, and the conversion lives here rather than as string
 * interpolation scattered through the component.
 */

/** `sessionId:lapNumber`. */
export type LapKey = string;

export function lapKey(sessionId: number, lapNumber: number): LapKey {
  return `${sessionId}:${lapNumber}`;
}

/**
 * Digits or nothing.
 *
 * `Number("")` is 0, not NaN, so a half-formed key like ":" would otherwise
 * parse as a real reference to session 0 rather than being rejected.
 */
function toInt(raw: string | undefined): number {
  return raw !== undefined && /^\d+$/.test(raw) ? Number(raw) : NaN;
}

export function parseLapKey(key: LapKey): { sessionId: number; lapNumber: number } {
  const [sessionId, lapNumber] = key.split(":");
  return { sessionId: toInt(sessionId), lapNumber: toInt(lapNumber) };
}

/**
 * The selected laps, grouped into one list per session.
 *
 * Traces are fetched per session, so the selection has to be taken apart
 * before it can be loaded -- and a lap number must never be carried to a
 * session it did not come from.
 */
export function lapsBySession(keys: Iterable<LapKey>): Map<number, number[]> {
  const grouped = new Map<number, number[]>();
  for (const key of keys) {
    const { sessionId, lapNumber } = parseLapKey(key);
    if (!Number.isFinite(sessionId) || !Number.isFinite(lapNumber)) continue;
    const list = grouped.get(sessionId) ?? [];
    if (!list.includes(lapNumber)) list.push(lapNumber);
    grouped.set(sessionId, list);
  }
  for (const list of grouped.values()) list.sort((a, b) => a - b);
  return grouped;
}

/**
 * How a lap is named once it is one of several drivers'.
 *
 * Within one session the driver's name is noise on every row, so it is left
 * off; across sessions it is the only thing distinguishing two identical lap
 * numbers, so it leads. A date follows the name when one driver has two
 * sessions open, because "Oliver" twice in a legend is no better than "Lap
 * 10" twice.
 */
export function lapLabel(
  lapNumber: number,
  driverName: string,
  options: { multiSession: boolean; qualifier?: string | null } = { multiSession: false },
): string {
  if (!options.multiSession) return `Lap ${lapNumber}`;
  const who = options.qualifier ? `${driverName} ${options.qualifier}` : driverName;
  return `${who} · Lap ${lapNumber}`;
}

/**
 * A short name per open session, disambiguated only where it has to be.
 *
 * Two sessions from the same driver on different days are told apart by
 * date; two on the same day by start time. Adding the date to every tab
 * regardless would push the names out of a row of tabs for the common case
 * of one session each from three different drivers.
 */
export function tabLabels(
  sessions: { sessionId: number; driverName: string; startDate: string | null; startTime: string | null }[],
): Map<number, { name: string; qualifier: string | null }> {
  const byName = new Map<string, typeof sessions>();
  for (const session of sessions) {
    const list = byName.get(session.driverName) ?? [];
    list.push(session);
    byName.set(session.driverName, list);
  }

  const labels = new Map<number, { name: string; qualifier: string | null }>();
  for (const [name, group] of byName) {
    if (group.length === 1) {
      labels.set(group[0].sessionId, { name, qualifier: null });
      continue;
    }
    const datesDiffer = new Set(group.map((s) => s.startDate)).size > 1;
    for (const session of group) {
      labels.set(session.sessionId, {
        name,
        qualifier: datesDiffer
          ? (session.startDate ?? "?")
          : (session.startTime ?? "?").slice(0, 5),
      });
    }
  }
  return labels;
}
