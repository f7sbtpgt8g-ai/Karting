/**
 * The display formats the Streamlit app uses, so the two look like one
 * product while they run side by side.
 */

/** `SS.mmm` -- design 1a's lap-time format (`_da1a_time_str` in app.py). */
export function lapTime(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || Number.isNaN(seconds)) return "--.---";
  return seconds.toFixed(3);
}

/**
 * `sessions.start_date` is TEXT, written by the parser as `DD-MM-YYYY`.
 *
 * That matters for more than display: sorting the column as text puts
 * 02-01-2026 before 15-12-2025, which is why this returns a real Date for
 * the sort to use rather than letting the raw string through. (The Streamlit
 * page sorts the raw column and has the same off-by-a-month-boundary
 * behaviour; worth not carrying across.)
 */
export function parseSessionDate(raw: string | null | undefined): Date | null {
  if (!raw) return null;
  const dmy = raw.match(/^(\d{2})-(\d{2})-(\d{4})$/);
  if (dmy) {
    const [, d, m, y] = dmy;
    return new Date(Number(y), Number(m) - 1, Number(d));
  }
  const parsed = new Date(raw);
  return Number.isNaN(parsed.getTime()) ? null : parsed;
}

/** `Feb 03, 2026`, matching `_home_format_date`. */
export function sessionDate(raw: string | null | undefined): string {
  const parsed = parseSessionDate(raw);
  if (!parsed) return raw || "?";
  return parsed.toLocaleDateString("en-US", { month: "short", day: "2-digit", year: "numeric" });
}

/** `14:32` from the stored `HH:MM:SS`, matching `_home_format_time`. */
export function sessionTime(raw: string | null | undefined): string {
  if (!raw) return "?";
  const match = raw.match(/^(\d{1,2}):(\d{2})/);
  return match ? `${match[1].padStart(2, "0")}:${match[2]}` : raw;
}

/** ISO `YYYY-MM-DD` for `<input type="date">`, from the stored format. */
export function isoDate(raw: string | null | undefined): string {
  const parsed = parseSessionDate(raw);
  if (!parsed) return "";
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${parsed.getFullYear()}-${pad(parsed.getMonth() + 1)}-${pad(parsed.getDate())}`;
}
