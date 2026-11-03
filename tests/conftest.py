"""Fixtures shared by several test files (pytest provides them automatically; test files never import each other)."""

import pandas as pd
import pytest

from valuation_monitor.config import parse_config
from valuation_monitor.data import StockData
from valuation_monitor.filings import Announcement, build_history
from valuation_monitor.service import Monitor
from valuation_monitor.web import create_app


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

    final = pd.DataFrame(
        {"close": [300.0], "source": ["tencent"], "status": ["agree"], "tencent_close": [300.0], "yahoo_close_unadjusted": [300.0], "yahoo_factor": [1.0]},
        index=pd.DatetimeIndex(["2024-01-01"], name="date"),
    )
    monitor.db.write_series("prices_final", {"symbol": "0700.HK"}, final)
    monitor.db.write_frame("periods", "0700.HK", earnings.periods_frame())
    monitor.db.write_frame("ttm", "0700.HK", earnings.ttm_frame(), date_format="%Y-%m-%d")

    return monitor


@pytest.fixture
def client(monitor):
    app = create_app(monitor)
    app.testing = True
    return app.test_client()
