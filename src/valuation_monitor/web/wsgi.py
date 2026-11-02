"""WSGI entry point for running the web page on a server (e.g. Google Cloud Run behind gunicorn).

    gunicorn valuation_monitor.web.wsgi:app

Settings come from environment variables:
  VALUATION_CONFIG         path of the config file (default: config.yaml)
  VALUATION_DATA_DIR       where prices, filings and EPS data are saved (default: data); on Cloud Run a mounted bucket
  VALUATION_CACHE_DIR      the request cache directory (default: .cache)
  VALUATION_RELOAD_SECONDS pages are rebuilt from the saved data when older than this (default: 600), so data saved by the
                           scheduled update job shows up without restarting the server
The server itself never fetches in the background: the scheduled job (valuation-monitor-update) does, and the "Update data"
button fetches on demand.
"""

from __future__ import annotations

import os

from valuation_monitor.config import load_config
from valuation_monitor.service import Monitor
from valuation_monitor.web import create_app


def make_app():
    monitor = Monitor(
        load_config(os.environ.get("VALUATION_CONFIG", "config.yaml")),
        os.environ.get("VALUATION_DATA_DIR", "data"),
        os.environ.get("VALUATION_CACHE_DIR", ".cache"),
    )
    return create_app(monitor, reload_seconds=float(os.environ.get("VALUATION_RELOAD_SECONDS", "600")), cached_load_offline=True)


app = make_app()
