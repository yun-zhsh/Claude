"""Web interface: the home page lists the tracked stocks; each stock has its own page with trend charts, data and download buttons.

Two ways to use it:
  - valuation-monitor-web: a dynamic page run locally, with an "Update data" button;
  - valuation-monitor-export: exports the same pages as a static site (e.g. published to GitHub Pages),
    without the "Update data" button; a scheduled job exports it again.
"""

from __future__ import annotations

import argparse
import io
import shutil
import sys
import threading
import time
import webbrowser
from collections.abc import Callable
from pathlib import Path

import pandas as pd
from flask import Flask, abort, redirect, render_template, request, send_file, url_for

from valuation_monitor import __version__
from valuation_monitor.audit import explain
from valuation_monitor.config import Config, load_config
from valuation_monitor.service import PRICE_STATUS_LABELS, MetricResult, Monitor, StockReport
from valuation_monitor.web import downloads

# Colors from undervalued to overvalued: dark green, green, grey, orange, red
LEVEL_COLORS = ["#15803d", "#4ade80", "#9ca3af", "#fb923c", "#dc2626"]


def level_color(index: int | None, count: int) -> str:
    if index is None:
        return "#9ca3af"
    pos = round(index * (len(LEVEL_COLORS) - 1) / max(count - 1, 1))
    return LEVEL_COLORS[pos]


def window_fields(result: MetricResult, years: int, level_count: int) -> dict:
    """The values shown on the page under one lookback in years (already formatted as text).

    The initial render (Jinja) and switching the lookback in the browser (app.js) use the same data, so the formats cannot differ.
    """
    w = result.windows[years]
    v = w.valuation
    return {
        "current": f"{v.current:.2f}",
        "percentile": f"{v.percentile:.1f}",
        "level": v.level,
        "color": level_color(w.level_index, level_count),
        "range": f"{v.window_start:%Y-%m-%d} ~ {v.as_of:%Y-%m-%d} ({v.observations} trading days)",
        "stats": f"{v.min:.1f} / {v.mean:.1f} / {v.max:.1f}",
        "bands": " · ".join(f"P{p} {val:.1f}" for p, val in v.bands.items()),
        "start": f"{v.window_start:%Y-%m-%d}",  # the trend chart starts drawing from this day
        "band_values": {f"P{p}": round(val, 2) for p, val in v.bands.items()},
    }


def metric_views(report: StockReport, config: Config) -> list[dict]:
    """What is displayed for each metric under each lookback in years."""
    return [
        {
            "key": r.metric.key,
            "label": r.metric.label,
            "error": r.error,
            "windows": {y: window_fields(r, y, len(config.levels)) for y in r.windows},
        }
        for r in report.metrics
    ]


def view_data(reports: list[StockReport], config: Config, **extra) -> dict:
    """Data embedded in the page: app.js uses it to update the page when the lookback is switched."""
    fields = {
        f"{r.stock.symbol}|{m['key']}": m["windows"] for r in reports for m in metric_views(r, config)
    }
    return {"default": config.lookback_years, "options": config.lookback_options, "fields": fields, **extra}


def audit_payload(report: StockReport, result: MetricResult, window: pd.Series) -> dict:
    """The calculation shown in the chart tooltip: close and FX rate of every day, the earnings record used, and the formula of each earnings record.

    In the tooltip, ratio = close / (per-share value x FX rate); how the per-share value is calculated can also be found here.
    """
    data = report.data
    detail = result.metric.detail(data).loc[window.index]
    records = {}
    earnings = data.earnings
    for i in sorted({int(r) for r in detail["record"].dropna()}):
        row = data.fundamentals.iloc[i]
        rec = {
            "title": f"{earnings.ttm_detail[i]['period_label']} results (published {row['published']})" if earnings and earnings.ttm_detail else str(row["published"]),
            "per_share": float(row[result.metric.column]),
            "lines": explain(result.metric.column, earnings.ttm_detail[i]) if earnings and earnings.ttm_detail else [],
        }
        records[str(i)] = rec
    fx = detail["fx"]
    return {
        "price": [round(float(v), 3) for v in detail["close"]],
        "fx": None if (fx == 1).all() else [round(float(v), 6) for v in fx],
        "rec": [int(r) for r in detail["record"]],
        "records": records,
        "price_ccy": data.price_currency,
        "report_ccy": earnings.currency if earnings else data.price_currency,
    }


def chart_payload(report: StockReport, config: Config) -> dict:
    """Data of the trend charts: all data within the longest lookback; the page cuts it to the selected years."""
    charts = []
    for r in report.metrics:
        if not r.windows:
            continue
        start = r.windows[max(r.windows)].valuation.window_start
        window = r.series[r.series.index >= start].dropna()
        charts.append(
            {
                "key": r.metric.key,
                "label": r.metric.label,
                "dates": [f"{d:%Y-%m-%d}" for d in window.index],
                "values": [round(float(v), 2) for v in window],
                **audit_payload(report, r, window),
            }
        )
    close = report.data.close
    as_of = close.index[-1]
    close = close[close.index >= as_of - pd.DateOffset(years=config.max_lookback)]
    return {
        "metrics": charts,
        "price": {
            "dates": [f"{d:%Y-%m-%d}" for d in close.index],
            "values": [round(float(v), 3) for v in close],
            "starts": {y: f"{as_of - pd.DateOffset(years=y):%Y-%m-%d}" for y in config.lookback_options},
            "ccy": report.data.price_currency,
        },
    }


def create_app(
    monitor: Monitor,
    static_site: bool = False,
    reload_seconds: float = 0,
    cached_load_offline: bool = False,
    sync: Callable[[], object] | None = None,
    update_remotely: Callable[[], object] | None = None,
) -> Flask:
    """reload_seconds: when > 0, pages older than this are rebuilt from the saved data on the next request (a server whose data
    is updated by another process, e.g. a scheduled job). cached_load_offline: such automatic loads do not use the network;
    only the "Update data" button (and `refresh()`) fetch.

    sync: called before each automatic rebuild, to bring the saved data up to date first (e.g. download a newer database).
    update_remotely: when given, the "Update data" button calls this (e.g. starts the update job) instead of fetching itself;
    the page then shows that an update was started.
    """
    app = Flask(__name__)
    state: dict[str, StockReport] = {}
    loaded_at = [0.0]
    lock = threading.Lock()

    def refresh(offline: bool = False) -> None:
        with lock:
            previous = monitor.offline
            monitor.offline = offline
            try:
                reports = monitor.analyze_all()
            finally:
                monitor.offline = previous
            state.clear()
            state.update({r.stock.symbol: r for r in reports})
            loaded_at[0] = time.monotonic()

    sync_lock = threading.Lock()

    def ensure_state() -> None:
        def stale() -> bool:
            return not state or (reload_seconds > 0 and time.monotonic() - loaded_at[0] > reload_seconds)

        if stale():
            with sync_lock:  # one request does the work, the others wait and then find it done
                if stale():
                    if sync:
                        sync()
                    refresh(offline=cached_load_offline)

    app.config["refresh"] = refresh

    @app.context_processor
    def helpers():
        levels = monitor.config.levels
        return {
            "level_color": lambda i: level_color(i, len(levels)),
            "levels": levels,
            "level_colors": [level_color(i, len(levels)) for i in range(len(levels))],
            "status_labels": PRICE_STATUS_LABELS,
            "default_years": monitor.config.lookback_years,
            "year_options": monitor.config.lookback_options,
            "max_lookback_years": monitor.config.max_lookback,
            "static_site": static_site,
            "version": __version__,
            "download_name": downloads.download_name,
        }

    @app.get("/")
    def index():
        ensure_state()
        reports = list(state.values())
        views = {r.stock.symbol: metric_views(r, monitor.config) for r in reports}
        return render_template("index.html", reports=reports, views=views, data=view_data(reports, monitor.config))

    # page URLs end with / and download URLs carry an extension, so the URLs stay the same after exporting a static site
    @app.get("/stock/<symbol>/")
    def stock(symbol: str):
        ensure_state()
        report = state.get(symbol) or abort(404)
        payload = chart_payload(report, monitor.config) if report.data is not None else None
        data = view_data([report], monitor.config, symbol=symbol, charts=payload)
        return render_template("stock.html", report=report, views=metric_views(report, monitor.config), data=data)

    @app.post("/refresh")
    def refresh_view():
        target = request.form.get("next", "")
        # only allow redirecting back to pages of this site ("//host" would be taken by the browser as an external URL)
        target = target if target.startswith("/") and not target.startswith("//") else url_for("index")
        if update_remotely:
            try:
                update_remotely()
                note = "updating=1"
            except Exception as e:  # e.g. missing permission: the page says so instead of failing
                print(f"Could not start the update: {e}", file=sys.stderr)
                note = "update_failed=1"
            return redirect(f"{target}{'&' if '?' in target else '?'}{note}")
        refresh()
        return redirect(target)

    @app.get("/stock/<symbol>/download/<name>")
    def download(symbol: str, name: str):
        if symbol not in {s.symbol for s in monitor.config.stocks} or name not in downloads.FILES:
            abort(404)
        ensure_state()
        content = downloads.build(monitor, symbol, name, state.get(symbol))
        return send_file(io.BytesIO(content), as_attachment=True, download_name=downloads.download_name(symbol, name))

    return app


def main(argv: list[str] | None = None) -> int:
    from valuation_monitor.cli import add_common_args

    parser = argparse.ArgumentParser(prog="valuation-monitor-web", description="View stock valuations as a web page")
    add_common_args(parser)
    parser.add_argument("--port", type=int, default=8050, help="Port of the web page (default: 8050)")
    parser.add_argument("--host", default="127.0.0.1", help="Address to listen on (default: this machine only)")
    parser.add_argument("--no-browser", action="store_true", help="Do not open the browser automatically")
    args = parser.parse_args(argv)

    monitor = Monitor(load_config(args.config), args.data_dir, args.cache_dir, args.refresh, db_path=args.db)
    app = create_app(monitor)
    print("Updating data...")
    app.config["refresh"]()
    url = f"http://{'127.0.0.1' if args.host in ('0.0.0.0', '::') else args.host}:{args.port}/"
    print(f"Started: {url} (press Ctrl+C to quit)")
    if not args.no_browser:
        threading.Timer(1.0, webbrowser.open, args=[url]).start()
    app.run(host=args.host, port=args.port, debug=False)
    return 0


def export_site(monitor: Monitor, out: str | Path, base_path: str = "") -> list[Path]:
    """Export all pages and downloads as a static site. base_path is the sub-path the site lives under (e.g. /repo-name on GitHub Pages)."""
    out = Path(out)
    app = create_app(monitor, static_site=True)
    app.config["refresh"]()
    client = app.test_client()
    base_url = f"http://localhost{base_path.rstrip('/')}/"

    pages = {"/": "index.html"}
    for stock in monitor.config.stocks:
        pages[f"/stock/{stock.symbol}/"] = f"stock/{stock.symbol}/index.html"
        for name in downloads.FILES:
            pages[f"/stock/{stock.symbol}/download/{name}"] = f"stock/{stock.symbol}/download/{name}"

    if out.exists():
        shutil.rmtree(out)
    written = []
    for url, rel in pages.items():
        resp = client.get(url, base_url=base_url)
        if resp.status_code != 200:
            raise RuntimeError(f"Failed to export {url}: HTTP {resp.status_code}")
        target = out / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(resp.data)
        written.append(target)
    shutil.copytree(app.static_folder, out / "static")
    (out / ".nojekyll").touch()  # make GitHub Pages publish as is, without Jekyll processing
    return written


def export_main(argv: list[str] | None = None) -> int:
    from valuation_monitor.cli import add_common_args

    parser = argparse.ArgumentParser(prog="valuation-monitor-export", description="Update the data and export a static website")
    add_common_args(parser)
    parser.add_argument("--out", default="site", help="Output directory (default: site; it is emptied first)")
    parser.add_argument("--base-path", default="", help="Sub-path the site is served from, e.g. /repository-name for a GitHub Pages project site")
    args = parser.parse_args(argv)

    monitor = Monitor(load_config(args.config), args.data_dir, args.cache_dir, args.refresh, db_path=args.db)
    written = export_site(monitor, args.out, args.base_path)
    print(f"Exported {len(written)} files to {args.out}/")
    return 0
