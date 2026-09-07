"""Part 4: the activity layer. Weekly qualifying streaks with a limited
freeze/grace mechanism, and the purely-cosmetic midweek-training badge.

Architecturally separate from mu/sigma on purpose -- nothing in this module
ever looks at or produces a rating number. The one place the two layers
touch at all is `elo.py`'s `_shrink_sigma` taking a per-session weight, which
`engine.py` derives from *this* module's session-count-per-week (a second
same-week session shrinks sigma at a reduced weight rather than full) --
see config.midweek_sigma_weight. That is the full extent of the coupling.

Streak state is derived purely from the history of qualifying weeks up to
whatever the batch job has seen so far -- there is no background process
that silently zeroes a streak out from elapsed real time alone (the same
"decay is evaluated from evidence, not ticked by a clock" choice
`driver_ratings` makes for sigma). A streak that stops being fed simply
stops changing; the UI pairs it with "days since last verified session" so
a stale streak still reads honestly rather than as a false "still going".
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from .config import DEFAULT_RATING_CONFIG, RatingConfig


def week_start(d: date) -> date:
    """The Monday of `d`'s week."""
    return d - timedelta(days=d.weekday())


@dataclass
class WeekCount:
    week_start: date
    verified_session_count: int


def qualifying_weeks(weeks: list[WeekCount], config: RatingConfig = DEFAULT_RATING_CONFIG) -> list[date]:
    """Which weeks (by Monday) had enough verified sessions to count,
    sorted chronologically."""
    return sorted(
        w.week_start for w in weeks if w.verified_session_count >= config.streak_min_sessions_per_week
    )


def midweek_bonus_weeks(weeks: list[WeekCount], config: RatingConfig = DEFAULT_RATING_CONFIG) -> set[date]:
    """Weeks that earn the cosmetic "trained mid-week" badge -- never fed
    back into mu/sigma, purely a display flag."""
    return {w.week_start for w in weeks if w.verified_session_count >= config.midweek_bonus_min_sessions}


@dataclass
class StreakState:
    current_streak: int
    longest_streak: int
    freezes_available: int
    last_qualifying_week: date | None


def compute_streak(sorted_qualifying_weeks: list[date], config: RatingConfig = DEFAULT_RATING_CONFIG) -> StreakState:
    """Fold a chronological list of qualifying week-starts into the current
    streak, longest streak, and banked freezes.

    Rule (configurable via `RatingConfig`, not hard-coded here): a single
    missed week consumes one banked freeze and the streak survives; two or
    more consecutive missed weeks break it regardless of freezes banked.
    A freeze is earned every `streak_freeze_earn_interval_weeks` consecutive
    qualifying weeks, capped at `streak_max_banked_freezes`.
    """
    if not sorted_qualifying_weeks:
        return StreakState(0, 0, config.streak_initial_freezes, None)

    streak = 0
    longest = 0
    freezes = config.streak_initial_freezes
    prev: date | None = None

    for week in sorted_qualifying_weeks:
        if prev is None:
            streak = 1
        else:
            gap_weeks = (week - prev).days // 7
            missed = gap_weeks - 1
            if missed <= 0:
                # Same week counted twice, or truly consecutive -- either
                # way, no break. (missed < 0 can't happen for a sorted,
                # deduplicated input, but a duplicate week_start is handled
                # the same as "consecutive" rather than erroring.)
                streak += 1
            elif missed == 1 and freezes > 0:
                freezes -= 1
                streak += 1
            else:
                streak = 1

        if streak > 0 and streak % config.streak_freeze_earn_interval_weeks == 0:
            freezes = min(config.streak_max_banked_freezes, freezes + 1)

        longest = max(longest, streak)
        prev = week

    return StreakState(current_streak=streak, longest_streak=longest, freezes_available=freezes, last_qualifying_week=prev)


def midweek_session_weight(
    session_index_in_week: int, config: RatingConfig = DEFAULT_RATING_CONFIG
) -> float:
    """The sigma-shrink weight for the Nth verified session contributing in
    one driver-week (0-indexed): full weight for the first, a reduced but
    non-zero weight for every one after -- diminishing returns on volume
    within a week, never a route to inflating confidence (and so the
    displayed rating) through sheer session count."""
    return 1.0 if session_index_in_week == 0 else config.midweek_sigma_weight
