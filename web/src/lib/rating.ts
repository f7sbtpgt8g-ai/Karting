/**
 * Display-side Driver Rating math: everything that's a function of "now"
 * rather than of a specific contributing session.
 *
 * `driver_ratings` (supabase/migrations/0015_driver_rating.sql) stores only
 * `mu` and `sigmaAtLastUpdate` -- the confidence value as of the last
 * verified session, not "right now". Sigma keeps growing purely from
 * elapsed time since then (Part 3's Glicko-2-style decay), so the
 * *effective* sigma, the displayed conservative rating (`mu - k*sigma`),
 * and whether a driver counts as provisional are all derived here, at
 * render time, rather than written back by a batch job whose only purpose
 * would be advancing a clock.
 *
 * This is a genuinely separate config from `telemetry/rating/config.py`'s
 * `RatingConfig` -- that one governs the batch job's mu/sigma *updates*
 * (Elo K-factor, sigma shrink rate, validity thresholds, streak rules);
 * this one governs decay and display only, which the batch job never
 * computes at all. There is no shared constant to keep in sync between the
 * two beyond what each file's docstring cross-references -- if you change
 * the grace window or decay rate, only this file needs it.
 */

export type RatingDisplayConfig = {
  /** How conservative the displayed rating is: `mu - k * sigmaEffective`. */
  k: number;
  /** Weeks since the last verified session before sigma starts growing at
   *  all -- karting is seasonal, so an off-season gap shouldn't read as
   *  "getting rusty" the same way a mid-season gap would. */
  decayGraceWeeks: number;
  /** Growth-rate constant `c` in `sqrt(sigma^2 + c^2 * weeksPastGrace)` --
   *  Glicko-2's own shape for uncertainty growing with elapsed time. */
  decayRatePerSqrtWeek: number;
  /** Sigma never grows past this, however long the gap -- an indefinitely
   *  inactive driver reads as "fully provisional again", not an ever-more
   *  extreme number. Matches the default starting sigma, since that's
   *  already "no evidence yet". */
  sigmaMax: number;
  /** A driver is provisional (not yet a settled number) while their
   *  effective sigma is above this. */
  provisionalSigmaThreshold: number;
};

/** Reasoned starting points, not measured constants -- see the module
 *  docstring. Expected to move once there's real multi-driver, multi-season
 *  data to tune against. */
export const DEFAULT_RATING_DISPLAY_CONFIG: RatingDisplayConfig = {
  k: 3,
  decayGraceWeeks: 6,
  decayRatePerSqrtWeek: 15,
  sigmaMax: 350,
  provisionalSigmaThreshold: 200,
};

const MS_PER_WEEK = 7 * 24 * 60 * 60 * 1000;

/** Whole weeks between `from` and `to` (>= 0) -- `to` defaults to now, an
 *  explicit param only so tests aren't racing the clock. */
export function weeksSince(from: string | Date, to: Date = new Date()): number {
  const fromMs = typeof from === "string" ? new Date(from).getTime() : from.getTime();
  const elapsedMs = Math.max(0, to.getTime() - fromMs);
  return elapsedMs / MS_PER_WEEK;
}

/**
 * Sigma right now, grown from `sigmaAtLastUpdate` purely as a function of
 * elapsed time since `lastVerifiedSessionAt` -- track conditions never
 * enter this calculation (Part 3's explicit separation from Part 2's
 * fairness handling). Returns `sigmaAtLastUpdate` unchanged for a driver
 * with no verified session yet (nothing to decay from).
 */
export function effectiveSigma(
  sigmaAtLastUpdate: number,
  lastVerifiedSessionAt: string | Date | null,
  now: Date = new Date(),
  config: RatingDisplayConfig = DEFAULT_RATING_DISPLAY_CONFIG,
): number {
  if (!lastVerifiedSessionAt) return sigmaAtLastUpdate;
  const weeksPastGrace = Math.max(0, weeksSince(lastVerifiedSessionAt, now) - config.decayGraceWeeks);
  if (weeksPastGrace <= 0) return sigmaAtLastUpdate;
  const grown = Math.sqrt(sigmaAtLastUpdate ** 2 + config.decayRatePerSqrtWeek ** 2 * weeksPastGrace);
  return Math.min(config.sigmaMax, grown);
}

/** The conservative number shown everywhere -- leaderboards included. */
export function displayedRating(
  mu: number,
  sigmaEffective: number,
  config: RatingDisplayConfig = DEFAULT_RATING_DISPLAY_CONFIG,
): number {
  return mu - config.k * sigmaEffective;
}

/** Whether to show the "provisional" badge rather than the settled number
 *  as if it were final. */
export function isProvisional(
  sigmaEffective: number,
  config: RatingDisplayConfig = DEFAULT_RATING_DISPLAY_CONFIG,
): boolean {
  return sigmaEffective > config.provisionalSigmaThreshold;
}

export type DriverRatingRow = {
  driverProfileId: number;
  mu: number;
  sigmaAtLastUpdate: number;
  lastVerifiedSessionAt: string | null;
  sessionsRatedCount: number;
};

/** Everything a rating card/leaderboard row needs, derived in one place so
 *  a caller never has to remember the effectiveSigma -> displayedRating ->
 *  isProvisional order. */
export type DisplayedRating = {
  displayedRating: number;
  sigmaEffective: number;
  provisional: boolean;
};

export function deriveDisplayedRating(
  row: Pick<DriverRatingRow, "mu" | "sigmaAtLastUpdate" | "lastVerifiedSessionAt">,
  now: Date = new Date(),
  config: RatingDisplayConfig = DEFAULT_RATING_DISPLAY_CONFIG,
): DisplayedRating {
  const sigmaEffective = effectiveSigma(row.sigmaAtLastUpdate, row.lastVerifiedSessionAt, now, config);
  return {
    displayedRating: displayedRating(row.mu, sigmaEffective, config),
    sigmaEffective,
    provisional: isProvisional(sigmaEffective, config),
  };
}

export type RatingHistoryRow = {
  id: number;
  sessionDbId: number;
  computedAt: string;
  mechanism: "field" | "reference";
  cohortTrack: string | null;
  cohortDate: string | null;
  cohortConditions: string | null;
  cohortClass: string | null;
  cohortSize: number;
  muBefore: number;
  muAfter: number;
  sigmaBefore: number;
  sigmaAfter: number;
  note: string | null;
};

export type DriverStreakRow = {
  currentStreak: number;
  longestStreak: number;
  freezesAvailable: number;
  lastQualifyingWeek: string | null;
};

export type WeeklyActivityRow = {
  weekStart: string;
  verifiedSessionCount: number;
  hasMidweekBonus: boolean;
};
