"""Driver Rating: field-relative pace scoring, kept strictly separate from
the streak/activity layer -- see supabase/migrations/0015_driver_rating.sql
for the storage, scripts/compute_ratings.py for the batch entry point.

    validity.py   Part 1 -- per-lap verified/flagged/excluded gate.
    elo.py        Part 2/3 -- cohort pace comparison, pairwise Elo-style mu
                  update, historical-reference fallback, sigma shrink.
    streaks.py    Part 4 -- weekly qualifying streaks, freeze/grace,
                  midweek bonus. Never touches mu/sigma.
    config.py     Every batch-side tunable, in one place, not hard-coded
                  into the modules above.
    engine.py     Orchestration: ties the above together into one
                  idempotent batch run.
"""
