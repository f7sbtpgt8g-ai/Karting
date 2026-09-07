"""Part 4: weekly streaks, freeze/grace, and the midweek bonus -- against
synthetic week histories, no database."""

from __future__ import annotations

from datetime import date

from telemetry.rating.config import RatingConfig
from telemetry.rating.streaks import (
    WeekCount,
    compute_streak,
    midweek_bonus_weeks,
    midweek_session_weight,
    qualifying_weeks,
    week_start,
)

CONFIG = RatingConfig()


def _mondays(*offsets_weeks: int) -> list[date]:
    base = date(2026, 1, 5)  # a Monday
    return [date.fromordinal(base.toordinal() + 7 * o) for o in offsets_weeks]


def test_week_start_is_the_monday():
    assert week_start(date(2026, 6, 3)).weekday() == 0  # Wednesday -> that week's Monday
    assert week_start(date(2026, 6, 1)).weekday() == 0  # Monday itself -> unchanged


def test_qualifying_weeks_filters_by_minimum_sessions():
    weeks = [
        WeekCount(week_start=date(2026, 1, 5), verified_session_count=1),
        WeekCount(week_start=date(2026, 1, 12), verified_session_count=0),
    ]
    assert qualifying_weeks(weeks, CONFIG) == [date(2026, 1, 5)]


def test_midweek_bonus_weeks_needs_at_least_two_sessions():
    weeks = [
        WeekCount(week_start=date(2026, 1, 5), verified_session_count=1),
        WeekCount(week_start=date(2026, 1, 12), verified_session_count=2),
    ]
    assert midweek_bonus_weeks(weeks, CONFIG) == {date(2026, 1, 12)}


def test_midweek_session_weight_full_for_first_reduced_after():
    assert midweek_session_weight(0, CONFIG) == 1.0
    assert midweek_session_weight(1, CONFIG) == CONFIG.midweek_sigma_weight
    assert midweek_session_weight(2, CONFIG) == CONFIG.midweek_sigma_weight


def test_no_history_gives_zero_streak_and_initial_freezes():
    state = compute_streak([], CONFIG)
    assert state.current_streak == 0
    assert state.freezes_available == CONFIG.streak_initial_freezes


def test_consecutive_weeks_build_a_streak():
    weeks = _mondays(0, 1, 2, 3)
    state = compute_streak(weeks, CONFIG)
    assert state.current_streak == 4
    assert state.longest_streak == 4


def test_single_missed_week_consumes_a_freeze_and_survives():
    weeks = _mondays(0, 1, 2, 4)  # week 3 missed
    state = compute_streak(weeks, CONFIG)
    assert state.current_streak == 4  # survived, still counts every qualifying week
    assert state.freezes_available == CONFIG.streak_initial_freezes - 1


def test_single_missed_week_breaks_it_once_freezes_are_exhausted():
    config = RatingConfig(streak_initial_freezes=0, streak_max_banked_freezes=0)
    weeks = _mondays(0, 1, 4)  # weeks 2,3 missed with zero freezes banked
    state = compute_streak(weeks, config)
    assert state.current_streak == 1  # broke, restarted at the last qualifying week


def test_two_consecutive_missed_weeks_always_breaks_the_streak():
    # Plenty of freezes banked, but the brief is explicit: two consecutive
    # misses breaks it regardless.
    config = RatingConfig(streak_initial_freezes=5, streak_max_banked_freezes=5)
    weeks = _mondays(0, 1, 2, 5)  # weeks 3 and 4 both missed
    state = compute_streak(weeks, config)
    assert state.current_streak == 1


def test_freezes_are_earned_on_interval_and_capped():
    config = RatingConfig(
        streak_initial_freezes=0, streak_max_banked_freezes=1, streak_freeze_earn_interval_weeks=3
    )
    weeks = _mondays(0, 1, 2)  # exactly one earn-interval of consecutive weeks
    state = compute_streak(weeks, config)
    assert state.freezes_available == 1  # earned, and capped at 1 even if more intervals pass

    weeks_longer = _mondays(0, 1, 2, 3, 4, 5)  # two earn-intervals worth
    state_longer = compute_streak(weeks_longer, config)
    assert state_longer.freezes_available == 1  # still capped


def test_longest_streak_survives_a_later_break():
    weeks = _mondays(0, 1, 2, 3, 6)  # 4-week streak, then a 2-week gap breaks it
    state = compute_streak(weeks, CONFIG)
    assert state.current_streak == 1
    assert state.longest_streak == 4
