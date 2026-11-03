# Container image for Google Cloud Run: the web page (default command) and the scheduled update job
# (override the command with `valuation-monitor-update`). Nothing is baked in: the original filings live in a Cloud Storage
# bucket mounted at /data, and the SQLite database is a local copy of a bucket object (VALUATION_DB_URI, set at deploy time).
# See cloud.py and docs/deploy-google-cloud.md.
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends tzdata && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 1. Dependencies: this layer is rebuilt only when pyproject.toml changes. The package is installed as an empty stub (and the
#    build leftovers removed) so that editing the code does not reinstall pandas, matplotlib and the rest.
COPY pyproject.toml ./
RUN mkdir -p src/valuation_monitor && touch src/valuation_monitor/__init__.py README.md \
    && pip install --no-cache-dir '.[cloud]' \
    && rm -rf build src/*.egg-info

# 2. The code (changes often): installed on top without touching the dependencies.
COPY README.md ./
COPY src ./src
COPY config.yaml ./
RUN pip install --no-cache-dir --no-deps .

ENV VALUATION_CONFIG=/app/config.yaml \
    VALUATION_DATA_DIR=/data \
    VALUATION_DB_PATH=/tmp/valuation/valuation.db \
    VALUATION_CACHE_DIR=/tmp/cache \
    TZ=Asia/Hong_Kong \
    PYTHONUNBUFFERED=1

# One worker keeps the page state in one place; threads serve concurrent requests. The long timeout is for the
# "Update data" button, which fetches from the network while the request waits.
CMD exec gunicorn --bind ":${PORT:-8080}" --workers 1 --threads 4 --timeout 900 valuation_monitor.web.wsgi:app
