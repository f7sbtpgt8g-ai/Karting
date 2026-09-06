# Ingest worker: polls `upload_batches`, parses uploaded Unipro exports,
# writes sessions/laps/session_cache. Build from the repo root:
#
#   docker build -f worker/Dockerfile -t karting-worker .
#   docker run --env-file worker/.env karting-worker
#
# On Railway/Render/Fly, point the service at this Dockerfile with the repo
# root as build context -- it needs `telemetry/` as well as `worker/`.

FROM python:3.12-slim

# Only for psycopg2/pyarrow wheels' runtime deps; the -binary wheels mean no
# compiler is needed, so the image stays small.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY worker/requirements.txt ./worker/requirements.txt
RUN pip install --no-cache-dir -r worker/requirements.txt

# The parser and analysis code, unchanged from what the Streamlit app runs.
COPY telemetry/ ./telemetry/
COPY worker/ ./worker/

# Not root: this process holds the service-role key, so it gets no more
# privilege in the container than it needs either.
RUN useradd --create-home --uid 10001 worker && chown -R worker /app
USER worker

CMD ["python", "-m", "worker.main"]
