# Container image for Google Cloud Run: the web page (default command) and the scheduled update job
# (override the command with `valuation-monitor-update`). The data is not baked in: it lives in a Cloud Storage
# bucket mounted at /data. See docs/deploy-google-cloud.md.
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends tzdata && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY config.yaml ./
RUN pip install --no-cache-dir '.[cloud]'

ENV VALUATION_CONFIG=/app/config.yaml \
    VALUATION_DATA_DIR=/data \
    VALUATION_CACHE_DIR=/tmp/cache \
    TZ=Asia/Hong_Kong \
    PYTHONUNBUFFERED=1

# One worker keeps the page state in one place; threads serve concurrent requests. The long timeout is for the
# "Update data" button, which fetches from the network while the request waits.
CMD exec gunicorn --bind ":${PORT:-8080}" --workers 1 --threads 4 --timeout 900 valuation_monitor.web.wsgi:app
