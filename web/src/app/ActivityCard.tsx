import type { DriverStreakRow, WeeklyActivityRow } from "@/lib/rating";

/** The Monday of `d`'s week, as an ISO date string -- matches how
 *  `driver_weekly_activity.week_start` is computed server-side
 *  (telemetry/rating/streaks.py's `week_start`). */
function isoWeekStart(d: Date): string {
  const monday = new Date(d);
  const day = (monday.getDay() + 6) % 7; // Mon=0..Sun=6
  monday.setDate(monday.getDate() - day);
  return monday.toISOString().slice(0, 10);
}

function daysSince(iso: string | null): number | null {
  if (!iso) return null;
  const ms = Date.now() - new Date(iso).getTime();
  return Math.max(0, Math.floor(ms / (24 * 60 * 60 * 1000)));
}

/**
 * The activity/streak layer -- purely motivational, never a rating input
 * beyond the reduced-weight sigma-shrink `telemetry/rating/streaks.py`
 * already applies server-side. Kept as its own card, deliberately not
 * mixed into `RatingCard`, so a normal week's session still reads as
 * progress even on a week the competitive number barely moves.
 */
export default function ActivityCard({
  streak,
  weeklyActivity,
  lastVerifiedSessionAt,
}: {
  streak: DriverStreakRow | null;
  weeklyActivity: WeeklyActivityRow[];
  lastVerifiedSessionAt: string | null;
}) {
  const thisWeek = isoWeekStart(new Date());
  const thisWeekRow = weeklyActivity.find((w) => w.weekStart === thisWeek);
  const last30Days = weeklyActivity
    .filter((w) => Date.now() - new Date(w.weekStart).getTime() < 30 * 24 * 60 * 60 * 1000)
    .reduce((sum, w) => sum + w.verifiedSessionCount, 0);
  const days = daysSince(lastVerifiedSessionAt);

  return (
    <div className="rounded border border-hairline bg-surface p-4">
      <div className="label mb-1">Activity</div>
      <div className="flex items-baseline gap-2">
        <span className="font-mono text-3xl font-bold">{streak?.currentStreak ?? 0}</span>
        <span className="text-xs text-muted">
          week{streak?.currentStreak === 1 ? "" : "s"} streak
          {(streak?.freezesAvailable ?? 0) > 0 && (
            <span title="A freeze covers one missed week without breaking your streak."> · {streak?.freezesAvailable} freeze{streak?.freezesAvailable === 1 ? "" : "s"} banked</span>
          )}
        </span>
      </div>

      <dl className="mt-3 grid grid-cols-2 gap-y-1 text-xs">
        <dt className="text-muted">This week</dt>
        <dd className="text-right font-mono">
          {thisWeekRow?.verifiedSessionCount ?? 0}
          {thisWeekRow?.hasMidweekBonus && (
            <span className="ml-1 rounded bg-gain/20 px-1 py-0.5 text-[9px] font-semibold uppercase tracking-wider text-gain">
              trained mid-week
            </span>
          )}
        </dd>

        <dt className="text-muted">Last 30 days</dt>
        <dd className="text-right font-mono">{last30Days}</dd>

        <dt className="text-muted">Longest streak</dt>
        <dd className="text-right font-mono">{streak?.longestStreak ?? 0}</dd>

        <dt className="text-muted">Days since last verified session</dt>
        <dd className="text-right font-mono">{days ?? "—"}</dd>
      </dl>
    </div>
  );
}
