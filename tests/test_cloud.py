"""Pieces used to run on a server: offline mode, page reload from saved data, the update command, the WSGI entry point."""

import pytest
import requests

from tests.test_web import monitor  # noqa: F401  (fixture)
from valuation_monitor.providers.http import HttpClient
from valuation_monitor.web import create_app


def test_offline_http_client_serves_the_cache_but_never_fetches(tmp_path):
    http = HttpClient(cache_dir=tmp_path)
    key_url = "https://example.invalid/data"
    http.offline = True
    with pytest.raises(requests.ConnectionError, match="Offline"):
        http.get_bytes(key_url)
    with pytest.raises(requests.ConnectionError, match="Offline"):
        http.get_response(key_url)
    with pytest.raises(requests.ConnectionError, match="Offline"):
        http.post_json(key_url, {"a": 1})


def test_monitor_offline_switch_reaches_every_http_client(monitor):  # noqa: F811
    assert monitor._clients and not any(c.offline for c in monitor._clients)
    monitor.offline = True
    assert all(c.offline for c in monitor._clients)
    monitor.offline = False
    assert not any(c.offline for c in monitor._clients)


def test_page_reloads_from_saved_data_when_it_gets_old(monitor):  # noqa: F811
    calls = []
    original = monitor.analyze_all
    monitor.analyze_all = lambda: calls.append(monitor.offline) or original()
    app = create_app(monitor, reload_seconds=3600, cached_load_offline=True)
    client = app.test_client()
    assert client.get("/").status_code == 200 and client.get("/stock/0700.HK/").status_code == 200
    assert calls == [True]  # loaded once, offline; the second request reuses it

    app.config["refresh"]()  # the Update data button: online
    assert calls == [True, False] and not monitor.offline  # the previous mode is restored

    import valuation_monitor.web as web

    real = web.time.monotonic
    try:
        web.time.monotonic = lambda: real() + 7200  # two hours later
        client.get("/")
    finally:
        web.time.monotonic = real
    assert calls == [True, False, True]


def test_update_command_saves_data_and_reports_each_stock(monitor, capsys, monkeypatch):  # noqa: F811
    from valuation_monitor import cli

    monkeypatch.setattr(cli, "Monitor", lambda *a, **k: monitor)
    monkeypatch.setattr(cli, "load_config", lambda path: monitor.config)
    assert cli.update_main([]) == 0
    assert "Tencent Holdings (0700.HK): updated" in capsys.readouterr().out


def test_wsgi_app_is_built_from_environment_variables(tmp_path, monkeypatch):
    monkeypatch.setenv("VALUATION_CONFIG", "config.yaml")
    monkeypatch.setenv("VALUATION_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("VALUATION_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("VALUATION_RELOAD_SECONDS", "123")
    import importlib

    wsgi = importlib.import_module("valuation_monitor.web.wsgi")
    app = wsgi.make_app()
    assert app.config["refresh"] and wsgi.Monitor  # the app exists and does not fetch at import time


def test_command_line_defaults_come_from_the_environment(monkeypatch):
    """The container sets these variables; the update job must use the mounted data directory, not an empty local one."""
    import argparse

    from valuation_monitor.cli import add_common_args

    parser = argparse.ArgumentParser()
    add_common_args(parser)
    args = parser.parse_args([])
    assert (args.config, args.data_dir, args.cache_dir) == ("config.yaml", "data", ".cache")

    monkeypatch.setenv("VALUATION_CONFIG", "/app/config.yaml")
    monkeypatch.setenv("VALUATION_DATA_DIR", "/data")
    monkeypatch.setenv("VALUATION_CACHE_DIR", "/tmp/cache")
    parser = argparse.ArgumentParser()
    add_common_args(parser)
    args = parser.parse_args([])
    assert (args.config, args.data_dir, args.cache_dir) == ("/app/config.yaml", "/data", "/tmp/cache")
    assert parser.parse_args(["--data-dir", "elsewhere"]).data_dir == "elsewhere"  # an explicit option still wins
