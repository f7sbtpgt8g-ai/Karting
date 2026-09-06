"""Background ingest worker.

Runs the parsing pipeline outside the web request cycle, on a container with
a real CPU budget and no request timeout -- a genuine Unipro export is tens
of megabytes and ~900k sparse rows, which takes ~18s to parse and is well
past what a serverless function is built for.

Deliberately thin: everything it computes comes from `telemetry.analysis`
and `telemetry.storage`, unchanged. Extracting that façade (Part 1) is what
makes this module small enough to be obviously correct.
"""
