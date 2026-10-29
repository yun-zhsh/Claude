import io
import zipfile
from datetime import datetime

import pandas as pd
import pytest

from valuation_monitor.config import parse_config
from valuation_monitor.data import StockData
from valuation_monitor.filings import Announcement, build_history
from valuation_monitor.service import Monitor
from valuation_monitor.web import create_app


@pytest.fixture
def client(tmp_path):
    config = parse_config({"stocks": [{"symbol": "0700.HK", "name": "腾讯控股", "metrics": ["pe", "pe_non_ifrs"]}]})
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
    paths["quarterly"].parent.mkdir(parents=True)
    paths["quarterly"].write_text(earnings.quarters_frame().to_csv(index=False))
    paths["ttm"].write_text(earnings.ttm.to_csv(index=False))

    app = create_app(monitor)
    app.testing = True
    return app.test_client()


def test_index_lists_stocks(client):
    html = client.get("/").get_data(as_text=True)
    assert "腾讯控股" in html and "0700.HK" in html
    assert "/stock/0700.HK" in html
    assert "PE (TTM)" in html and "分位" in html


def test_stock_page_has_charts_and_downloads(client):
    html = client.get("/stock/0700.HK").get_data(as_text=True)
    assert 'id="chart-pe"' in html and 'id="chart-pe_non_ifrs"' in html and 'id="chart-price"' in html
    for kind in ("prices", "eps", "ttm", "raw"):
        assert f"/stock/0700.HK/download/{kind}" in html
    assert client.get("/stock/9999.HK").status_code == 404


def test_downloads(client):
    r = client.get("/stock/0700.HK/download/prices")
    assert r.status_code == 200
    text = r.data.decode("utf-8-sig")
    assert text.splitlines()[0].startswith("日期,收盘价（实际成交价）")
    assert "腾讯证券,一致" in text
    assert "历史股价" in r.headers["Content-Disposition"] or "%E5%8E%86%E5%8F%B2" in r.headers["Content-Disposition"]

    assert client.get("/stock/0700.HK/download/eps").data.decode("utf-8-sig").startswith("季度,币种")
    names = zipfile.ZipFile(io.BytesIO(client.get("/stock/0700.HK/download/raw").data)).namelist()
    assert "prices/0700.HK/final.csv" in names and "eps/0700.HK_quarterly.csv" in names
    assert client.get("/stock/0700.HK/download/other").status_code == 404


def test_refresh_redirects_back(client):
    r = client.post("/refresh", data={"next": "/stock/0700.HK"})
    assert r.status_code == 302 and r.headers["Location"].endswith("/stock/0700.HK")
    for target in ("https://example.com", "//example.com"):
        r = client.post("/refresh", data={"next": target})
        assert "example.com" not in r.headers["Location"]
