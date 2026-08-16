# Sample data

Drop real Unipro TSV exports here for local testing (this directory is
gitignored except for this file, so real telemetry never gets committed).

The synthetic fixture used by the automated test suite lives at
`tests/fixtures/synthetic_session.tsv` instead -- see the main README's
"Status / important caveat" section for why: the real sample file
referenced by the original project spec was not available when this tool
was built, so the parser has only been validated against a simulated
fixture that reproduces the documented format quirks. Drop a real export
here and re-run `python -m pytest` / the app against it as the first real
validation step.
