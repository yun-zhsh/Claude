import io
import zipfile
from datetime import datetime

import pandas as pd
import pytest

from valuation_monitor.config import parse_config
from valuation_monitor.data import StockData
from valuation_monitor.filings import Announcement, build_history
from valuation_monitor.service import Monitor
from valuation_monitor.web import create_app, export_site


@pytest.fixture
def monitor(tmp_path):
    config = parse_config({"stocks": [{"symbol": "0700.HK", "name": "Tencent Holdings", "metrics": ["pe", "pe_non_ifrs"]}]})
    monitor = Monitor(config, data_dir=tmp_path, cache_dir=tmp_path / "cache")

    days = pd.bdate_range("2024-01-01", periods=300)
    close = pd.Series([300.0 + i % 50 for i in range(300)], index=days)
    earnings = build_history(
        [
            Announcement(pd.Period("2023Q4", freq="Q"), pd.Timestamp("2023-12-20 17:00"), "u1", "CNY", True,
                         {"eps": [5.0, 4.0, 4.0], "non_ifrs_eps": [6.0, 5.0, 5.0]}),
            Announcement(pd.Period("2024Q1", freq="Q"), pd.Timestamp("2023-12-21 17:00"), "u2", "CNY", True,
                         {"eps": [5.0, 4.0, 5.0], "non_ifrs_eps": [6.0, 5.0, 6.0]}),
            Announcement(pd.Period("2024Q2", freq="Q"), pd.Timestamp("2023-12-22 17:00"), "u3", "CNY", True,
                         {"eps": [5.0, 4.0, 5.0], "non_ifrs_eps": [6.0, 5.0, 6.0]}),
        ]
    )
    data = StockData(config.stocks[0], close, "HKD", pd.Series(1.1, index=days), earnings.ttm, earnings)
    monitor.loader.load = lambda stock: data
    monitor.price_check = lambda symbol: {"agree": 299, "tencent_only": 1}

    prices = monitor.prices.final_path("0700.HK")
    prices.parent.mkdir(parents=True)
    prices.write_text("date,close,source,status,tencent_close,yahoo_close_unadjusted,yahoo_factor\n"
                      "2024-01-01,300.0,tencent,agree,300.0,300.0,1.0\n")
    paths = monitor.earnings.paths("0700.HK")
    paths["periods"].parent.mkdir(parents=True)
    paths["periods"].write_text(earnings.periods_frame().to_csv(index=False))
    paths["ttm"].write_text(earnings.ttm_frame().to_csv(index=False))

    return monitor


@pytest.fixture
def client(monitor):
    app = create_app(monitor)
    app.testing = True
    return app.test_client()


def test_footer_shows_the_version(client):
    from valuation_monitor import __version__

    assert f"Stock Valuation Monitor v{__version__}" in client.get("/").get_data(as_text=True)


def test_index_lists_stocks(client):
    html = client.get("/").get_data(as_text=True)
    assert "Tencent Holdings" in html and "0700.HK" in html
    assert "/stock/0700.HK/" in html
    assert "Update data" in html
    assert "PE (TTM)" in html and "percentile" in html


def test_stock_page_has_charts_and_downloads(client):
    html = client.get("/stock/0700.HK/").get_data(as_text=True)
    assert 'id="chart-pe"' in html and 'id="chart-pe_non_ifrs"' in html and 'id="chart-price"' in html
    for name in ("prices.csv", "eps.csv", "ttm.csv", "raw.zip"):
        assert f"/stock/0700.HK/download/{name}" in html
    assert 'download="0700.HK_historical_prices.csv"' in html
    assert client.get("/stock/0700.HK").status_code == 308  # a URL without the trailing / redirects
    assert client.get("/stock/9999.HK/").status_code == 404


def test_downloads(client):
    r = client.get("/stock/0700.HK/download/prices.csv")
    assert r.status_code == 200
    text = r.data.decode("utf-8-sig")
    assert text.splitlines()[0].startswith("Date,Close (actual traded price)")
    assert "Tencent,Agree" in text
    assert "historical_prices" in r.headers["Content-Disposition"]

    assert client.get("/stock/0700.HK/download/eps.csv").data.decode("utf-8-sig").startswith("Period (quarter or half year),Currency")
    names = zipfile.ZipFile(io.BytesIO(client.get("/stock/0700.HK/download/raw.zip").data)).namelist()
    assert "prices/0700.HK/final.csv" in names and "eps/0700.HK_periods.csv" in names
    assert client.get("/stock/0700.HK/download/other").status_code == 404


def test_refresh_redirects_back(client):
    r = client.post("/refresh", data={"next": "/stock/0700.HK/"})
    assert r.status_code == 302 and r.headers["Location"].endswith("/stock/0700.HK/")
    for target in ("https://example.com", "//example.com"):
        r = client.post("/refresh", data={"next": target})
        assert "example.com" not in r.headers["Location"]


def test_export_site(monitor, tmp_path):
    out = tmp_path / "site"
    export_site(monitor, out, base_path="/Claude")
    index = (out / "index.html").read_text(encoding="utf-8")
    assert 'href="/Claude/stock/0700.HK/"' in index
    assert "/Claude/static/style.css" in index
    assert "/refresh" not in index and "Update data" not in index  # the static site has no update button
    page = (out / "stock" / "0700.HK" / "index.html").read_text(encoding="utf-8")
    assert "/Claude/stock/0700.HK/download/prices.csv" in page and "updated automatically on a schedule" in page
    assert (out / "stock" / "0700.HK" / "download" / "prices.csv").read_bytes().decode("utf-8-sig").startswith("Date")
    assert (out / "static" / "chart.umd.min.js").exists()
    assert (out / ".nojekyll").exists()


def test_drop_empty_series():
    from valuation_monitor.web.downloads import drop_empty_series

    df = pd.DataFrame(
        {
            "period": ["2025H1"],
            "eps": [1.5], "eps_published": ["2025-08-19 18:28"], "eps_effective": ["2025-08-20"], "eps_sources": [2], "eps_max_deviation": [0.0],
            "non_ifrs_eps": [None], "non_ifrs_eps_published": [None], "non_ifrs_eps_effective": [None],
            "non_ifrs_eps_sources": [0], "non_ifrs_eps_max_deviation": [None],
        }
    )
    out = drop_empty_series(df)
    assert list(out.columns) == ["period", "eps", "eps_published", "eps_effective", "eps_sources", "eps_max_deviation"]


def test_download_headers_are_unique():
    import collections

    from valuation_monitor.web import downloads

    for columns in (downloads.PERIOD_COLUMNS, downloads.TTM_COLUMNS, downloads.PRICE_COLUMNS):
        names = list(columns.values())
        assert [n for n, c in collections.Counter(names).items() if c > 1] == []


def view_json(html):
    import json
    import re

    return json.loads(re.search(r'<script type="application/json" id="view-data">(.*?)</script>', html, re.S).group(1))


def test_index_has_years_dropdown_and_all_windows(client):
    html = client.get("/").get_data(as_text=True)
    assert 'id="years-select"' in html
    for y in (10, 5, 3, 2, 1):
        assert f'<option value="{y}"' in html and f"Past {y} {'year' if y == 1 else 'years'}" in html
    assert '<option value="10" selected>' in html  # default 10 years
    assert 'data-keep-years' in html and 'data-ref="0700.HK|pe"' in html and "static/app.js" in html

    view = view_json(html)
    assert view["default"] == 10 and view["options"] == [10, 5, 3, 2, 1]
    windows = view["fields"]["0700.HK|pe"]
    assert set(windows) == {"10", "5", "3", "2", "1"}  # the order of keys does not matter; the page looks them up by key
    f = windows["1"]
    assert set(f) == {"current", "percentile", "level", "color", "range", "stats", "bands", "start", "band_values"}
    assert f["start"] > windows["10"]["start"]  # the one-year window starts later than the ten-year window
    assert set(f["band_values"]) == {"P10", "P30", "P50", "P70", "P90"}


def test_stock_page_has_chart_data_for_every_window(client):
    html = client.get("/stock/0700.HK/").get_data(as_text=True)
    view = view_json(html)
    assert view["symbol"] == "0700.HK"
    charts = view["charts"]
    assert {m["key"] for m in charts["metrics"]} == {"pe", "pe_non_ifrs"}
    assert set(charts["price"]["starts"]) == {"10", "5", "3", "2", "1"}
    # the chart data covers the longest lookback; the page cuts it to the selected years
    assert charts["metrics"][0]["dates"][0] >= view["fields"]["0700.HK|pe"]["10"]["start"]
    assert 'data-f="percentile"' in html and "ValuationView.onChange" in html
    assert 'href="/"' in html and "data-keep-years" in html


def test_export_includes_app_script(monitor, tmp_path):
    out = tmp_path / "site2"
    export_site(monitor, out, base_path="/Claude")
    assert (out / "static" / "app.js").exists()
    assert 'id="years-select"' in (out / "index.html").read_text(encoding="utf-8")


def test_valuation_download_shows_how_each_ratio_is_calculated(client):
    import io

    raw = client.get("/stock/0700.HK/download/valuation.csv").data.decode("utf-8-sig")
    df = pd.read_csv(io.StringIO(raw))
    close = "Close (price currency)"
    converted = "PE (TTM) | Per-share value (price currency) = per-share value x FX rate"
    ratio = "PE (TTM) | Ratio = close / per-share value (price currency)"
    ok = df[ratio].notna()
    assert ok.any()
    assert ((df.loc[ok, close] / df.loc[ok, converted]) - df.loc[ok, ratio]).abs().max() < 1e-9
    assert (df["PE (TTM) | Per-share value (reporting currency)"] * df["FX rate (1 unit of reporting currency = ? price currency)"] - df[converted]).abs().max() < 1e-9
    assert df.loc[ok, "PE (TTM) | Earnings record (period)"].iloc[-1] == "2024Q2"


def test_ttm_download_shows_the_components_and_formulas(client):
    import io

    df = pd.read_csv(io.StringIO(client.get("/stock/0700.HK/download/ttm.csv").data.decode("utf-8-sig")))
    row = df[df["TTM period 4"] == "2024Q2"].iloc[0]
    parts = [row[f"Period {k}: basic EPS"] for k in range(1, 5)]
    eps_ttm = "TTM EPS = sum of the periods' basic EPS"
    assert row[eps_ttm] == pytest.approx(sum(parts))
    assert row["TTM EPS formula (* = obtained by subtracting cumulative figures)"].endswith(f"= {row[eps_ttm]:g}")
    assert "2023Q3" in row["TTM EPS formula (* = obtained by subtracting cumulative figures)"]


def test_chart_payload_carries_the_calculation_for_the_tooltip(client):
    html = client.get("/stock/0700.HK/").get_data(as_text=True)
    charts = view_json(html)["charts"]["metrics"][0]
    n = len(charts["dates"])
    assert len(charts["price"]) == len(charts["rec"]) == n
    assert charts["fx"] and len(charts["fx"]) == n and charts["price_ccy"] == "HKD" and charts["report_ccy"] == "CNY"
    rec = charts["records"][str(charts["rec"][-1])]
    assert "TTM EPS" in rec["lines"][0] and "2024Q2" in rec["title"]
    # the ratio recomputed in the tooltip matches the value on the trend chart
    k = n - 1
    assert charts["price"][k] / (rec["per_share"] * charts["fx"][k]) == pytest.approx(charts["values"][k], abs=0.006)
