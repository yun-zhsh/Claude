"""本地网页界面：首页列出跟踪的股票，每只股票有自己的页面显示走势图、数据和下载按钮。"""

from __future__ import annotations

import argparse
import io
import threading
import webbrowser

import pandas as pd
from flask import Flask, abort, redirect, render_template, request, send_file, url_for

from valuation_monitor.config import load_config
from valuation_monitor.service import PRICE_STATUS_LABELS, Monitor, StockReport
from valuation_monitor.web import downloads

# 从低估到高估的配色：深绿、绿、灰、橙、红
LEVEL_COLORS = ["#15803d", "#4ade80", "#9ca3af", "#fb923c", "#dc2626"]


def level_color(index: int | None, count: int) -> str:
    if index is None:
        return "#9ca3af"
    pos = round(index * (len(LEVEL_COLORS) - 1) / max(count - 1, 1))
    return LEVEL_COLORS[pos]


def chart_payload(report: StockReport, lookback_years: int) -> dict:
    """页面上画图需要的数据（只取回看区间内的部分）。"""
    charts = []
    for r in report.metrics:
        if r.valuation is None:
            continue
        window = r.series[r.series.index >= r.valuation.window_start].dropna()
        charts.append(
            {
                "key": r.metric.key,
                "label": r.metric.label,
                "dates": [f"{d:%Y-%m-%d}" for d in window.index],
                "values": [round(float(v), 2) for v in window],
                "bands": {f"P{p}": round(v, 2) for p, v in r.valuation.bands.items()},
                "current": round(r.valuation.current, 2),
            }
        )
    close = report.data.close
    close = close[close.index >= close.index[-1] - pd.DateOffset(years=lookback_years)]
    price = {"dates": [f"{d:%Y-%m-%d}" for d in close.index], "values": [round(float(v), 3) for v in close]}
    return {"metrics": charts, "price": price}


def create_app(monitor: Monitor) -> Flask:
    app = Flask(__name__)
    state: dict[str, StockReport] = {}
    lock = threading.Lock()

    def refresh() -> None:
        with lock:
            reports = monitor.analyze_all()
            state.clear()
            state.update({r.stock.symbol: r for r in reports})

    app.config["refresh"] = refresh

    @app.context_processor
    def helpers():
        levels = monitor.config.levels
        return {
            "level_color": lambda i: level_color(i, len(levels)),
            "levels": levels,
            "level_colors": [level_color(i, len(levels)) for i in range(len(levels))],
            "status_labels": PRICE_STATUS_LABELS,
            "lookback_years": monitor.config.lookback_years,
        }

    @app.get("/")
    def index():
        if not state:
            refresh()
        return render_template("index.html", reports=list(state.values()))

    @app.get("/stock/<symbol>")
    def stock(symbol: str):
        if not state:
            refresh()
        report = state.get(symbol) or abort(404)
        payload = chart_payload(report, monitor.config.lookback_years) if report.data is not None else None
        return render_template("stock.html", report=report, payload=payload, kinds=downloads.KINDS)

    @app.post("/refresh")
    def refresh_view():
        refresh()
        target = request.form.get("next", "")
        # 只允许跳回本站页面（"//host" 会被浏览器当成外部网址）
        return redirect(target if target.startswith("/") and not target.startswith("//") else url_for("index"))

    @app.get("/stock/<symbol>/download/<kind>")
    def download(symbol: str, kind: str):
        if symbol not in {s.symbol for s in monitor.config.stocks} or kind not in downloads.KINDS:
            abort(404)
        content, filename = downloads.build(monitor, symbol, kind)
        return send_file(io.BytesIO(content), as_attachment=True, download_name=filename)

    return app


def main(argv: list[str] | None = None) -> int:
    from valuation_monitor.cli import add_common_args

    parser = argparse.ArgumentParser(prog="valuation-monitor-web", description="以网页形式查看股票估值")
    add_common_args(parser)
    parser.add_argument("--port", type=int, default=8050, help="网页端口（默认 8050）")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址（默认只允许本机访问）")
    parser.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
    args = parser.parse_args(argv)

    monitor = Monitor(load_config(args.config), args.data_dir, args.cache_dir, args.refresh)
    app = create_app(monitor)
    print("正在更新数据……")
    app.config["refresh"]()
    url = f"http://{'127.0.0.1' if args.host in ('0.0.0.0', '::') else args.host}:{args.port}/"
    print(f"已启动：{url}（按 Ctrl+C 退出）")
    if not args.no_browser:
        threading.Timer(1.0, webbrowser.open, args=[url]).start()
    app.run(host=args.host, port=args.port, debug=False)
    return 0
