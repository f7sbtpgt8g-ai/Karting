# Sample data

`default_session.tsv` is a real Unipro export, committed intentionally as
the app's default file during active development ("build phase") so it
loads automatically without re-uploading every time -- see `app.py`'s
`DEFAULT_TSV_PATH`. Uploading any file in the sidebar overrides it
immediately for that session; it does not touch the file on disk.

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
