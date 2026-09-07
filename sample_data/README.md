# Sample data

`default_session.tsv` is a real Unipro export, committed intentionally so
there's a real (not synthetic) file to test and verify against without
needing your own export -- `tests/test_worker.py`,
`tests/test_backfill_analysis.py`, `tests/test_analysis_store.py`,
`tests/test_ingest_paths.py` and `scripts/verify_analysis_extraction.py`
all read it directly.

**This repo is currently public**, so this file (and the GPS track,
lap times, and RPM data in it) is visible to anyone who finds the repo.
That trade-off was chosen deliberately for convenience during this build
phase -- reconsider before treating this repo as a long-term home for real
telemetry, e.g. by making it private or swapping to an external, non-public
file store.

Everything else you drop in this directory is gitignored as before (only
`README.md` and `default_session.tsv` are tracked).

The synthetic fixture used by the automated test suite is separate, at
`tests/fixtures/synthetic_session.tsv` -- see the main README's "Status"
section for the real-world quirks validating against `default_session.tsv`
surfaced and fixed.
