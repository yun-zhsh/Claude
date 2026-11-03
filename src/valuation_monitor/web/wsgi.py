"""WSGI entry point for running the web page on a server (e.g. Google Cloud Run behind gunicorn).

    gunicorn valuation_monitor.web.wsgi:app

Settings come from environment variables:
  VALUATION_CONFIG         path of the config file (default: config.yaml)
  VALUATION_DATA_DIR       where the original filings are saved (default: data); on Cloud Run a mounted bucket
  VALUATION_DB_PATH        the SQLite database (default: valuation.db in the data directory); on Cloud Run a local file
  VALUATION_DB_URI         gs://bucket/valuation.db: the bucket object the database is downloaded from (see cloud.py)
  VALUATION_UPDATE_JOB     projects/PROJECT/locations/REGION/jobs/NAME: the update job the "Update data" button starts
  VALUATION_CACHE_DIR      the request cache directory (default: .cache)
  VALUATION_RELOAD_SECONDS pages are rebuilt when older than this (default: 600); a newer database is downloaded first, so data
                           saved by the scheduled update job shows up without restarting the server
The server itself never fetches and never uploads the database: the scheduled job (valuation-monitor-update) does. The "Update data"
button starts that job when VALUATION_UPDATE_JOB is set.
"""

from __future__ import annotations

import os
from pathlib import Path

from valuation_monitor.cloud import RemoteDatabase, start_job
from valuation_monitor.config import load_config
from valuation_monitor.service import Monitor
from valuation_monitor.web import create_app


def make_app():
    data_dir = os.environ.get("VALUATION_DATA_DIR", "data")
    db_path = Path(os.environ.get("VALUATION_DB_PATH") or Path(data_dir) / "valuation.db")
    remote = RemoteDatabase.from_env(db_path)
    if remote:
        remote.pull()
    job = os.environ.get("VALUATION_UPDATE_JOB", "").strip()
    monitor = Monitor(
        load_config(os.environ.get("VALUATION_CONFIG", "config.yaml")),
        data_dir,
        os.environ.get("VALUATION_CACHE_DIR", ".cache"),
        db_path=db_path,
    )
    return create_app(
        monitor,
        reload_seconds=float(os.environ.get("VALUATION_RELOAD_SECONDS", "600")),
        cached_load_offline=True,
        sync=remote.pull if remote else None,
        update_remotely=(lambda: start_job(job)) if job else None,
    )


app = make_app()
