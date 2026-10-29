"""网页界面：首页列出跟踪的股票，每只股票有自己的页面显示走势图、数据和下载按钮。

两种用法：
  - valuation-monitor-web：本地运行的动态网页，可以随时点“更新数据”；
  - valuation-monitor-export：用同一套页面导出静态网站（如发布到 GitHub Pages），
    没有“更新数据”按钮，由定时任务重新导出。
"""

from __future__ import annotations

import argparse
import io
import shutil
import threading
import webbrowser
from pathlib import Path

import pandas as pd
from flask import Flask, abort, redirect, render_template, request, send_file, url_for

from valuation_monitor.audit import explain
from valuation_monitor.config import Config, load_config
from valuation_monitor.service import PRICE_STATUS_LABELS, MetricResult, Monitor, StockReport
from valuation_monitor.web import downloads

# 从低估到高估的配色：深绿、绿、灰、橙、红
LEVEL_COLORS = ["#15803d", "#4ade80", "#9ca3af", "#fb923c", "#dc2626"]


def level_color(index: int | None, count: int) -> str:
    if index is None:
        return "#9ca3af"
    pos = round(index * (len(LEVEL_COLORS) - 1) / max(count - 1, 1))
    return LEVEL_COLORS[pos]


def window_fields(result: MetricResult, years: int, level_count: int) -> dict:
    """某个回看年数下，页面上要显示的各项数值（已格式化成文字）。

    页面初始渲染（Jinja）和在浏览器里切换回看年数（app.js）用的是同一份，格式不会不一致。
    """
    w = result.windows[years]
    v = w.valuation
    return {
        "current": f"{v.current:.2f}",
        "percentile": f"{v.percentile:.1f}",
        "level": v.level,
        "color": level_color(w.level_index, level_count),
        "range": f"{v.window_start:%Y-%m-%d} ~ {v.as_of:%Y-%m-%d}（{v.observations} 个交易日）",
        "stats": f"{v.min:.1f} / {v.mean:.1f} / {v.max:.1f}",
        "bands": " · ".join(f"P{p} {val:.1f}" for p, val in v.bands.items()),
        "start": f"{v.window_start:%Y-%m-%d}",  # 走势图从这天开始画
        "band_values": {f"P{p}": round(val, 2) for p, val in v.bands.items()},
    }


def metric_views(report: StockReport, config: Config) -> list[dict]:
    """每个指标在各个回看年数下的显示内容。"""
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
    """嵌入页面的数据：app.js 据此在切换回看年数时更新页面。"""
    fields = {
        f"{r.stock.symbol}|{m['key']}": m["windows"] for r in reports for m in metric_views(r, config)
    }
    return {"default": config.lookback_years, "options": config.lookback_options, "fields": fields, **extra}


def audit_payload(report: StockReport, result: MetricResult, window: pd.Series) -> dict:
    """走势图提示框要显示的计算过程：每天的收盘价、汇率、采用的业绩记录，以及各条业绩记录的算式。

    提示框里的 比率 = 收盘价 ÷ （每股数据 × 汇率），其中每股数据又是怎么算出来的，都能在这里找到。
    """
    data = report.data
    detail = result.metric.detail(data).loc[window.index]
    records = {}
    earnings = data.earnings
    for i in sorted({int(r) for r in detail["record"].dropna()}):
        row = data.fundamentals.iloc[i]
        rec = {
            "title": f"{earnings.ttm_detail[i]['period_label']} 业绩（{row['published']} 发布）" if earnings and earnings.ttm_detail else str(row["published"]),
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
    """走势图的数据：取最长回看年数内的全部数据，页面上按所选年数截取。"""
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


def create_app(monitor: Monitor, static_site: bool = False) -> Flask:
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
            "default_years": monitor.config.lookback_years,
            "year_options": monitor.config.lookback_options,
            "max_lookback_years": monitor.config.max_lookback,
            "static_site": static_site,
            "download_name": downloads.download_name,
        }

    @app.get("/")
    def index():
        if not state:
            refresh()
        reports = list(state.values())
        views = {r.stock.symbol: metric_views(r, monitor.config) for r in reports}
        return render_template("index.html", reports=reports, views=views, data=view_data(reports, monitor.config))

    # 页面网址以 / 结尾、下载文件网址带扩展名，这样导出成静态网站后网址保持不变
    @app.get("/stock/<symbol>/")
    def stock(symbol: str):
        if not state:
            refresh()
        report = state.get(symbol) or abort(404)
        payload = chart_payload(report, monitor.config) if report.data is not None else None
        data = view_data([report], monitor.config, symbol=symbol, charts=payload)
        return render_template("stock.html", report=report, views=metric_views(report, monitor.config), data=data)

    @app.post("/refresh")
    def refresh_view():
        refresh()
        target = request.form.get("next", "")
        # 只允许跳回本站页面（"//host" 会被浏览器当成外部网址）
        return redirect(target if target.startswith("/") and not target.startswith("//") else url_for("index"))

    @app.get("/stock/<symbol>/download/<name>")
    def download(symbol: str, name: str):
        if symbol not in {s.symbol for s in monitor.config.stocks} or name not in downloads.FILES:
            abort(404)
        if not state:
            refresh()
        content = downloads.build(monitor, symbol, name, state.get(symbol))
        return send_file(io.BytesIO(content), as_attachment=True, download_name=downloads.download_name(symbol, name))

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


def export_site(monitor: Monitor, out: str | Path, base_path: str = "") -> list[Path]:
    """把所有页面和下载文件导出为静态网站。base_path 是网站所在的子路径（如 GitHub Pages 的 /仓库名）。"""
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
            raise RuntimeError(f"导出 {url} 失败：HTTP {resp.status_code}")
        target = out / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(resp.data)
        written.append(target)
    shutil.copytree(app.static_folder, out / "static")
    (out / ".nojekyll").touch()  # 让 GitHub Pages 原样发布，不经过 Jekyll 处理
    return written


def export_main(argv: list[str] | None = None) -> int:
    from valuation_monitor.cli import add_common_args

    parser = argparse.ArgumentParser(prog="valuation-monitor-export", description="更新数据并导出静态网站")
    add_common_args(parser)
    parser.add_argument("--out", default="site", help="输出目录（默认 site，会先清空）")
    parser.add_argument("--base-path", default="", help="网站所在的子路径，如 GitHub Pages 项目网站为 /仓库名")
    args = parser.parse_args(argv)

    monitor = Monitor(load_config(args.config), args.data_dir, args.cache_dir, args.refresh)
    written = export_site(monitor, args.out, args.base_path)
    print(f"已导出 {len(written)} 个文件到 {args.out}/")
    return 0
